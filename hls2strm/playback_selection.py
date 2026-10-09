"""播放候选预检：有限读取 CDN，失败只排除当前源/线路，不修改活动会话。"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from urllib.parse import urljoin
from curl_cffi import CurlError

from .errors import FetchError, NotFound, ParseError
from .fetcher import close_stream
from .quality import filter_master
from .runtime import deadline, traffic


def candidate_key(resolved) -> tuple[int, int]:
    return resolved.source["id"], resolved.line["id"] if resolved.line else 0


class PlaybackSelection:
    def __init__(self, resolver):
        self.resolver = resolver
        self.checked = OrderedDict()
        self.cooling = OrderedDict()

    async def probe(self, resolved, want=None):
        fetcher = self.resolver.fetcher
        headers = resolved.traits.headers
        cache_key = (resolved.url, tuple(sorted(headers.items())), want)
        if self.checked.get(cache_key, 0) > time.monotonic():
            return
        target = resolved.url
        if target.split("?", 1)[0].lower().endswith(".m3u8"):
            for _ in range(4):
                body = (await getattr(fetcher, "get_playlist", fetcher.get_bytes)(target, headers=headers)).decode("utf-8", "replace")
                if not body.lstrip().startswith("#EXTM3U"):
                    raise FetchError("CDN 没有返回有效的 HLS 清单")
                if "#EXT-X-STREAM-INF" in body:
                    picked = filter_master(body, want)
                    ref = picked[1] if picked else next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
                    if not ref:
                        raise FetchError("主清单没有视频档位")
                    target = urljoin(target, ref)
                    continue
                ref = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
                if not ref:
                    raise FetchError("播放清单没有分片")
                target = urljoin(target, ref)
                break
            else:
                raise FetchError("播放清单嵌套过深")
        session = getattr(fetcher, "media_session", fetcher.session)
        response = await session.get(target, stream=True, headers={**headers, "Range": "bytes=0-32767"},
                                     timeout=self.resolver.store.current.resolve_attempt_timeout)
        try:
            if response.status_code not in (200, 206):
                raise FetchError(f"媒体预检返回 HTTP {response.status_code}")
            kind = response.headers.get("content-type", "").lower()
            if "text/html" in kind or "application/json" in kind:
                raise FetchError("CDN 返回错误页面而非媒体")
            got = 0
            async for chunk in response.aiter_content():
                got += len(chunk)
                if got >= 32768:
                    break
            if not got:
                raise FetchError("媒体预检返回空内容")
        finally:
            await close_stream(response)
        self.checked[cache_key] = time.monotonic() + 30
        self.checked.move_to_end(cache_key)
        while len(self.checked) > 512:
            self.checked.popitem(last=False)

    async def choose(self, slug: str, *, excluded=frozenset(), **options):
        resolver = self.resolver
        limit = deadline.get() or time.monotonic() + resolver.store.current.resolve_timeout
        dt, tt = deadline.set(limit), traffic.set("play")
        blocked = set(excluded)
        # 显式试播指定站点/线路仍允许重试，普通自动选源暂时避开近期坏候选。
        cooling = {key for key, until in self.cooling.items() if until > time.monotonic()} if not options.get("site") else set()
        blocked.update(cooling)
        failures = []
        try:
            async with asyncio.timeout_at(limit):
                while True:
                    try:
                        resolved = await resolver.resolve(slug, excluded=frozenset(blocked), **options)
                    except (NotFound, FetchError, ParseError):
                        if cooling:
                            # 没有更好的源时重试冷却源，保留本轮失败和用户排除。
                            blocked.difference_update(cooling - set(excluded))
                            cooling.clear()
                            continue
                        if failures:
                            raise FetchError("；".join(failures)) from None
                        raise
                    key = candidate_key(resolved)
                    try:
                        await asyncio.wait_for(self.probe(resolved, options.get("want")),
                                               resolver.store.current.resolve_attempt_timeout)
                    except (FetchError, NotFound, ParseError, TimeoutError, OSError, CurlError) as error:
                        message = str(error) or "媒体预检超时"
                        failures.append(f"{resolved.site.label} {resolved.line['line'] if resolved.line else ''}：{message}")
                        blocked.add(key)
                        cooling.discard(key)
                        self.cooling[key] = time.monotonic() + 300
                        self.cooling.move_to_end(key)
                        while len(self.cooling) > 512:
                            self.cooling.popitem(last=False)
                        if resolved.line:
                            await resolver.db.line_failed(resolved.line["id"], message)
                        else:
                            await resolver.db.source_failed(resolved.source["id"], message)
                        resolver.metrics.inc("play_preflight_failover")
                        continue
                    self.cooling.pop(key, None)
                    return resolved
        except TimeoutError:
            raise FetchError("播放选源与媒体预检超过总时间预算") from None
        finally:
            deadline.reset(dt)
            traffic.reset(tt)
