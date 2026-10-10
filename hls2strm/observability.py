"""日志（stdout + 滚动文件 + 内存环形缓冲推给 Web）与运行指标。"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import re
import secrets
import queue
import sys
import time
from collections import Counter, deque
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from urllib.parse import urlsplit
from pathlib import Path

from .errors import RelayAborted
from .runtime import log_area, request_id, traffic

LOG_AREAS = ("play", "task", "system")
_PLAY_LOGGERS = frozenset(("play", "playback", "playback_selection", "line_speed", "health"))
_SYSTEM_LOGGERS = frozenset(("app", "api", "auth", "config", "config_transfer", "runtime", "observability", "logs"))
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s]: %(message)s"
_listener = None


def redact(message: str) -> str:
    def safe(match):
        try:
            url = urlsplit(match.group(0))
            host = url.hostname or "?"
            if ":" in host:
                host = f"[{host}]"
            return f"{url.scheme}://{host}{':' + str(url.port) if url.port else ''}/…"
        except ValueError:
            return "[URL]"
    return re.sub(r"https?://[^\s）)]+", safe, message)


class SafeQueueHandler(QueueHandler):
    def prepare(self, record):
        record.request_id = request_id.get() or "-"
        rec = super().prepare(record)
        rec.msg = redact(rec.msg)
        return rec

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            ring.dropped += 1
            self.queue.put_nowait(record)


def shutdown_logging():
    global _listener
    if _listener is not None:
        while _listener.queue.full():
            try:
                _listener.queue.get_nowait()
            except queue.Empty:
                break
        _listener.stop()
        for handler in _listener.handlers:
            handler.close()
        _listener = None


def log_area_of(name: str) -> str:
    """日志分在哪个区：play 播放（播放请求、线路测速、连通性检测），task 任务（抓取等后台任务、归并、订阅调度），
    system 系统（启动、接口操作、登录等）。抓取刷屏时各区分开存，播放日志不会被挤掉。"""
    if traffic.get() in ("play", "speed"):
        return "play"
    if area := log_area.get():
        return area
    if name in _PLAY_LOGGERS:
        return "play"
    return "system" if name in _SYSTEM_LOGGERS or name.startswith("uvicorn") else "task"


class RingHandler(logging.Handler):
    """每个分区保留最近 N 条日志，并推送给订阅者（SSE）。"""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: dict[str, deque[dict]] = {area: deque(maxlen=capacity) for area in LOG_AREAS}
        self._ids = itertools.count(1)
        self.instance = secrets.token_hex(8)
        self.dropped = 0
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = redact(record.getMessage())
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            name = record.name.removeprefix("hls2strm.")
            item = {
                "id": next(self._ids),
                "ts": record.created,
                "level": record.levelname,
                "name": name,
                "area": log_area_of(name),
                "msg": msg,
                "request_id": request_id.get(),
            }
            self.records[item["area"]].append(item)
            if self._loop is not None and self._subscribers:
                for q in list(self._subscribers):
                    self._loop.call_soon_threadsafe(self._offer, q, item)
        except Exception:
            self.handleError(record)

    def _offer(self, q: asyncio.Queue, item: dict) -> None:
        if q.full():
            q.get_nowait()
            self.dropped += 1
        q.put_nowait(item)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def since(self, after_id: int = 0, limit: int = 500, area: str = "") -> list[dict]:
        """after_id 之后的日志，最多 limit 条（取最新的）；area 为空时各区合在一起按先后排，否则只取这个区。"""
        if area:
            return [r for r in self.records.get(area, ()) if r["id"] > after_id][-limit:]
        return list(heapq.merge(*([r for r in q if r["id"] > after_id] for q in self.records.values()),
                                key=lambda r: r["id"]))[-limit:]

    def backlog(self, after_id: int = 0, per_area: int = 300) -> list[dict]:
        """每个区各取最近 per_area 条，合在一起按先后排：刚打开页面时，抓取刷屏也能看到之前的播放日志。"""
        return list(heapq.merge(*(self.since(after_id, per_area, area) for area in LOG_AREAS), key=lambda r: r["id"]))


ring = RingHandler()


class UvicornFilter(logging.Filter):
    """uvicorn 的服务器日志（启动、监听、异常）都记在 uvicorn.error 下，不是错误，显示成 uvicorn；
    中转断流（RelayAborted）已经记过一行，丢掉 uvicorn 那条带 traceback 的。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.name = "uvicorn"
        return not (record.exc_info and isinstance(record.exc_info[1], RelayAborted))


def setup_logging(data_dir: Path, level: str = "INFO") -> None:
    global _listener
    shutdown_logging()
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(LOG_FORMAT)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(fmt)
    file = RotatingFileHandler(log_dir / "hls2strm.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    pending = queue.Queue(maxsize=2048)
    _listener = QueueListener(pending, stdout, file)
    _listener.start()
    for h in (SafeQueueHandler(pending), ring):
        root.addHandler(h)
    set_level(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.INFO)
    uv = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, UvicornFilter) for f in uv.filters):
        uv.addFilter(UvicornFilter())


def set_level(level: str) -> None:
    """本服务的日志级别，运行中也能改（不保存，重启后回到 HLS2STRM_LOG_LEVEL）。"""
    logging.getLogger("hls2strm").setLevel(level.upper())


def get_level() -> str:
    return logging.getLevelName(logging.getLogger("hls2strm").getEffectiveLevel())


class Metrics:
    """进程内计数器 + 最近一段时间的请求速率。"""

    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.started_at = time.time()
        self._requests: deque[float] = deque(maxlen=10000)
        self.timings: dict[str, deque] = {}
        self.gauges: Counter[str] = Counter()

    def observe(self, key: str, milliseconds: float) -> None:
        if key not in self.timings and len(self.timings) >= 128:
            return
        self.timings.setdefault(key, deque(maxlen=512)).append((time.time(), milliseconds))

    def timing_snapshot(self) -> dict:
        out = {}
        for key, samples in self.timings.items():
            vals = sorted(v for ts, v in samples if ts >= time.time() - 900)
            if vals:
                out[key] = {"count": len(vals), "p50_ms": round(vals[(len(vals)-1)//2], 1),
                            "p95_ms": round(vals[min(len(vals)-1, int(len(vals)*.95))], 1), "max_ms": round(vals[-1], 1)}
        return out

    def inc(self, key: str, n: int = 1) -> None:
        self.counters[key] += n

    def request(self) -> None:
        self._requests.append(time.time())

    def requests_per_minute(self) -> int:
        cutoff = time.time() - 60
        return sum(1 for t in self._requests if t >= cutoff)

    def snapshot(self) -> dict:
        return {
            "uptime": int(time.time() - self.started_at),
            "requests_per_minute": self.requests_per_minute(),
            "counters": dict(self.counters),
            "timings": self.timing_snapshot(),
            "gauges": dict(self.gauges),
            "log_dropped": ring.dropped,
        }
