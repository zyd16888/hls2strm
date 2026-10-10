"""播放解析：strm 指向 /play/{slug}.m3u8，挑一个源换取新鲜的播放地址，302 过去或由本服务中转。

一部作品可以有多个源（不同站点）。挑源顺序见 Resolver.rank；某个源取地址失败（下架、被拦、解析失败）就换下一个。
多线路站点（SupJav 等）的一个源下面还有多条线路：按设置里的顺序试，每条线路各自缓存直链、各自冷却。
中转：/hls/{源 id}/{相对路径} 按源的播放地址所在目录拼出上游地址，带上站点要求的请求头。
多层播放列表（master → 子清单 → 分片）里的地址都改写成本服务的相对地址；不在同一目录下的地址签名后放进 _x/。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, replace
from weakref import WeakValueDictionary
from urllib.parse import quote, urlencode, urljoin

from curl_cffi import CurlError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from .config import SettingsStore
from .db import Database, source_cooldown
from .errors import RelayAborted, PoolBusy
from .fetcher import Blocked, FetchError, Fetcher, NotFound, close_stream
from .observability import Metrics
from .parser import ParseError, VideoGone
from .health import HealthTracker, host_key
from .quality import QualityProber, filter_master, from_master, label as quality_label, needed as quality_needed
from .quality import parse_heights, parse_label, tier as quality_tier
from .sites.hosts import ts_start
from .sites import SITES, Site, SourceDetail, StreamTraits, find_by_code, get_site
from .runtime import deadline as request_deadline, traffic, stage, measured_lock, attempt_wait_state
from .playback import PlaybackSessions
from .playback_selection import PlaybackSelection

log = logging.getLogger(__name__)

DEFAULT_DURATION = 2 * 3600
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
HLS_PATH_RE = re.compile(
    r"^(?:_x/[0-9a-f]{16}/[A-Za-z0-9_-]+(?:\.[a-z0-9]{1,5})?|[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+){0,5})$")
_EXT_RE = re.compile(r"\.([a-z0-9]{1,5})$")
DISGUISE_EXTS = (".jpeg", ".jpg", ".png", ".gif", ".webp", ".html", ".txt", ".js", ".css", ".woff2", ".woff", ".svg",
                 ".xml", ".json", ".vtt", ".srt")
SUBTITLE_CODES = {"zh": "zh", "none": "", "en": "en"}
_SIGN_KEY = secrets.token_bytes(16)  # 独立使用时的后备密钥；应用启动时从数据库的持久密钥派生。


def set_signing_key(secret: str) -> None:
    global _SIGN_KEY
    _SIGN_KEY = hashlib.sha256(("hls2strm-play:" + secret).encode()).digest()


class NoDirectSource(Exception):
    """作品有源，但没有播放器能直连的（都要中转）。"""


class CandidateExcluded(FetchError):
    """本轮主动跳过候选，不是源站失败，不能写入全局冷却。"""


@dataclass
class Resolved:
    video: dict
    source: dict
    site: Site
    line: dict | None = None  # 多线路站点：这次用的线路
    proxy_forced: bool = False  # 线路设置了强制中转
    allow_direct: bool = False

    @property
    def traits(self) -> StreamTraits:
        if self.line is None:
            return self.site.stream
        t = self.site.line_traits(self.line["line"], self.line["host"])
        if self.line.get("referer"):
            t = replace(t, headers={**t.headers, "Referer": self.line["referer"]})
        if self.proxy_forced:
            return replace(t, direct=False)
        return replace(t, ip_bound=False, ip_uncertain=False) if self.allow_direct and t.direct else t

    @property
    def host(self) -> str:
        """播放站（连通性按它记）。"""
        return host_key(self.site, self.line)

    @property
    def url(self) -> str:
        return (self.line or self.source)["stream_url"]

    @property
    def expires(self) -> int | None:
        return (self.line or self.source)["stream_expires"]


class Resolver:
    """按作品挑源、维护各源的播放地址：过期前自动换新，同一个源的并发请求只抓一次。"""

    def __init__(self, db: Database, fetcher: Fetcher, store: SettingsStore, metrics: Metrics) -> None:
        self.db = db
        self.fetcher = fetcher
        self.store = store
        self.metrics = metrics
        self.quality = QualityProber(db, fetcher, store.current.quality_concurrency, store.current.quality_pending)
        self.health = HealthTracker(store)
        self._locks = WeakValueDictionary()
        self.sessions = PlaybackSessions(store.current.play_session_entries, store.current.play_session_hours)
        store.on_change(self._on_settings)
        self.selection = PlaybackSelection(self)

    def _on_settings(self, old, new):
        self.sessions.capacity = new.play_session_entries
        self.sessions.hours = new.play_session_hours
        self.quality.configure(new.quality_concurrency, new.quality_pending)

    # ---- 挑源 ----

    def rank(self, sources: list[dict], *, site: str | None = None, direct_only: bool = False,
             remote: bool = False, want: int | None = None) -> list[dict]:
        """可用的源按播放优先顺序排列：字幕偏好 → 不在失败冷却中 →（按连通性挑源时）播放站的连通性
        →（直连优先时）能 302 的 → 画质 → 站点优先级 → 有现成地址。画质优先关掉时，画质排在站点优先级后面。

        字幕不回退（subtitle_fallback 关）时，只留首选里现有的那一档字幕。
        remote：播放器是外网客户端（网关），绑了本服务出口 IP 的直链不算能 302。
        want：要指定档位（/play/{slug}@720p.m3u8）时，有这一档的源排前面，不管「画质优先」开没开。
        """
        s = self.store.current
        t = time.time()
        pref = [SUBTITLE_CODES[x] for x in s.subtitle_priority]
        out = []
        for src in sources:
            st = SITES.get(src["site"])
            if st is None or src["status"] != "active" or not s.site(src["site"]).enabled:
                continue
            if site and src["site"] != site:
                continue
            if direct_only and not self._direct_ok(st):
                continue
            out.append(src)

        def tier(src: dict) -> int:
            return pref.index(src["subtitle"]) if src["subtitle"] in pref else len(pref)

        if out and not s.subtitle_fallback:
            best = min(tier(x) for x in out)
            out = [x for x in out if tier(x) == best]
        health = self.health.source_tier if s.health_rank else lambda _: 0

        def quality(x: dict) -> tuple:
            if want is not None:
                return self.want_key(x, want)
            return self.quality_key(x["height"]) if s.quality_first else ()

        return sorted(out, key=lambda x: (tier(x), source_cooldown(x, s.source_failure_cooldown, s.source_failure_cooldown_max) > t, health(get_site(x["site"])),
                                          s.prefer_direct and not self._can_302(get_site(x["site"]), remote),
                                          quality(x), s.site_rank(x["site"]), not self._fresh(x, 60),
                                          self.quality_key(x["height"])))

    def want_key(self, row: dict, want: int) -> tuple[int, int]:
        """要指定档位时的排序键：有这一档的在前，其次是比它低里最接近的，最后是比它高的。"""
        tiers = ({quality_tier(h) for h in parse_heights(row["heights"])}
                 or {quality_tier(row["height"] or self.store.current.quality_unknown or 720)})
        if want in tiers:
            return 0, 0
        lower = [x for x in tiers if x < want]
        return (1, want - max(lower)) if lower else (2, min(tiers) - want)

    def quality_key(self, height: int | None) -> tuple[bool, int]:
        """画质的排序键：越清楚越靠前；设了上限时，超过上限的排在不超过的后面（超得越多越靠后）。
        还不知道画质的按设置里的默认值算。"""
        s = self.store.current
        h = height or s.quality_unknown
        over = bool(s.quality_max) and h > s.quality_max
        return over, (h if over else -h)

    def _can_302(self, site: Site, remote: bool) -> bool:
        """这个站的源能不能 302 给播放器（多线路站点看有没有这样的线路）；remote 时绑出口 IP 的不算。"""
        if remote:
            return self._direct_ok(site)
        if not site.multi_line:
            return site.stream.direct
        cfg = self.store.current.site(site.name)
        return any(cfg.line(n).enabled and not cfg.line(n).proxy and cfg.line(n).direct_mode != "proxy" and site.line_traits(n, "").direct
                   for n in site.line_specs)

    def _direct_ok(self, site: Site) -> bool:
        """能不能给网关（外网客户端直连）：要能 302、直链不绑本服务的出口 IP；多线路站点看有没有这样的线路。"""
        if not site.multi_line:
            return site.stream.direct and not site.stream.ip_bound
        cfg = self.store.current.site(site.name)
        return any(self._line_direct_ok(site, cfg, name, "") for name in site.line_specs)

    @staticmethod
    def _line_direct_ok(site: Site, cfg, name: str, host: str) -> bool:
        lc = cfg.line(name)
        t = site.line_traits(name, host)
        return lc.enabled and not lc.proxy and lc.direct_mode != "proxy" and t.direct and (not t.ip_bound or lc.direct_mode == "allow")

    def _fresh(self, src: dict, need: int) -> bool:
        if not src["stream_url"]:
            return False
        site = get_site(src["site"])
        traits = site.line_traits(src["line"]) if site.multi_line else site.stream
        if not traits.expires:
            return True
        return (src["stream_expires"] or 0) - time.time() >= need

    def rank_lines(self, site: Site, lines: list[dict], *, want: str | None = None,
                   direct_only: bool = False, remote: bool = False, height: int | None = None) -> list[dict]:
        """一个源的线路按设置排好：启用的、支持的；不在失败冷却中的在前，（按连通性挑源时）播放站连通性好的在前，
        （直连优先时）能 302 的在前，画质优先时再比画质，最后按设置里的线路顺序。"""
        s = self.store.current
        cfg = s.site(site.name)
        t = time.time()
        out = []
        for ln in lines:
            name = ln["line"]
            spec = site.line_specs.get(name)
            if want and name != want:
                continue
            if not cfg.line(name).enabled or (spec is not None and not spec.supported):
                continue
            if direct_only and not self._line_direct_ok(site, cfg, name, ln["host"]):
                continue
            out.append(ln)

        def can_302(ln: dict) -> bool:
            tr = site.line_traits(ln["line"], ln["host"])
            lc = cfg.line(ln["line"])
            return tr.direct and not lc.proxy and lc.direct_mode != "proxy" and not (remote and tr.ip_bound and lc.direct_mode != "allow")

        def health(ln: dict) -> int:
            return self.health.tier(host_key(site, ln)) if s.health_rank else 0

        def quality(ln: dict) -> tuple:
            if height is not None:
                return self.want_key(ln, height)
            return self.quality_key(ln["height"]) if s.quality_first else ()

        return sorted(out, key=lambda ln: (source_cooldown(ln, s.source_failure_cooldown, s.source_failure_cooldown_max) > t, health(ln), s.prefer_direct and not can_302(ln),
                                           quality(ln), cfg.line_rank(ln["line"])))

    def _need(self, v: dict, min_remaining: int | None) -> int:
        if min_remaining is not None:
            return min_remaining
        return (v.get("duration") or DEFAULT_DURATION) + self.store.current.hls_margin * 60

    # ---- 取地址 ----

    async def resolve(self, slug: str, *, min_remaining: int | None = None, site: str | None = None,
                      line: str | None = None, direct_only: bool = False, remote: bool = False,
                      want: int | None = None, relay: bool = False,
                      excluded: frozenset[tuple[int, int]] = frozenset()) -> Resolved:
        """挑一个能用的源并返回它的新鲜地址；依次尝试，全部失败才报错。line 只在指定了 site 时有效。
        want：要的档位，有这一档的源、线路优先。拿到地址后，还不知道画质的在后台顺手探测（只请求 CDN）。"""
        limit = request_deadline.get() or time.monotonic() + self.store.current.resolve_timeout
        dt, tt = request_deadline.set(limit), traffic.set("play")
        try:
            with stage(self.metrics, "play.resolve"):
                async with asyncio.timeout_at(limit):
                    r = await self._resolve(slug, min_remaining=min_remaining, site=site, line=line, direct_only=direct_only,
                                            remote=remote, want=want, relay=relay, excluded=excluded)
        except TimeoutError:
            self.metrics.inc("play_deadline_exceeded")
            raise FetchError("播放解析超过总时间预算") from None
        finally:
            request_deadline.reset(dt)
            traffic.reset(tt)
        if self.store.current.quality_capture and quality_needed(r.line or r.source):
            self.quality.spawn(r.source["id"], r.line["id"] if r.line else None, r.url, r.traits.headers)
        return r

    async def _resolve(self, slug: str, *, min_remaining: int | None, site: str | None, line: str | None,
                       direct_only: bool, remote: bool, want: int | None, relay: bool = False,
                       excluded: frozenset[tuple[int, int]] = frozenset()) -> Resolved:
        v = await self.db.get_video(slug.lower())
        if v is None:
            v = await self.discover(slug.lower())
        sources = await self.db.get_sources(v["id"])
        ranked = self.rank(sources, site=site, direct_only=direct_only, remote=remote, want=want)
        # 先应用字幕偏好，再排除本轮失败候选，不能借排除绕过“字幕不回退”。
        ranked = [src for src in ranked if (src["id"], 0) not in excluded]
        timeout = self.store.current.resolve_timeout
        deadline = request_deadline.get() or time.monotonic() + timeout
        if not ranked:
            if not site and not excluded and (r := await self._discover_for(v, deadline, direct_only=direct_only)):
                return r
            if excluded:
                raise FetchError("没有剩余可用源或线路")
            if direct_only and self.rank(sources, site=site):
                raise NoDirectSource(slug)
            raise NotFound(f"{slug} 没有可用的源")
        errors: list[str] = []
        blocked: Blocked | None = None
        gone = 0
        for i, src in enumerate(ranked):
            left = deadline - time.monotonic()
            if left <= 0:
                errors.append(f"超过 {timeout} 秒，没再试后面的源")
                break
            label = f"{get_site(src['site']).label} {src['key']}"
            wait_state = {}
            wait_token = attempt_wait_state.set(wait_state)
            try:
                attempts = 2 if get_site(src["site"]).multi_line else 1
                need = 60 if relay and not get_site(src["site"]).multi_line else self._need(v, min_remaining)
                r = await asyncio.wait_for(self._ensure(v, src, need, line=line if site else None,
                                                        direct_only=direct_only, remote=remote, height=want,
                                                        excluded=excluded), min(left, self.store.current.resolve_attempt_timeout * attempts))
            except (NotFound, VideoGone) as e:
                await self.db.mark_source_gone(src["site"], src["key"])
                gone += 1
                errors.append(f"{label} 已下架")
                log.info("源 %s 已下架：%s", label, e)
                continue
            except Blocked as e:
                blocked = e
                errors.append(f"{label} 站点拦截中")
                continue
            except CandidateExcluded:
                errors.append(f"{label} 的线路已在本轮排除")
                continue
            except PoolBusy:
                raise
            except TimeoutError:
                errors.append(f"{label}：单次解析超时")
                self.metrics.inc("play_attempt_timeout")
                if not wait_state.get("local_timeout"):
                    await self.db.source_failed(src["id"], "单次解析超时")
                continue
            except (FetchError, ParseError) as e:
                msg = str(e) or "超时"
                if not wait_state.get("local_timeout"):
                    await self.db.source_failed(src["id"], msg)
                errors.append(f"{label}：{msg}")
                log.warning("源 %s 取地址失败：%s", label, msg)
                continue
            finally:
                attempt_wait_state.reset(wait_token)
            if i:
                self.metrics.inc("play_failover")
                log.info("%s 前 %d 个源不可用，改用 %s", slug, i, label)
            return r
        if not site and not excluded and (r := await self._discover_for(v, deadline, direct_only=direct_only)):
            return r
        if gone == len(ranked):
            raise NotFound(f"{slug} 的源都已下架")
        if blocked is not None and len(errors) == 1:
            raise blocked
        raise FetchError("；".join(errors) or "没有剩余可用源或线路")

    async def _discover_for(self, v: dict, deadline: float, *, direct_only: bool = False) -> Resolved | None:
        """现场找源：已知的源都不能用时，按番号到还没有源的站点各找一次，找到就加成源并用它。

        最近查过、没有这部片的站点跳过（probe_recheck_days）；字幕不回退时，找到的源字幕不合偏好也不用。
        """
        s = self.store.current
        if not s.play_discover or not v.get("code_key"):
            return None
        have = {src["site"] for src in await self.db.get_sources(v["id"])}
        cutoff = time.time() - s.probe_recheck_days * 86400
        for name in s.site_priority:
            site = SITES[name]
            if name in have or not s.site(name).enabled or not site.can_lookup:
                continue
            if direct_only and not self._direct_ok(site):
                continue
            check = await self.db.get_source_check(v["id"], name)
            if check and not check["found"] and check["checked_at"] > cutoff:
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                found = await asyncio.wait_for(
                    find_by_code(site, self.fetcher.site(name), v["code"], bool(v["uncensored"]), priority=True), min(left, s.resolve_attempt_timeout))
            except (NotFound, VideoGone):
                found = []  # 搜索页 404 也是这个站没有，不能让整个解析变成「影片不存在」
            except (Blocked, FetchError, ParseError, TimeoutError) as e:
                log.info("现场找源 %s：%s 失败：%s", v["slug"], site.label, e)
                continue
            await self.db.set_source_check(v["id"], name, bool(found))
            for item in found:
                if isinstance(item, SourceDetail):
                    await self.db.upsert_detail(name, item, v["slug"], s.site_rank, video_id=v["id"])
                else:
                    await self.db.upsert_item(name, item, v["slug"], s.site_rank, video_id=v["id"])
            if not found:
                continue
            self.metrics.inc("play_discovered")
            v = await self.db.get_video_by_id(v["id"]) or v
            usable = [src for src in self.rank(await self.db.get_sources(v["id"]), direct_only=direct_only) if src["site"] == name]
            if not usable:
                log.info("现场找源 %s：在 %s 找到了，但字幕不合偏好（字幕不回退），不用", v["slug"], site.label)
                continue
            log.info("现场找源 %s：已知的源都不能用，在 %s 找到 %s", v["slug"], site.label,
                     "、".join(x.key for x in found))
            left = deadline - time.monotonic()
            try:
                if left <= 0:
                    return None
                return await asyncio.wait_for(self._ensure(v, usable[0], self._need(v, None), direct_only=direct_only), min(left, s.resolve_attempt_timeout))
            except (NotFound, VideoGone, Blocked, FetchError, ParseError, TimeoutError) as e:
                log.info("现场找源 %s：%s 的源取地址失败：%s", v["slug"], site.label, e)
        return None

    async def discover(self, slug: str) -> dict:
        """库里没有的影片（比如别的工具生成的 strm）：把 slug 当番号，按站点优先顺序到各站找，找到就入库。"""
        s = self.store.current
        errors: list[Exception] = []
        for name in s.site_priority:
            site = SITES[name]
            if not site.can_lookup or not s.site(name).enabled or (name != "jable" and not s.play_discover):
                continue
            try:
                found = await asyncio.wait_for(find_by_code(site, self.fetcher.site(name), slug, priority=True), s.resolve_attempt_timeout)
            except (Blocked, FetchError, ParseError, TimeoutError) as e:
                errors.append(e)
                log.info("在 %s 找 %s 失败：%s", site.label, slug, e)
                continue
            if not found:
                continue
            item = found[0]
            if isinstance(item, SourceDetail):
                vid = await self.db.upsert_detail(name, item, slug, s.site_rank)
            else:
                vid, _ = await self.db.upsert_item(name, item, slug, s.site_rank)
            log.info("库里没有 %s，在 %s 找到了，已入库", slug, site.label)
            return await self.db.get_video_by_id(vid)
        hard = [e for e in errors if not isinstance(e, (NotFound, VideoGone))]
        if hard:
            if isinstance(hard[0], TimeoutError):
                raise FetchError("找源超时")
            raise hard[0]
        raise NotFound(slug)

    async def _ensure(self, v: dict, src: dict, need: int, stale: str | None = None, *, line: str | None = None,
                       direct_only: bool = False, remote: bool = False, height: int | None = None,
                       excluded: frozenset[tuple[int, int]] = frozenset()) -> Resolved:
        """保证这个源有剩余有效期 ≥ need 的地址；stale 是调用方确认已失效的地址，缓存仍是它时强制刷新。"""
        site = get_site(src["site"])
        if site.multi_line:
            return await self._ensure_lines(v, src, site, need, stale, line, direct_only, remote, height, excluded)
        if stale is None and self._fresh(src, need):
            self.metrics.inc("play_cache_hit")
            return Resolved(v, src, site)
        lock = self._locks.setdefault(src["id"], asyncio.Lock())
        async with measured_lock(self.metrics, lock, "play.refresh_wait"):
            src = await self.db.get_source(src["id"])
            if stale is not None:
                if src["stream_url"] and src["stream_url"] != stale:
                    return Resolved(v, src, site)
            elif self._fresh(src, need):
                self.metrics.inc("play_cache_hit")
                return Resolved(v, src, site)
            try:
                st = await site.fetch_stream(self.fetcher.site(site.name), src["key"])
            except PoolBusy:
                raise
            except (FetchError, ParseError) as e:
                self.health.record(site.name, False, error=str(e))
                raise
            self.health.record(site.name, True)
            if st.detail is not None:
                await self.db.upsert_detail(site.name, st.detail, v["slug"], self.store.current.site_rank)
            await self.db.set_stream(src["id"], st.url, st.expires)
            self.metrics.inc("play_refresh")
            src = await self.db.get_source(src["id"])
            v = await self.db.get_video_by_id(v["id"]) or v
        return Resolved(v, src, site)

    async def _ensure_lines(self, v: dict, src: dict, site: Site, need: int, stale: str | None,
                            want: str | None, direct_only: bool, remote: bool = False,
                            height: int | None = None, excluded: frozenset[tuple[int, int]] = frozenset()) -> Resolved:
        """多线路站点：按顺序找一条有新鲜直链的线路，没有就逐条现取；全失败时重抓一次详情（线路数据可能换了）再试。"""
        lock = self._locks.setdefault(src["id"], asyncio.Lock())
        errors: list[str] = []
        async with measured_lock(self.metrics, lock, "play.refresh_wait"):
            lines = await self.db.get_lines(src["id"])
            refreshed = False
            if not lines:
                await self._refresh_detail(v, src, site)
                refreshed = True
                lines = await self.db.get_lines(src["id"])
            for _ in range(2):
                for ln in self.rank_lines(site, lines, want=want, direct_only=direct_only, remote=remote,
                                          height=height):
                    if (src["id"], ln["id"]) in excluded:
                        continue
                    traits = site.line_traits(ln["line"], ln["host"])
                    fresh = ln["stream_url"] and (not traits.expires or (ln["stream_expires"] or 0) - time.time() >= need)
                    if fresh and ln["stream_url"] != stale:
                        if (src["line"], src["stream_url"]) != (ln["line"], ln["stream_url"]):
                            await self.db.use_line(src["id"], ln)
                        self.metrics.inc("play_cache_hit")
                        return await self._resolved(v, src["id"], site, ln["id"])
                    try:
                        await asyncio.wait_for(self._refresh_line(src, site, ln, use=True), self.store.current.resolve_attempt_timeout)
                    except PoolBusy:
                        raise
                    except (FetchError, ParseError, NotFound, ValueError, KeyError, TimeoutError) as e:
                        errors.append(f"{ln['line']}：{e}")
                        log.info("%s %s 线路 %s 取直链失败：%s", site.label, src["key"], ln["line"], e)
                        continue
                    self.metrics.inc("play_refresh")
                    return await self._resolved(v, src["id"], site, ln["id"])
                if refreshed or any(sid == src["id"] for sid, _ in excluded):
                    break
                await self._refresh_detail(v, src, site)
                refreshed = True
                lines = await self.db.get_lines(src["id"])
        if not errors and any(sid == src["id"] for sid, _ in excluded):
            raise CandidateExcluded("本轮已排除该源的可用线路")
        raise FetchError("；".join(errors) or "没有可用的线路（都停用了，或者都不支持）")

    async def _refresh_line(self, src: dict, site: Site, ln: dict, *, use: bool) -> None:
        """现取这条线路的直链，记下直链、画质和播放站的连通性；失败时线路进冷却。use：设成源当前在用的线路。"""
        try:
            hs = await site.resolve_line(self.fetcher, ln["line"], ln["link"])
        except PoolBusy:
            raise
        except (FetchError, ParseError, NotFound, ValueError, KeyError) as e:
            await self.db.line_failed(ln["id"], str(e))
            if not isinstance(e, NotFound):
                self.health.record(host_key(site, ln), False, error=str(e))
            raise
        self.health.record(host_key(site, {"line": ln["line"], "host": hs.host}), True)
        await self.db.set_line_stream(ln["id"], hs.url, hs.expires, hs.host, hs.referer, use=use)
        if hs.quality is not None:
            await self.quality.save(src["id"], ln["id"], hs.quality)

    async def line_stream(self, v: dict, src: dict, ln: dict, need: int = 60) -> Resolved:
        """指定线路的新鲜地址（探测画质、检测连通性用）：有没过期的现成地址就不访问播放站；
        不切换源当前在用的线路（正在中转的播放只认当前线路）。"""
        site = get_site(src["site"])
        traits = site.line_traits(ln["line"], ln["host"])
        if not (ln["stream_url"] and (not traits.expires or (ln["stream_expires"] or 0) - time.time() >= need)):
            await self._refresh_line(src, site, ln, use=False)
            ln = next(x for x in await self.db.get_lines(src["id"]) if x["id"] == ln["id"])
        lc = self.store.current.site(site.name).line(ln["line"])
        return Resolved(v, src, site, ln, lc.proxy or lc.direct_mode == "proxy", lc.direct_mode == "allow")

    async def _refresh_detail(self, v: dict, src: dict, site: Site) -> None:
        d = await site.fetch_detail(self.fetcher.site(site.name), src["key"], priority=True)
        await self.db.upsert_detail(site.name, d, v["slug"], self.store.current.site_rank)

    async def _resolved(self, v: dict, source_id: int, site: Site, line_id: int) -> Resolved:
        src = await self.db.get_source(source_id)
        line = next(ln for ln in await self.db.get_lines(source_id) if ln["id"] == line_id)
        lc = self.store.current.site(site.name).line(line["line"])
        return Resolved(await self.db.get_video_by_id(v["id"]) or v, src, site, line,
                        lc.proxy or lc.direct_mode == "proxy", lc.direct_mode == "allow")

    async def ensure_source(self, source_id: int, *, min_remaining: int = 60, stale: str | None = None,
                            line_id: int | None = None) -> Resolved:
        """中转分片时用：只认这一个源、这条线路（不同站点、不同线路切片不一样，播到一半不能换）。"""
        src = await self.db.get_source(source_id)
        if src is None:
            raise NotFound(f"源 #{source_id}")
        v = await self.db.get_video_by_id(src["video_id"])
        if v is None:
            raise NotFound(f"源 #{source_id} 的作品")
        if line_id is not None:
            lock = self._locks.setdefault(source_id, asyncio.Lock())
            async with measured_lock(self.metrics, lock, "play.refresh_wait"):
                ln = next((x for x in await self.db.get_lines(source_id) if x["id"] == line_id), None)
                if ln is None:
                    raise NotFound("播放线路已不存在")
                site = get_site(src["site"])
                traits = site.line_traits(ln["line"], ln["host"])
                fresh = ln["stream_url"] and (not traits.expires or (ln["stream_expires"] or 0)-time.time() >= min_remaining)
                if not fresh or (stale and ln["stream_url"] == stale):
                    await self._refresh_line(src, site, ln, use=False)
                return await self._resolved(v, source_id, site, line_id)
        return await self._ensure(v, src, min_remaining, stale, line=src["line"] or None)

    async def session_source(self, source_id: int, session: str, stale: str | None = None) -> Resolved:
        r = await self.sessions.restore(self.db, session, source_id)
        if stale is None and (not r.traits.expires or (r.expires or 0)-time.time() >= 60):
            return r
        refreshed = await self.ensure_source(source_id, stale=stale, line_id=r.line["id"] if r.line else None)
        self.sessions.update(session, refreshed)
        return refreshed


router = APIRouter()


def _ctx(request: Request):
    return request.app.state.ctx


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "Content-Length, Content-Type",
}


def _check_token(request: Request, t: str) -> None:
    token = _ctx(request).store.current.play_token
    if token and not secrets.compare_digest(t.encode(), token.encode()):
        raise HTTPException(403, "播放令牌错误")


def direct_blocker(proxy_user_agents: list[str], ua: str, origin: str, fetch_mode: str = "") -> str:
    """客户端不能直连 CDN 的原因；能直连返回空串。

    CDN 拒绝 UA 含 Lavf / python-requests 的请求，也不返回 CORS 头：浏览器里 hls.js 这类脚本请求
    （Sec-Fetch-Mode: cors）被 302 到 CDN 后会跨域失败；同源请求不带 Origin，所以要看 Sec-Fetch-Mode。
    """
    for p in proxy_user_agents:
        if p and p in ua:
            return f"客户端 UA 含 {p}，CDN 会拒绝"
    if origin:
        return "浏览器跨域请求，CDN 不返回 CORS 头"
    if fetch_mode.lower() == "cors":
        return "浏览器脚本请求（Sec-Fetch-Mode: cors），302 到 CDN 后会跨域失败"
    return ""


def _left(r: Resolved) -> str:
    if not r.traits.expires:
        return "地址长期有效"
    if not r.expires:
        return "地址有效期未知"  # 地址格式不认识，取不到过期时间
    return f"地址剩余 {int(r.expires - time.time()) // 60} 分钟"


def _label(r: Resolved) -> str:
    row = r.line or r.source
    q = f"，{quality_label(row['height'])}" if row["height"] else ""
    return f"{r.site.label} {r.source['key']}" + (f" 线路 {r.line['line']}" if r.line else "") + q


async def _resolve_or_http(request: Request, slug: str, **kw) -> Resolved:
    try:
        return await _ctx(request).resolver.selection.choose(slug, **kw)
    except (NotFound, VideoGone):
        raise HTTPException(404, f"影片不存在或已下架：{slug}") from None
    except Blocked as e:
        raise HTTPException(503, f"站点拦截中：{e}", headers={"Retry-After": str(int(e.retry_after))}) from None
    except PoolBusy as e:
        raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
    except (FetchError, ParseError) as e:
        log.warning("解析播放地址失败 %s：%s", slug, e)
        raise HTTPException(502, f"解析播放地址失败：{e}") from None


def _parse_name(name: str) -> tuple[str, int | None]:
    """'ipzz-983.m3u8' → ('ipzz-983', None)；'ipzz-983@720p.m3u8' → ('ipzz-983', 720)。认不出的 404。"""
    slug, _, version = name.lower().removesuffix(".m3u8").partition("@")
    want = parse_label(version) if version else None
    if not SLUG_RE.fullmatch(slug) or (version and want is None):
        raise HTTPException(404, f"无法识别的影片：{name}")
    return slug, want


def _variant_wanted(ctx, r: Resolved, want: int | None) -> bool:
    """要不要从多档的主播放列表里挑一档：指定了档位，或者设置是只给最高一档；源知道自己有不止一档才挑。"""
    if want is None and ctx.store.current.variant_mode != "highest":
        return False
    row = r.line or r.source
    return _is_hls(r.url) and len({quality_tier(h) for h in parse_heights(row["heights"])}) > 1


async def _pick_variant(ctx, r: Resolved, want: int | None) -> str:
    """302 用：多档的主播放列表挑出要的那一档（没指定就最高档）的子清单地址；不用挑、读不到的给主播放列表地址。"""
    if not _variant_wanted(ctx, r, want):
        return r.url
    try:
        text = (await _playlist(ctx, r.url, r.traits.headers)).decode("utf-8", "replace")
    except (FetchError, NotFound):
        return r.url
    picked = filter_master(text, want)
    return urljoin(r.url, picked[1]) if picked and picked[1] else r.url


@router.api_route("/play/{name}", methods=["GET", "HEAD"])
async def play(name: str, request: Request, t: str = "", proxy: int = 0, src: str = "", line: str = "",
               variants: str = "", prepare: bool = False, skip: str = "", continuous: bool = False):
    """proxy=1 强制中转（网页试播用）；src=站点 只用这个站点的源，line=线路 再限定线路。
    variants=all 多码率的主播放列表整个给（网页试播要切画质），不管设置里的「只给最高一档」。
    {slug}@720p.m3u8 要指定档位（多画质版本的 strm 用）：有这一档的源优先，多档的主播放列表只给这一档。
    浏览器跨域请求（带 Origin）也走中转并返回 CORS 头。"""
    ctx = _ctx(request)
    _check_token(request, t)
    slug, want = _parse_name(name)
    ctx.metrics.inc("play_requests")
    s = ctx.store.current
    ua = request.headers.get("user-agent", "")
    origin = request.headers.get("origin", "")
    fetch_mode = request.headers.get("sec-fetch-mode", "")
    excluded = set()
    if skip:
        parts = skip.split(",")
        if len(parts) > 64 or any(not re.fullmatch(r"\d+:\d+", part) for part in parts):
            raise HTTPException(422, "无效的已尝试源列表")
        excluded = {tuple(map(int, part.split(":"))) for part in parts}
    forced = prepare or bool(proxy) or continuous or s.play_mode == "proxy" or bool(direct_blocker(s.proxy_user_agents, ua, origin, fetch_mode))
    r = await _resolve_or_http(request, slug, site=src or None, line=line or None, want=want,
                                relay=forced, remote=s.play_remote, excluded=frozenset(excluded))
    if prepare:
        return JSONResponse({**playback_source(r), "url": await _media_location(request, r, want)},
                            headers={"Cache-Control": "no-store"})
    blocked_uas = s.proxy_user_agents if r.traits.ua_block else []
    proxied = (bool(proxy) or continuous or s.play_mode == "proxy" or not r.traits.direct or (s.play_remote and r.traits.ip_bound)
               or bool(direct_blocker(blocked_uas, ua, origin, fetch_mode)))
    log.info("播放 %s%s：%s %s（%s，客户端 %s，UA %s）", slug, f"（要 {quality_label(want)}）" if want else "", _label(r),
             "中转" if proxied else "302", _left(r), request.client.host if request.client else "?", ua[:60])
    if proxied:
        ctx.metrics.inc("play_proxy")
        resp = await (_proxy_playlist(request, r, t, want, all_variants=variants == "all") if _is_hls(r.url)
                      else _proxy_file(request, r))
    else:
        ctx.metrics.inc("play_redirect")
        resp = RedirectResponse(await _pick_variant(ctx, r, want), status_code=302)
    if origin:
        resp.headers.update(CORS_HEADERS)
    return resp


@router.options("/play/{name}")
@router.options("/hls/{source_id}/{path:path}")
async def cors_preflight():
    return Response(status_code=204, headers=CORS_HEADERS)


@router.get("/api/resolve/{name:path}")
async def resolve_for_gateway(name: str, request: Request, ua: str = "", origin: str = "", fetch_mode: str = "",
                              min_remaining: int | None = None, mode: str = "", relay: str = ""):
    """给 embyGateway 的 http_resolver 后端用：返回客户端（外网）能播的地址，由网关 302。

    mode（不带用设置里的 resolve_mode）：
      auto      按挑源偏好（画质优先 / 直连优先）挑：挑中的能直连给 CDN 地址，要中转给中转地址
      redirect  只挑能直连的源（比如 Jable）；作品只有要中转的源时给中转地址
      proxy     一律给中转地址，流量走本服务
    中转地址：relay=gateway（网关能转发）时是本服务的地址，网关只取路径、经它转发（能用上网关的统计）；
    否则是公网中转地址（resolve_proxy_url）下的，客户端直接连本服务。两者都没有时，auto 退回只挑能直连的，
    只有要中转的源返回 409。返回里的 relay 说明给的是不是中转地址。
    能直连的源这个客户端连不了（浏览器跨域请求、CDN 拒绝的 UA，和 /play 的判断一样），有中转就给中转。
    直连的源都取地址失败、或者站点拦截中，有中转就给中转（中转时还能用别的源），不报错：
    网关遇到错误会回退去反代 Emby，Emby 再 302 到 strm 里的内网地址，外网客户端访问不到。
    name 可以是 slug、slug.m3u8，也可以是网关 objectKey 原样（如 play/ipzz-983.m3u8），取最后一段；
    多画质版本的 strm 是 ipzz-983@720p.m3u8，挑有这一档的源，多档的主播放列表给这一档的子清单。
    """
    ctx = _ctx(request)
    s = ctx.store.current
    if not s.resolve_token:
        raise HTTPException(403, "未设置 resolve_token，接口未开放")
    auth = request.headers.get("authorization", "")
    given = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else request.query_params.get("token", "")
    if not secrets.compare_digest(given.encode(), s.resolve_token.encode()):
        raise HTTPException(401, "resolve_token 错误")
    slug, want = _parse_name(name.rsplit("/", 1)[-1])
    mode = mode or s.resolve_mode
    if mode == "continuous":  # 兼容旧网关请求，只做原样中转。
        mode = "proxy"
    if mode not in ("auto", "redirect", "strict_redirect", "proxy"):
        raise HTTPException(400, f"mode 只能是 auto、redirect、strict_redirect、proxy：{mode}")
    ctx.metrics.inc("resolve_requests")
    extra = f"，UA {ua[:60]}" + (f"，fetch_mode {fetch_mode}" if fetch_mode else "")
    base = str(request.base_url).rstrip("/") if relay == "gateway" else s.resolve_proxy_url
    relay_url = _relay_url(s, base, slug, want) if base else ""

    def relayed(why: str, duration: int | None = None, bound: bool = False) -> dict:
        log.info("resolve %s：%s，返回%s中转地址（%s%s）", slug, why, "经网关转发的" if relay == "gateway" else "公网",
                 mode, extra)
        ttl = min(s.resolve_relay_ttl, (duration or 0)+3600) if bound else s.resolve_relay_ttl
        return {"slug": slug, "url": relay_url, "relay": True, "expires_at": 0, "ttl": ttl, "duration": duration}

    if mode == "proxy":
        if not relay_url:
            raise HTTPException(409, "mode=proxy 要先设置公网中转地址，或者网关开经网关中转")
        return relayed("要求原样中转")
    resolver = ctx.resolver
    try:
        if mode == "auto" and relay_url:
            r = await resolver.selection.choose(slug, min_remaining=min_remaining, remote=True, want=want)
            if not (r.traits.direct and not r.traits.ip_bound):
                location = await _bound_relay_url(ctx, s, base, r, want)
                return {**relayed(f"挑中 {_label(r)} 要中转", r.video.get("duration"), bound=True), "url": location,
                        "source": playback_source(r)}
        else:
            r = await resolver.selection.choose(slug, min_remaining=min_remaining, direct_only=True, remote=True, want=want)
    except NoDirectSource:
        if mode == "strict_redirect":
            raise HTTPException(409, "这部影片没有可供外部客户端直连的源，已禁止中转回退") from None
        if not relay_url:
            log.info("resolve %s：只有要中转的源，没有中转地址，返回 409（%s）", slug, extra.lstrip("，"))
            raise HTTPException(409, "这部影片只有需要本服务中转的源，没有设置公网中转地址") from None
        return relayed("只有要中转的源")
    except (NotFound, VideoGone):
        raise HTTPException(404, f"影片不存在或已下架：{slug}") from None
    except PoolBusy as e:
        raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
    except (Blocked, FetchError, ParseError) as e:
        if relay_url and mode != "strict_redirect":
            return relayed(f"能直连的源取地址失败（{e}）")
        if isinstance(e, Blocked):
            raise HTTPException(503, f"站点拦截中：{e}", headers={"Retry-After": str(int(e.retry_after))}) from None
        log.warning("解析播放地址失败 %s：%s", slug, e)
        raise HTTPException(502, f"解析播放地址失败：{e}") from None
    blocked_uas = s.proxy_user_agents if r.traits.ua_block else []
    if why := direct_blocker(blocked_uas, ua, origin, fetch_mode):
        if mode == "strict_redirect":
            raise HTTPException(409, f"只允许直连，但当前客户端需要中转：{why}")
        if relay_url:
            location = await _bound_relay_url(ctx, s, base, r, want)
            return {**relayed(f"{_label(r)} 能直连，但{why}", r.video.get("duration"), bound=True), "url": location,
                    "source": playback_source(r)}
    expires = r.expires or 0
    log.info("resolve %s：返回 %s 的 CDN 地址（%s，%s%s）", slug, _label(r), _left(r), mode, extra)
    return {"slug": slug, "url": await _pick_variant(ctx, r, want), "relay": False, "expires_at": expires,
            "source": playback_source(r), "state": "redirect_issued",
            "ttl": max(0, int(expires - time.time())), "duration": r.video.get("duration")}


def _relay_url(s, base: str, slug: str, want: int | None = None) -> str:
    url = f"{base}/play/{slug}{'@' + quality_label(want).lower() if want else ''}.m3u8?proxy=1"
    return url + (f"&t={quote(s.play_token)}" if s.play_token else "")


async def _bound_relay_url(ctx, settings, base, resolved, want):
    session = await ctx.resolver.sessions.persist(ctx.db, resolved)
    query = {"s": session}
    if settings.play_token:
        query["t"] = settings.play_token
    if want is not None:
        query["want"] = want
    return f"{base}/media/{resolved.source['id']}?{urlencode(query)}"


# ---- 中转 ----


def _sign(url: str) -> str:
    return hmac.new(_SIGN_KEY, url.encode(), hashlib.sha256).hexdigest()[:16]


def _encode_x(url: str) -> str:
    return f"_x/{_sign(url)}/" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _decode_x(path: str) -> str | None:
    _, sig, data = path.split("/", 2)
    data = data.split(".", 1)[0]  # 去掉为播放器加的扩展名（base64url 里没有点）
    try:
        url = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode()
    except ValueError:
        return None
    return url if hmac.compare_digest(sig, _sign(url)) and url.startswith(("http://", "https://")) else None


def rewrite_playlist(text: str, playlist_url: str, root: str, up: str, q: str, disguised: bool) -> str:
    """把播放列表里的地址改成本服务的相对地址。

    playlist_url：这个播放列表在上游的地址；root：源的播放地址所在目录，它下面的文件按相对路径中转；
    up：从当前播放列表的位置回到 /hls/{源 id}/ 的相对前缀；q：附加的查询串（播放令牌）。
    """

    def local(ref: str, segment: bool) -> str:
        absolute = urljoin(playlist_url, ref.strip())
        rel = absolute[len(root):] if absolute.startswith(root) and "?" not in absolute else ""
        if not rel or not HLS_PATH_RE.fullmatch(rel) or ".." in rel.split("/"):
            # 不在源目录下（或带查询串）的地址：签名后中转，并带上扩展名让播放器认得出是清单还是分片
            path = absolute.split("?", 1)[0].lower()
            m = _EXT_RE.search(path.rsplit("/", 1)[-1])
            ext = ".m3u8" if path.endswith(".m3u8") else ".ts" if segment else (m.group(0) if m else "")
            rel = _encode_x(absolute) + ext
        if segment and disguised and rel.lower().endswith(DISGUISE_EXTS):
            rel += ".ts"  # 伪装成图片的 TS 分片：新版 ffmpeg 会按扩展名拒收，改名后再中转
        return up + rel + q

    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = local(line, segment=not line.split("?", 1)[0].endswith(".m3u8"))
        elif 'URI="' in line:
            line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{local(m.group(1), segment=False)}"', line)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _upstream(r: Resolved, path: str) -> str | None:
    if path.startswith("_x/"):
        return _decode_x(path)
    if r.traits.disguised_segments and path.endswith(".ts") and path[:-3].lower().endswith(DISGUISE_EXTS):
        path = path[:-3]
    return r.url.rsplit("/", 1)[0] + "/" + path


def _cdn_error(request: Request, r: Resolved, msg: str) -> HTTPException:
    """中转时 CDN 出错：记进这个播放站的连通性，返回给播放器 502。"""
    _ctx(request).resolver.health.record(r.host, False, error=msg)
    return HTTPException(502, msg)


def _relay_aborted(request: Request, r: Resolved, what: str, sent: int, e: Exception) -> RelayAborted:
    """中转传到一半 CDN 断开：记一行日志、记进连通性，返回的异常抛出去让 uvicorn 断开连接（播放器会重试这一段）。"""
    _ctx(request).resolver.health.record(r.host, False, error=f"传到一半断开：{e}")
    line = f" 线路 {r.line['line']}" if r.line else ""
    log.warning("中转 %s：%s%s 的 %s 传到 %d KB 时 CDN 断开：%s", r.video["slug"], r.site.label, line, what,
                sent // 1024, e)
    return RelayAborted(str(e))


def _is_hls(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".m3u8")


async def _playlist(ctx, url: str, headers: dict | None):
    with stage(ctx.metrics, "play.playlist"):
        return await getattr(ctx.fetcher, "get_playlist", ctx.fetcher.get_bytes)(url, headers=headers)


async def _media_location(request: Request, r: Resolved, want: int | None = None) -> str:
    """类型探测与实际播放复用同一快照，避免再次选源后媒体格式改变。"""
    ctx = _ctx(request)
    session = await ctx.resolver.sessions.persist(ctx.db, r)
    query = {"s": session}
    for key in ("t", "variants"):
        if value := request.query_params.get(key):
            query[key] = value
    if want is not None:
        query["want"] = str(want)
    return f"../media/{r.source['id']}?{urlencode(query)}"


def playback_source(r: Resolved) -> dict:
    from .health import host_label
    return {"source_id": r.source["id"], "line_id": r.line["id"] if r.line else 0,
            "site": r.site.name, "label": r.site.label, "line": r.line["line"] if r.line else "",
            "host": host_label(r.host), "media_type": "hls" if _is_hls(r.url) else "file",
            "height": (r.line or r.source).get("height") or 0,
            "duration": r.video.get("duration") or 0}


async def _proxy_file(request: Request, r: Resolved) -> Response:
    """直链是 mp4 这类单文件（如 streamtape）：原样转发，带上 Range，支持拖动。"""
    ctx = _ctx(request)
    headers = dict(r.traits.headers)
    if rng := request.headers.get("range"):
        headers["Range"] = rng
    try:
        resp = await getattr(ctx.fetcher, "file_session", ctx.fetcher.session).get(r.url, stream=True, headers=headers,
                                             timeout=ctx.store.current.request_timeout)
    except PoolBusy as e:
        raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
    except Exception as e:
        raise _cdn_error(request, r, f"请求直链失败：{e}") from None
    if resp.status_code not in (200, 206):
        await close_stream(resp)
        raise _cdn_error(request, r, f"直链返回 HTTP {resp.status_code}")
    ctx.resolver.health.record(r.host, True)
    out = {k: v for k in ("content-length", "content-range", "accept-ranges")
           if (v := resp.headers.get(k))}
    if request.method == "HEAD":
        await close_stream(resp)
        out["Content-Location"] = await _media_location(request, r)
        return Response(status_code=resp.status_code, headers=out,
                        media_type=resp.headers.get("content-type") or "video/mp4")

    async def body():
        sent = 0
        try:
            async for chunk in resp.aiter_content():
                sent += len(chunk)
                ctx.metrics.inc("relay_bytes", len(chunk))
                yield chunk
        except CurlError as e:
            ctx.metrics.inc("relay_aborted")
            raise _relay_aborted(request, r, "视频文件", sent, e) from None
        except (asyncio.CancelledError, GeneratorExit):
            ctx.metrics.inc("relay_client_disconnect")
            raise
        finally:
            await close_stream(resp)

    return StreamingResponse(body(), status_code=resp.status_code, headers=out,
                             media_type=resp.headers.get("content-type") or "video/mp4")


@router.api_route("/media/{source_id}", methods=["GET", "HEAD"])
async def media_file(source_id: int, request: Request, s: str, t: str = "", want: int | None = None,
                     variants: str = ""):
    """网页播放：清单、Range/拖动保持源、线路和地址快照一致。"""
    _check_token(request, t)
    try:
        r = await _ctx(request).resolver.session_source(source_id, s)
    except (NotFound, VideoGone):
        raise HTTPException(404, "播放会话不存在或已失效，请重新试播") from None
    except PoolBusy as e:
        raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
    except (Blocked, FetchError, ParseError) as e:
        raise HTTPException(502, f"刷新播放地址失败：{e}") from None
    if _is_hls(r.url):
        return await _proxy_playlist(request, r, t, want, all_variants=variants == "all")
    return await _proxy_file(request, r)


async def _proxy_playlist(request: Request, r: Resolved, t: str, want: int | None = None,
                          all_variants: bool = False) -> Response:
    ctx = _ctx(request)
    try:
        raw = await _playlist(ctx, r.url, r.traits.headers)
    except PoolBusy as e:
        raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
    except (FetchError, NotFound) as e:
        raise _cdn_error(request, r, f"获取播放列表失败：{e}") from None
    required = ctx.resolver._need(r.video, None)
    root = r.url.rsplit("/", 1)[0] + "/"
    # 带独立签名/跨目录的分片不能可靠地替换根地址，继续要求完整影片有效期。
    refs = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip() and not ln.startswith("#")]
    refs += re.findall(r'URI="([^"]+)"', raw.decode("utf-8", "replace"))
    unsafe = any("?" in urljoin(r.url, ref) or not urljoin(r.url, ref).startswith(root) for ref in refs)
    if not r.site.multi_line and r.traits.expires and (r.expires or 0)-time.time() < required and unsafe:
        r = await _resolve_or_http(request, r.video["slug"], min_remaining=required,
                                   site=request.query_params.get("src") or None, line=request.query_params.get("line") or None, want=want)
        if not _is_hls(r.url):
            return await _proxy_file(request, r)
        try:
            raw = await _playlist(ctx, r.url, r.traits.headers)
        except PoolBusy as error:
            raise HTTPException(503, str(error), headers={"Retry-After": "1"}) from None
        except (FetchError, NotFound) as error:
            raise _cdn_error(request, r, f"获取新播放列表失败：{error}") from None
    ctx.resolver.health.record(r.host, True)
    if request.method == "HEAD":
        return Response(media_type="application/vnd.apple.mpegurl",
                        headers={"Content-Location": await _media_location(request, r, want)})
    session = await ctx.resolver.sessions.persist(ctx.db, r)
    q = f"?s={session}" + (f"&t={quote(t)}" if t else "")
    root = r.url.rsplit("/", 1)[0] + "/"
    body = raw.decode("utf-8", "replace")
    row = r.line or r.source
    if (found := from_master(body)) is not None and row["heights"] != ",".join(map(str, found.heights)):
        # 元数据/版本文件更新不阻塞清单返回。
        ctx.resolver.quality.spawn(r.source["id"], r.line["id"] if r.line else None, r.url, r.traits.headers)
    highest = ((ctx.store.current.variant_mode == "highest" and not all_variants)
               or request.query_params.get("variants") == "highest")
    if (want is not None or highest) and (picked := filter_master(body, want)):
        body = picked[0]  # 只给要的那一档（没指定就最高档）
    # /play/{slug}.m3u8 回到 /hls/{源 id}/：相对地址解析，不依赖对外地址的写法
    text = rewrite_playlist(body, r.url, root, f"../hls/{r.source['id']}/", q, r.traits.disguised_segments)
    return Response(text, media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{source_id}/{path:path}")
async def hls_file(source_id: int, path: str, request: Request, t: str = "", s: str = ""):
    """中转模式：转发子清单、分片和 key；遇到 403 自动换新地址重试一次。"""
    ctx = _ctx(request)
    _check_token(request, t)
    if not HLS_PATH_RE.fullmatch(path) or ".." in path.split("/"):
        raise HTTPException(404)
    try:
        r = await (ctx.resolver.session_source(source_id, s) if s else ctx.resolver.ensure_source(source_id))
    except (NotFound, VideoGone):
        raise HTTPException(404, "源不存在或已下架") from None
    except Blocked as e:
        raise HTTPException(503, f"站点拦截中：{e}", headers={"Retry-After": str(int(e.retry_after))}) from None
    except PoolBusy as e:
        raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
    except (FetchError, ParseError) as e:
        raise HTTPException(502, f"解析播放地址失败：{e}") from None
    headers_up = r.traits.headers or None
    for attempt in range(2):
        url = _upstream(r, path)
        if url is None:
            raise HTTPException(404)
        try:
            resp = await getattr(ctx.fetcher, "hls_session", ctx.fetcher.session).get(url, stream=True, timeout=ctx.store.current.request_timeout,
                                                 headers=headers_up)
        except PoolBusy as e:
            raise HTTPException(503, str(e), headers={"Retry-After": "1"}) from None
        except Exception as e:
            raise _cdn_error(request, r, f"CDN 请求失败：{e}") from None
        if resp.status_code in (403, 410) and attempt == 0 and not path.startswith("_x/"):
            await close_stream(resp)
            ctx.metrics.inc("play_token_expired")
            try:
                r = await (ctx.resolver.session_source(source_id, s, stale=r.url) if s else ctx.resolver.ensure_source(source_id, stale=r.url))
            except (NotFound, VideoGone, Blocked, FetchError, ParseError) as e:
                raise HTTPException(502, f"CDN 地址失效，换新失败：{e}") from None
            continue
        if resp.status_code != 200:
            await close_stream(resp)
            raise _cdn_error(request, r, f"CDN 返回 HTTP {resp.status_code}")
        ctx.resolver.health.record(r.host, True)
        cors = dict(CORS_HEADERS) if request.headers.get("origin") else {}
        if path.endswith(".m3u8"):
            try:
                body = b"".join([chunk async for chunk in resp.aiter_content()])
            finally:
                await close_stream(resp)
            q = (f"?s={s}" + (f"&t={quote(t)}" if t else "")) if s else (f"?t={quote(t)}" if t else "")
            up = "../" * path.count("/")  # 从当前子清单的位置回到 /hls/{源 id}/
            root = r.url.rsplit("/", 1)[0] + "/"
            text = rewrite_playlist(body.decode("utf-8", "replace"), url, root, up, q, r.traits.disguised_segments)
            return Response(text, media_type="application/vnd.apple.mpegurl", headers=cors)

        strip = r.traits.fake_header

        async def stream(resp=resp, r=r):
            sent = 0
            try:
                head = b""
                async for chunk in resp.aiter_content():
                    sent += len(chunk)
                    if strip and head is not None:
                        head += chunk  # 攒够一段再找 TS 起点，剥掉前面的假文件头
                        if len(head) < 8192 + 188 * 5:
                            continue
                        chunk, head = head[max(ts_start(head), 0):], None
                    ctx.metrics.inc("relay_bytes", len(chunk))
                    yield chunk
                if strip and head:
                    data = head[max(ts_start(head), 0):]
                    ctx.metrics.inc("relay_bytes", len(data))
                    yield data
            except CurlError as e:
                ctx.metrics.inc("relay_aborted")
                raise _relay_aborted(request, r, path, sent, e) from None
            except (asyncio.CancelledError, GeneratorExit):
                ctx.metrics.inc("relay_client_disconnect")
                raise
            finally:
                await close_stream(resp)

        headers = cors
        if (cl := resp.headers.get("content-length")) and not strip:
            headers["Content-Length"] = cl
        media = resp.headers.get("content-type") or "video/mp2t"
        if path.endswith(".ts"):
            media = "video/mp2t"
        return StreamingResponse(stream(), media_type=media, headers=headers)
    raise HTTPException(502, "CDN 地址刷新后仍不可用")
