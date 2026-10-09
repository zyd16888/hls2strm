"""独立 HTTP 资源与有界媒体缓冲。curl 生产者在缓冲满时暂停，由消费方恢复。"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_PAUSE, CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.utils import set_curl_options

from .runtime import stage

BUFFER_BYTES = 512 * 1024


class MediaResponse:
    def __init__(self, session, curl, metrics):
        self.session, self.curl, self.metrics = session, curl, metrics
        self.chunks = deque()
        self.buffered = 0
        self.paused = False
        self.closed = False
        self.finished = False
        self.error = None
        self.ready = asyncio.Event()
        self.quit_now = asyncio.Event()
        self.task = None
        self.status_code = 0
        self.headers = {}
        self.started = time.monotonic()

    def write(self, data):
        if self.quit_now.is_set():
            return CURL_WRITEFUNC_ERROR
        if self.buffered + len(data) > BUFFER_BYTES:
            self.paused = True
            self.metrics.inc("relay_buffer_pauses")
            return CURL_WRITEFUNC_PAUSE
        self.chunks.append(data)
        self.buffered += len(data)
        self.metrics.gauges["relay_buffer_bytes"] += len(data)
        self.ready.set()
        return len(data)

    async def aiter_content(self):
        first = True
        while True:
            if self.chunks:
                data = self.chunks.popleft()
                self.buffered -= len(data)
                self.metrics.gauges["relay_buffer_bytes"] -= len(data)
                if first:
                    self.metrics.observe("relay.ttfb", (time.monotonic()-self.started)*1000)
                    first = False
                yield data
                if self.paused and not self.finished and not self.closed:
                    self.paused = False
                    self.curl.pause(0)
            elif self.finished:
                if self.error:
                    raise self.error
                return
            else:
                self.ready.clear()
                await self.ready.wait()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.quit_now.set()
        if self.task and not self.task.done():
            self.session.acurl.remove_handle(self.curl)
            self.task.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        self.metrics.gauges["relay_buffer_bytes"] -= self.buffered
        self.buffered = 0
        self.chunks.clear()
        self.session.release_curl(self.curl)


class ManagedSession:
    """连接额度包含排队和完整媒体消费；设置变更后等待活动请求完成再退役。"""

    def __init__(self, settings, metrics, kind: str, capacity: int):
        self.raw = AsyncSession(impersonate=settings.impersonate or "chrome", proxy=settings.proxy or None,
                                trust_env=False, max_clients=capacity)
        self.metrics, self.kind = metrics, kind
        self.slots = asyncio.Semaphore(capacity)
        self.active = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def request(self, method, url, **kw):
        async with asyncio.timeout(kw.get("timeout", 30)):
            return await self._request(method, url, **kw)

    async def _request(self, method, url, **kw):
        self.active += 1
        self.idle.clear()
        handed_off, acquired = False, False
        try:
            with stage(self.metrics, f"pool.{self.kind}.wait"):
                await self.slots.acquire()
            acquired = True
            self.metrics.gauges[f"http_active_{self.kind}"] += 1
            with stage(self.metrics, f"upstream.{self.kind}"):
                if kw.pop("stream", False):
                    resp = await self._stream(url, **kw)
                    close = resp.aclose
                    async def release():
                        if resp.closed:
                            return
                        try:
                            await close()
                        finally:
                            self._release(True)
                    resp.aclose = release
                    handed_off = True
                    return resp
                async with asyncio.timeout(kw.get("timeout", 30)):
                    return await self.raw.request(method, url, **kw)
        finally:
            if not handed_off:
                self._release(acquired)

    def _release(self, acquired):
        if acquired:
            self.slots.release()
            self.metrics.gauges[f"http_active_{self.kind}"] -= 1
        self.active -= 1
        if not self.active:
            self.idle.set()

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def _stream(self, url, *, headers=None, timeout=30, **kw):
        curl = await self.raw.pop_curl()
        resp = MediaResponse(self.raw, curl, self.metrics)
        try:
            _, buffer, header_buffer, _, _, _ = set_curl_options(
                curl, "GET", url, params_list=[self.raw.params, None],
                headers_list=[self.raw.headers, headers], cookies_list=[self.raw.cookies, None],
                proxies_list=[self.raw.proxies, None], verify_list=[self.raw.verify, None], impersonate=self.raw.impersonate,
                timeout=timeout, stream=True, queue_class=asyncio.Queue, event_class=asyncio.Event,
                allow_redirects=True,
            )
        except BaseException:
            self.raw.release_curl(curl)
            raise
        curl.setopt(CurlOpt.WRITEFUNCTION, resp.write)
        curl.setopt(CurlOpt.TIMEOUT_MS, 0)
        future = self.raw.acurl.add_handle(curl)

        async def perform():
            try:
                await future
            except Exception as error:
                resp.error = error
            finally:
                resp.finished = True
                resp.ready.set()
        resp.task = asyncio.create_task(perform())
        try:
            async with asyncio.timeout(timeout):
                await resp.ready.wait()
            if resp.error and not resp.chunks:
                raise resp.error
            parsed = self.raw._parse_response(curl, buffer, header_buffer, "utf-8", False)
            resp.status_code, resp.headers = parsed.status_code, parsed.headers
            return resp
        except BaseException:
            await resp.aclose()
            raise

    async def close(self):
        await self.raw.close()
