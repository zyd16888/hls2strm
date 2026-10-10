"""请求上下文：时间预算、流量类别和关联编号。"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import contextmanager
from contextlib import asynccontextmanager
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="")
deadline: ContextVar[float | None] = ContextVar("deadline", default=None)
traffic: ContextVar[str] = ContextVar("traffic", default="background")
log_area: ContextVar[str] = ContextVar("log_area", default="")  # 日志分区（见 observability.log_area_of），空的按流量和模块判断
attempt_wait_state: ContextVar[dict | None] = ContextVar("attempt_wait_state", default=None)


@asynccontextmanager
async def local_queue():
    """记录单次候选预算是否在本服务排队时耗尽，不把拥塞记为源故障。"""
    state = attempt_wait_state.get()
    try:
        yield
    except asyncio.CancelledError:
        if state is not None:
            state["local_timeout"] = True
        raise


@contextmanager
def background():
    tokens = (deadline.set(None), traffic.set("background"), request_id.set(""))
    try:
        yield
    finally:
        deadline.reset(tokens[0])
        traffic.reset(tokens[1])
        request_id.reset(tokens[2])


@contextmanager
def stage(metrics, name: str):
    start = time.monotonic()
    try:
        yield
    finally:
        metrics.observe(name, (time.monotonic() - start) * 1000)


@asynccontextmanager
async def measured_lock(metrics, lock, name: str):
    with stage(metrics, name):
        async with local_queue():
            await lock.acquire()
    try:
        yield
    finally:
        lock.release()


class JsonCompression:
    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1024, compresslevel=2)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/") and scope["path"] != "/api/logs/stream":
            return await self.compressed(scope, receive, send)
        return await self.app(scope, receive, send)


class RequestTelemetry:
    """ASGI 中间件：预算只约束响应开始前，媒体传输由停滞超时约束。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ctx = getattr(scope["app"].state, "ctx", None)
        if ctx is None:
            return await self.app(scope, receive, send)
        path = scope["path"]
        playback = path.startswith(("/play/", "/hls/", "/media/", "/api/resolve/"))
        tokens = (request_id.set(secrets.token_hex(8)), traffic.set("play" if playback else "background"))
        budget = time.monotonic() + ctx.store.current.resolve_timeout if playback else None
        dt = deadline.set(budget)
        started, status = time.monotonic(), 500
        headers_sent = False
        timer = asyncio.timeout_at(budget)

        async def observed_send(message):
            nonlocal headers_sent, status
            if message["type"] == "http.response.start":
                headers_sent, status = True, message["status"]
                if not timer.expired():
                    timer.reschedule(None)
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.get().encode()))
                headers.append((b"server-timing", f"app;dur={(time.monotonic()-started)*1000:.1f}".encode()))
                message = {**message, "headers": headers}
                route = getattr(scope.get("route"), "path", "other")
                ctx.metrics.observe(f"http.{route}", (time.monotonic()-started)*1000)
            await send(message)

        try:
            async with timer:
                await self.app(scope, receive, observed_send)
        except TimeoutError:
            if headers_sent:
                raise
            ctx.metrics.inc("play_deadline_exceeded")
            from starlette.responses import JSONResponse
            await JSONResponse({"detail": "播放解析超过总时间预算"}, status_code=504)(scope, receive, observed_send)
        finally:
            ctx.metrics.inc(f"http_{status // 100}xx")
            if status >= 500 or (time.monotonic()-started > 1 and not path.startswith(("/hls/", "/media/", "/static/", "/api/logs/stream"))):
                logging.getLogger(__name__).info("请求 %s %s %s HTTP %d %.0fms", request_id.get(), scope["method"],
                                                 getattr(scope.get("route"), "path", "other"), status,
                                                 (time.monotonic()-started)*1000)
            deadline.reset(dt)
            traffic.reset(tokens[1])
            request_id.reset(tokens[0])
