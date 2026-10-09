"""外部播放器的固定 VOD HLS：按时间生成独立分片，有界转码、缓存与候选切换。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .errors import FetchError, NotFound, ParseError
from .playback_selection import candidate_key

SEGMENT_SECONDS = 6
MAX_SESSIONS = 4
IDLE_SECONDS = 180
MAX_SEGMENT_BYTES = 16 * 1024 * 1024


@dataclass
class ContinuousSession:
    id: str
    resolved: object
    duration: float
    height: int
    site: str | None = None
    line: str | None = None
    excluded: set = field(default_factory=set)
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inspected: dict = field(default_factory=dict)


class ContinuousPlayback:
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = (ctx.boot.data_dir / "continuous").resolve()
        self.sessions = {}
        self.jobs = {}
        self.cache = OrderedDict()
        self.workers = ctx.store.current.continuous_workers
        self.slots = asyncio.Semaphore(self.workers)
        self.sweeper = None
        self.closed = False
        self.restoring = set()

    def start(self):
        if self.root.exists():
            for directory in self.root.iterdir():
                if directory.is_dir() and re.fullmatch(r"[0-9a-f]{24}", directory.name):
                    for path in directory.glob("*.ts"):
                        if path.stem.isdigit():
                            self.cache[(directory.name, int(path.stem))] = (path, path.stat().st_size)
            self._trim()
        self.sweeper = asyncio.create_task(self._sweep())

    async def close(self):
        self.closed = True
        tasks = list(self.jobs.values()) + ([self.sweeper] if self.sweeper else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        # 保留有界的完成分片与会话描述，重启后可恢复；未完成文件由生成任务 finally 删除。
        self.sessions.clear()
        self.cache.clear()

    def _remove(self, session):
        self.sessions.pop(session, None)
        directory = (self.root / session).resolve()
        if directory.parent == self.root and re.fullmatch(r"[0-9a-f]{24}", session):
            shutil.rmtree(directory, ignore_errors=True)
        for key in list(self.cache):
            if key[0] == session:
                self.cache.pop(key)

    async def _sweep(self):
        while True:
            await asyncio.sleep(30)
            active = {key[0] for key in self.jobs}
            for key, value in list(self.sessions.items()):
                if key not in active and time.monotonic() - value.touched > IDLE_SECONDS:
                    self._remove(key)
            if self.root.exists():
                for directory in self.root.iterdir():
                    if (directory.is_dir() and re.fullmatch(r"[0-9a-f]{24}", directory.name)
                            and directory.name not in self.sessions and directory.name not in self.restoring
                            and time.time() - directory.stat().st_mtime > IDLE_SECONDS):
                        self._remove(directory.name)

    def _available(self):
        settings = self.ctx.store.current
        if not settings.continuous_enabled:
            raise HTTPException(409, "连续播放未启用，请在设置中启用后重试")
        if not shutil.which(settings.continuous_ffmpeg) or not shutil.which(settings.continuous_ffprobe):
            raise HTTPException(503, "连续播放需要 FFmpeg 和 FFprobe，请检查安装与路径")
        if self.closed:
            raise HTTPException(503, "播放服务正在退出")

    def local_url(self, session_id: str, source_id: int) -> str:
        query = urlencode({"s": session_id, "t": self.ctx.store.current.play_token, "variants": "highest"})
        return f"http://127.0.0.1:{self.ctx.boot.port}/media/{source_id}?{query}"

    async def _process(self, *args):
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), self.ctx.store.current.continuous_timeout)
            if process.returncode:
                # FFmpeg 日志可能带签名 URL，客户端仅显示阶段，不泄露上游令牌。
                raise FetchError("媒体定位或转码失败")
            return stdout
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    async def inspect(self, session, resolved):
        key = (candidate_key(resolved), resolved.url)
        if key in session.inspected:
            return session.inspected[key]
        media_id = await self.ctx.resolver.sessions.persist(self.ctx.db, resolved)
        url = self.local_url(media_id, resolved.source["id"])
        raw = await self._process(self.ctx.store.current.continuous_ffprobe, "-v", "error", "-rw_timeout", "15000000",
                                  "-show_entries", "format=duration:stream=codec_type", "-of", "json", url)
        data = json.loads(raw)
        duration = float(data.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0 or not any(s["codec_type"] == "video" for s in data.get("streams", [])):
            raise FetchError("连续播放需要可定位的完整视频及有效时长")
        audio = any(s["codec_type"] == "audio" for s in data.get("streams", []))
        session.inspected[key] = (url, duration, audio)
        return url, duration, audio

    async def create(self, resolved, *, site=None, line=None, want=None):
        self._available()
        if len(self.sessions) + len(self.restoring) >= MAX_SESSIONS:
            raise HTTPException(503, "连续播放会话已满，空闲会话稍后自动释放", headers={"Retry-After": "30"})
        sid = await self.ctx.resolver.sessions.persist(self.ctx.db, resolved)
        height = min(resolved.source.get("height") or self.ctx.store.current.continuous_height,
                     self.ctx.store.current.continuous_height)
        if want is not None:
            height = min(height, want)
        height = max(360, height // 2 * 2)
        session = ContinuousSession(sid, resolved, 0, height, site, line)
        self.sessions[sid] = session  # 在 await 前占用会话额度。
        try:
            async with self.slots:
                _, session.duration, _ = await self.inspect(session, resolved)
            self.root.mkdir(parents=True, exist_ok=True)
            directory = self.root / sid
            directory.mkdir(exist_ok=True)
            # 仅保存本服务会话引用，源快照仍由 play_sessions 持久化。
            (directory / "session.json").write_text(json.dumps({"source_id": resolved.source["id"],
                "duration": session.duration, "height": height, "site": site, "line": line}), encoding="utf-8")
            return session
        except BaseException:
            self._remove(sid)
            raise

    async def get(self, sid):
        self._available()
        if not re.fullmatch(r"[0-9a-f]{24}", sid):
            raise HTTPException(404, "播放会话不存在")
        session = self.sessions.get(sid)
        if session is None:
            if sid in self.restoring or len(self.sessions) + len(self.restoring) >= MAX_SESSIONS:
                raise HTTPException(503, "连续播放会话已满")
            self.restoring.add(sid)
            try:
                data = json.loads((self.root / sid / "session.json").read_text(encoding="utf-8"))
                resolved = await self.ctx.resolver.sessions.restore(self.ctx.db, sid, data["source_id"])
                session = ContinuousSession(sid, resolved, data["duration"], data["height"], data["site"], data["line"])
            except (OSError, ValueError, KeyError, NotFound):
                raise HTTPException(404, "连续播放会话已失效，请重新打开影片") from None
            finally:
                self.restoring.discard(sid)
            self.sessions[sid] = session
        session.touched = time.monotonic()
        return session

    def playlist(self, session, token: str):
        # 每片独立编码并声明 discontinuity，支持直接跳到任意 VOD 分片。
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
        query = "?" + urlencode({"t": token}) if token else ""
        for index in range(math.ceil(session.duration / SEGMENT_SECONDS)):
            if index:
                lines.append("#EXT-X-DISCONTINUITY")
            length = min(SEGMENT_SECONDS, session.duration - index * SEGMENT_SECONDS)
            lines += [f"#EXTINF:{length:.3f},", f"../continuous/{session.id}/{index}.ts{query}"]
        return "\n".join(lines + ["#EXT-X-ENDLIST", ""])

    async def segment(self, session, index):
        if index < 0 or index >= math.ceil(session.duration / SEGMENT_SECONDS):
            raise HTTPException(404)
        key = session.id, index
        path = self.root / session.id / f"{index}.ts"
        if path.exists():
            self.cache[key] = (path, path.stat().st_size)
            self.cache.move_to_end(key)
            data = path.read_bytes()
            self._trim()
            return data
        if key not in self.jobs:
            if len(self.jobs) >= self.workers * 2:
                raise HTTPException(503, "转码队列已满，请稍后重试", headers={"Retry-After": "3"})
            task = asyncio.create_task(self._generate(session, index, path))
            self.jobs[key] = task
            def finished(done):
                self.jobs.pop(key, None)
                if not done.cancelled():
                    done.exception()  # 请求已断开时也回收任务异常。
            task.add_done_callback(finished)
        return await asyncio.shield(self.jobs[key])

    async def _generate(self, session, index, path):
        temporary = path.with_suffix(".part")
        try:
            return await self._generate_into(session, index, path, temporary)
        finally:
            temporary.unlink(missing_ok=True)

    async def _generate_into(self, session, index, path, temporary):
        async with asyncio.timeout(self.ctx.store.current.continuous_timeout):
            async with self.slots, session.lock:
                for attempt in range(3):
                    resolved = session.resolved
                    try:
                        url, duration, audio = await asyncio.wait_for(self.inspect(session, resolved),
                            max(3, self.ctx.store.current.continuous_timeout / 3))
                        if abs(duration - session.duration) > max(10, session.duration * .01):
                            raise FetchError("备用源时长不一致，不能自动续接")
                        await asyncio.wait_for(self._encode(url, audio, session, index, temporary),
                            max(3, self.ctx.store.current.continuous_timeout / 3))
                        data = temporary.read_bytes()
                        if not data or len(data) >= MAX_SEGMENT_BYTES:
                            raise FetchError("生成的分片为空或超过大小限制")
                        temporary.replace(path)
                        self.cache[(session.id, index)] = (path, len(data))
                        self._trim()
                        return data
                    except (FetchError, OSError, ValueError, TimeoutError) as error:
                        temporary.unlink(missing_ok=True)
                        session.excluded.add(candidate_key(resolved))
                        if attempt == 2:
                            raise FetchError(f"连续播放候选已用尽：{error}") from None
                        session.resolved = await self.ctx.resolver.selection.choose(resolved.video["slug"],
                            relay=True, min_remaining=60, site=session.site, line=session.line,
                            excluded=frozenset(session.excluded))
                        self.ctx.metrics.inc("continuous_switch")
        raise FetchError("连续播放分片生成失败")

    async def _encode(self, url, audio, session, index, path):
        start = index * SEGMENT_SECONDS
        length = min(SEGMENT_SECONDS, session.duration - start)
        height = session.height
        width = (height * 16 // 9) // 2 * 2
        args = [self.ctx.store.current.continuous_ffmpeg, "-nostdin", "-v", "error", "-y", "-rw_timeout", "15000000",
                "-ss", str(start), "-i", url]
        if not audio:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        args += ["-t", str(length), "-map", "0:v:0", "-map", "0:a:0" if audio else "1:a:0",
                 "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                 "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "2", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-output_ts_offset", str(start),
                 "-muxdelay", "0", "-f", "mpegts", "-fs", str(MAX_SEGMENT_BYTES), str(path)]
        await self._process(*args)

    def _trim(self):
        limit = self.ctx.store.current.continuous_cache_mb * 1024 * 1024
        size = sum(value[1] for value in self.cache.values())
        while size > limit and self.cache:
            _, (path, length) = self.cache.popitem(last=False)
            path.unlink(missing_ok=True)
            size -= length


router = APIRouter()


@router.get("/continuous/{session_id}/{name}")
async def continuous_media(session_id: str, name: str, request: Request, t: str = ""):
    from .play import _check_token, playback_source
    _check_token(request, t)
    service = request.app.state.ctx.continuous
    session = await service.get(session_id)
    if name == "info":
        return {"source": playback_source(session.resolved), "duration": session.duration, "height": session.height,
                "mode": "continuous", "state": "serving", "switches": len(session.excluded)}
    if name == "index.m3u8":
        return Response(service.playlist(session, t).replace("../continuous/", "../../continuous/"),
                        media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    if not re.fullmatch(r"\d+\.ts", name):
        raise HTTPException(404)
    try:
        return Response(await service.segment(session, int(name[:-3])), media_type="video/mp2t")
    except (FetchError, NotFound, ParseError, TimeoutError) as error:
        raise HTTPException(502, str(error) or "连续播放处理超时") from None
