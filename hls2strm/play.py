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
from urllib.parse import quote, urljoin

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from .config import SettingsStore
from .db import Database, source_cooldown
from .fetcher import Blocked, FetchError, Fetcher, NotFound
from .observability import Metrics
from .parser import ParseError, VideoGone
from .sites.hosts import ts_start
from .sites import SITES, Site, SourceDetail, StreamTraits, find_by_code, get_site

log = logging.getLogger(__name__)

DEFAULT_DURATION = 2 * 3600
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
HLS_PATH_RE = re.compile(
    r"^(?:_x/[0-9a-f]{16}/[A-Za-z0-9_-]+(?:\.[a-z0-9]{1,5})?|[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+){0,5})$")
_EXT_RE = re.compile(r"\.([a-z0-9]{1,5})$")
DISGUISE_EXTS = (".jpeg", ".jpg", ".png", ".gif", ".webp", ".html", ".txt", ".js", ".css", ".woff2", ".woff")
SUBTITLE_CODES = {"zh": "zh", "none": "", "en": "en"}
_SIGN_KEY = secrets.token_bytes(16)  # _x/ 地址的签名密钥，进程重启后旧地址失效（播放器会重新请求 /play）


class NoDirectSource(Exception):
    """作品有源，但没有播放器能直连的（都要中转）。"""


@dataclass
class Resolved:
    video: dict
    source: dict
    site: Site
    line: dict | None = None  # 多线路站点：这次用的线路
    proxy_forced: bool = False  # 线路设置了强制中转

    @property
    def traits(self) -> StreamTraits:
        if self.line is None:
            return self.site.stream
        t = self.site.line_traits(self.line["line"], self.line["host"])
        if self.line.get("referer"):
            t = replace(t, headers={**t.headers, "Referer": self.line["referer"]})
        return replace(t, direct=False) if self.proxy_forced else t

    @property
    def url(self) -> str:
        return self.source["stream_url"]

    @property
    def expires(self) -> int | None:
        return self.source["stream_expires"]


class Resolver:
    """按作品挑源、维护各源的播放地址：过期前自动换新，同一个源的并发请求只抓一次。"""

    def __init__(self, db: Database, fetcher: Fetcher, store: SettingsStore, metrics: Metrics) -> None:
        self.db = db
        self.fetcher = fetcher
        self.store = store
        self.metrics = metrics
        self._locks: dict[int, asyncio.Lock] = {}

    # ---- 挑源 ----

    def rank(self, sources: list[dict], *, site: str | None = None, direct_only: bool = False) -> list[dict]:
        """可用的源按播放优先顺序排列：字幕偏好 → 不在失败冷却中 → 站点优先级 → 有现成地址 → 分辨率。

        字幕不回退（subtitle_fallback 关）时，只留首选里现有的那一档字幕。
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
        return sorted(out, key=lambda x: (tier(x), source_cooldown(x) > t, s.site_rank(x["site"]),
                                          not self._fresh(x, 60), -(x["height"] or 0)))

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
        return lc.enabled and not lc.proxy and t.direct and not t.ip_bound

    def _fresh(self, src: dict, need: int) -> bool:
        if not src["stream_url"]:
            return False
        site = get_site(src["site"])
        traits = site.line_traits(src["line"]) if site.multi_line else site.stream
        if not traits.expires:
            return True
        return (src["stream_expires"] or 0) - time.time() >= need

    def rank_lines(self, site: Site, lines: list[dict], *, want: str | None = None,
                   direct_only: bool = False) -> list[dict]:
        """一个源的线路按设置排好：启用的、支持的；不在失败冷却中的在前，再按设置里的线路顺序。"""
        cfg = self.store.current.site(site.name)
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
        return sorted(out, key=lambda ln: (source_cooldown(ln) > t, cfg.line_rank(ln["line"])))

    def _need(self, v: dict, min_remaining: int | None) -> int:
        if min_remaining is not None:
            return min_remaining
        return (v.get("duration") or DEFAULT_DURATION) + self.store.current.hls_margin * 60

    # ---- 取地址 ----

    async def resolve(self, slug: str, *, min_remaining: int | None = None, site: str | None = None,
                      line: str | None = None, direct_only: bool = False) -> Resolved:
        """挑一个能用的源并返回它的新鲜地址；依次尝试，全部失败才报错。line 只在指定了 site 时有效。"""
        v = await self.db.get_video(slug.lower())
        if v is None:
            v = await self.discover(slug.lower())
        sources = await self.db.get_sources(v["id"])
        ranked = self.rank(sources, site=site, direct_only=direct_only)
        timeout = self.store.current.resolve_timeout
        deadline = time.monotonic() + timeout
        if not ranked:
            if not site and (r := await self._discover_for(v, deadline, direct_only=direct_only)):
                return r
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
            try:
                r = await asyncio.wait_for(self._ensure(v, src, self._need(v, min_remaining), line=line if site else None,
                                                        direct_only=direct_only), left)
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
            except (FetchError, ParseError, TimeoutError) as e:
                msg = str(e) or "超时"
                await self.db.source_failed(src["id"], msg)
                errors.append(f"{label}：{msg}")
                log.warning("源 %s 取地址失败：%s", label, msg)
                continue
            if i:
                self.metrics.inc("play_failover")
                log.info("%s 前 %d 个源不可用，改用 %s", slug, i, label)
            return r
        if not site and (r := await self._discover_for(v, deadline, direct_only=direct_only)):
            return r
        if gone == len(ranked):
            raise NotFound(f"{slug} 的源都已下架")
        if blocked is not None and len(errors) == 1:
            raise blocked
        raise FetchError("；".join(errors))

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
                    find_by_code(site, self.fetcher.site(name), v["code"], bool(v["uncensored"]), priority=True), left)
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
            usable = self.rank(await self.db.get_sources(v["id"]), site=name)
            if not usable:
                log.info("现场找源 %s：在 %s 找到了，但字幕不合偏好（字幕不回退），不用", v["slug"], site.label)
                continue
            log.info("现场找源 %s：已知的源都不能用，在 %s 找到 %s", v["slug"], site.label,
                     "、".join(x.key for x in found))
            left = deadline - time.monotonic()
            try:
                return await asyncio.wait_for(self._ensure(v, usable[0], self._need(v, None)), max(left, 1))
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
                found = await find_by_code(site, self.fetcher.site(name), slug, priority=True)
            except (Blocked, FetchError, ParseError) as e:
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
            raise hard[0]
        raise NotFound(slug)

    async def _ensure(self, v: dict, src: dict, need: int, stale: str | None = None, *, line: str | None = None,
                      direct_only: bool = False) -> Resolved:
        """保证这个源有剩余有效期 ≥ need 的地址；stale 是调用方确认已失效的地址，缓存仍是它时强制刷新。"""
        site = get_site(src["site"])
        if site.multi_line:
            return await self._ensure_lines(v, src, site, need, stale, line, direct_only)
        if stale is None and self._fresh(src, need):
            self.metrics.inc("play_cache_hit")
            return Resolved(v, src, site)
        lock = self._locks.setdefault(src["id"], asyncio.Lock())
        async with lock:
            src = await self.db.get_source(src["id"])
            if stale is not None:
                if src["stream_url"] and src["stream_url"] != stale:
                    return Resolved(v, src, site)
            elif self._fresh(src, need):
                return Resolved(v, src, site)
            st = await site.fetch_stream(self.fetcher.site(site.name), src["key"])
            if st.detail is not None:
                await self.db.upsert_detail(site.name, st.detail, v["slug"], self.store.current.site_rank)
            await self.db.set_stream(src["id"], st.url, st.expires)
            self.metrics.inc("play_refresh")
            src = await self.db.get_source(src["id"])
            v = await self.db.get_video_by_id(v["id"]) or v
        if len(self._locks) > 2000:
            self._locks = {k: lk for k, lk in self._locks.items() if lk.locked()}
        return Resolved(v, src, site)

    async def _ensure_lines(self, v: dict, src: dict, site: Site, need: int, stale: str | None,
                            want: str | None, direct_only: bool) -> Resolved:
        """多线路站点：按顺序找一条有新鲜直链的线路，没有就逐条现取；全失败时重抓一次详情（线路数据可能换了）再试。"""
        lock = self._locks.setdefault(src["id"], asyncio.Lock())
        errors: list[str] = []
        async with lock:
            lines = await self.db.get_lines(src["id"])
            refreshed = False
            if not lines:
                await self._refresh_detail(v, src, site)
                refreshed = True
                lines = await self.db.get_lines(src["id"])
            for _ in range(2):
                for ln in self.rank_lines(site, lines, want=want, direct_only=direct_only):
                    traits = site.line_traits(ln["line"], ln["host"])
                    fresh = ln["stream_url"] and (not traits.expires or (ln["stream_expires"] or 0) - time.time() >= need)
                    if fresh and ln["stream_url"] != stale:
                        if (src["line"], src["stream_url"]) != (ln["line"], ln["stream_url"]):
                            await self.db.use_line(src["id"], ln)
                        self.metrics.inc("play_cache_hit")
                        return await self._resolved(v, src["id"], site, ln["id"])
                    try:
                        hs = await site.resolve_line(self.fetcher, ln["line"], ln["link"])
                    except (FetchError, ParseError, NotFound, ValueError, KeyError) as e:
                        await self.db.line_failed(ln["id"], str(e))
                        errors.append(f"{ln['line']}：{e}")
                        log.info("%s %s 线路 %s 取直链失败：%s", site.label, src["key"], ln["line"], e)
                        continue
                    await self.db.set_line_stream(ln["id"], hs.url, hs.expires, hs.host, hs.referer)
                    self.metrics.inc("play_refresh")
                    return await self._resolved(v, src["id"], site, ln["id"])
                if refreshed:
                    break
                await self._refresh_detail(v, src, site)
                refreshed = True
                lines = await self.db.get_lines(src["id"])
        raise FetchError("；".join(errors) or "没有可用的线路（都停用了，或者都不支持）")

    async def _refresh_detail(self, v: dict, src: dict, site: Site) -> None:
        d = await site.fetch_detail(self.fetcher.site(site.name), src["key"], priority=True)
        await self.db.upsert_detail(site.name, d, v["slug"], self.store.current.site_rank)

    async def _resolved(self, v: dict, source_id: int, site: Site, line_id: int) -> Resolved:
        src = await self.db.get_source(source_id)
        line = next(ln for ln in await self.db.get_lines(source_id) if ln["id"] == line_id)
        forced = self.store.current.site(site.name).line(line["line"]).proxy
        return Resolved(await self.db.get_video_by_id(v["id"]) or v, src, site, line, forced)

    async def ensure_source(self, source_id: int, *, min_remaining: int = 60, stale: str | None = None) -> Resolved:
        """中转分片时用：只认这一个源、这条线路（不同站点、不同线路切片不一样，播到一半不能换）。"""
        src = await self.db.get_source(source_id)
        if src is None:
            raise NotFound(f"源 #{source_id}")
        v = await self.db.get_video_by_id(src["video_id"])
        if v is None:
            raise NotFound(f"源 #{source_id} 的作品")
        return await self._ensure(v, src, min_remaining, stale, line=src["line"] or None)


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
    return f"{r.site.label} {r.source['key']}" + (f" 线路 {r.line['line']}" if r.line else "")


async def _resolve_or_http(request: Request, slug: str, **kw) -> Resolved:
    try:
        return await _ctx(request).resolver.resolve(slug, **kw)
    except (NotFound, VideoGone):
        raise HTTPException(404, f"影片不存在或已下架：{slug}") from None
    except Blocked as e:
        raise HTTPException(503, f"站点拦截中：{e}", headers={"Retry-After": str(int(e.retry_after))}) from None
    except (FetchError, ParseError) as e:
        log.warning("解析播放地址失败 %s：%s", slug, e)
        raise HTTPException(502, f"解析播放地址失败：{e}") from None


@router.api_route("/play/{name}", methods=["GET", "HEAD"])
async def play(name: str, request: Request, t: str = "", proxy: int = 0, src: str = "", line: str = ""):
    """proxy=1 强制中转（网页试播用）；src=站点 只用这个站点的源，line=线路 再限定线路。
    浏览器跨域请求（带 Origin）也走中转并返回 CORS 头。"""
    ctx = _ctx(request)
    _check_token(request, t)
    slug = name.lower().removesuffix(".m3u8")
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(404)
    ctx.metrics.inc("play_requests")
    r = await _resolve_or_http(request, slug, site=src or None, line=line or None)
    s = ctx.store.current
    ua = request.headers.get("user-agent", "")
    origin = request.headers.get("origin", "")
    fetch_mode = request.headers.get("sec-fetch-mode", "")
    blocked_uas = s.proxy_user_agents if r.traits.ua_block else []
    proxied = (bool(proxy) or s.play_mode == "proxy" or not r.traits.direct
               or bool(direct_blocker(blocked_uas, ua, origin, fetch_mode)))
    log.info("播放 %s：%s %s（%s，客户端 %s，UA %s）", slug, _label(r), "中转" if proxied else "302", _left(r),
             request.client.host if request.client else "?", ua[:60])
    if proxied:
        ctx.metrics.inc("play_proxy")
        resp = await (_proxy_playlist(request, r, t) if _is_hls(r.url) else _proxy_file(request, r))
    else:
        ctx.metrics.inc("play_redirect")
        resp = RedirectResponse(r.url, status_code=302)
    if origin:
        resp.headers.update(CORS_HEADERS)
    return resp


@router.options("/play/{name}")
@router.options("/hls/{source_id}/{path:path}")
async def cors_preflight():
    return Response(status_code=204, headers=CORS_HEADERS)


@router.get("/api/resolve/{name:path}")
async def resolve_for_gateway(name: str, request: Request, ua: str = "", origin: str = "", fetch_mode: str = "",
                              min_remaining: int | None = None):
    """给 embyGateway 的 http_resolver 后端用：返回能让客户端直连的地址，由网关 302。

    只挑播放器能直连的源（比如 Jable）。作品只有要中转的源（比如 MissAV）时：设了 resolve_proxy_url 就返回
    本服务的中转地址，没设返回 409，网关回退。
    不按客户端判断回退：网关回退会反代 Emby，Emby 再 302 到 strm 里的内网地址，外部客户端访问不到。
    ua / origin / fetch_mode 只记日志，方便排查。
    name 可以是 slug、slug.m3u8，也可以是网关 objectKey 原样（如 play/ipzz-983.m3u8），取最后一段。
    """
    ctx = _ctx(request)
    s = ctx.store.current
    if not s.resolve_token:
        raise HTTPException(403, "未设置 resolve_token，接口未开放")
    auth = request.headers.get("authorization", "")
    given = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else request.query_params.get("token", "")
    if not secrets.compare_digest(given.encode(), s.resolve_token.encode()):
        raise HTTPException(401, "resolve_token 错误")
    slug = name.rsplit("/", 1)[-1].lower().removesuffix(".m3u8")
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(404, f"无法识别的影片：{name}")
    ctx.metrics.inc("resolve_requests")
    extra = f"，fetch_mode {fetch_mode}" if fetch_mode else ""
    try:
        r = await _resolve_or_http(request, slug, min_remaining=min_remaining, direct_only=True)
    except NoDirectSource:
        if not s.resolve_proxy_url:
            log.info("resolve %s：只有要中转的源，没设公网中转地址，返回 409（UA %s%s）", slug, ua[:60], extra)
            raise HTTPException(409, "这部影片只有需要本服务中转的源，没有设置公网中转地址") from None
        url = f"{s.resolve_proxy_url}/play/{slug}.m3u8?proxy=1"
        if s.play_token:
            url += f"&t={quote(s.play_token)}"
        log.info("resolve %s：返回本服务中转地址（UA %s%s）", slug, ua[:60], extra)
        return {"slug": slug, "url": url, "expires_at": 0, "ttl": 6 * 3600, "duration": None}
    expires = r.expires or 0
    log.info("resolve %s：返回 %s 的 CDN 地址（%s，UA %s%s）", slug, _label(r), _left(r), ua[:60], extra)
    return {"slug": slug, "url": r.url, "expires_at": expires,
            "ttl": max(0, int(expires - time.time())), "duration": r.video.get("duration")}


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


async def close_stream(resp) -> None:
    """关闭 stream=True 的响应。curl_cffi 的 aclose() 只等传输结束：不先设 quit_now 的话，客户端拖动、断开后
    会把整个文件继续下完、堆在内存里（mp4 中转时就是几百 MB）。"""
    if getattr(resp, "quit_now", None) is not None:
        resp.quit_now.set()
    await resp.aclose()


def _is_hls(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".m3u8")


async def _proxy_file(request: Request, r: Resolved) -> Response:
    """直链是 mp4 这类单文件（如 streamtape）：原样转发，带上 Range，支持拖动。"""
    ctx = _ctx(request)
    headers = dict(r.traits.headers)
    if rng := request.headers.get("range"):
        headers["Range"] = rng
    try:
        resp = await ctx.fetcher.session.get(r.url, stream=True, headers=headers,
                                             timeout=ctx.store.current.request_timeout)
    except Exception as e:
        raise HTTPException(502, f"请求直链失败：{e}") from None
    if resp.status_code not in (200, 206):
        await close_stream(resp)
        raise HTTPException(502, f"直链返回 HTTP {resp.status_code}")
    out = {k: v for k in ("content-length", "content-range", "accept-ranges")
           if (v := resp.headers.get(k))}
    if request.method == "HEAD":
        await close_stream(resp)
        return Response(status_code=resp.status_code, headers=out,
                        media_type=resp.headers.get("content-type") or "video/mp4")

    async def body():
        try:
            async for chunk in resp.aiter_content():
                yield chunk
        finally:
            await close_stream(resp)

    return StreamingResponse(body(), status_code=resp.status_code, headers=out,
                             media_type=resp.headers.get("content-type") or "video/mp4")


async def _proxy_playlist(request: Request, r: Resolved, t: str) -> Response:
    ctx = _ctx(request)
    try:
        raw = await ctx.fetcher.get_bytes(r.url, headers=r.traits.headers)
    except (FetchError, NotFound) as e:
        raise HTTPException(502, f"获取播放列表失败：{e}") from None
    q = f"?t={quote(t)}" if t else ""
    root = r.url.rsplit("/", 1)[0] + "/"
    body = raw.decode("utf-8", "replace")
    heights = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", body)]
    if heights and max(heights) != r.source["height"]:
        await ctx.db.update_source(r.source["id"], height=max(heights))  # 多码率的源记下最高分辨率，挑源时用
    # /play/{slug}.m3u8 回到 /hls/{源 id}/：相对地址解析，不依赖对外地址的写法
    text = rewrite_playlist(body, r.url, root, f"../hls/{r.source['id']}/", q, r.traits.disguised_segments)
    return Response(text, media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{source_id}/{path:path}")
async def hls_file(source_id: int, path: str, request: Request, t: str = ""):
    """中转模式：转发子清单、分片和 key；遇到 403 自动换新地址重试一次。"""
    ctx = _ctx(request)
    _check_token(request, t)
    if not HLS_PATH_RE.fullmatch(path) or ".." in path.split("/"):
        raise HTTPException(404)
    try:
        r = await ctx.resolver.ensure_source(source_id)
    except (NotFound, VideoGone):
        raise HTTPException(404, "源不存在或已下架") from None
    except Blocked as e:
        raise HTTPException(503, f"站点拦截中：{e}", headers={"Retry-After": str(int(e.retry_after))}) from None
    except (FetchError, ParseError) as e:
        raise HTTPException(502, f"解析播放地址失败：{e}") from None
    headers_up = r.traits.headers or None
    for attempt in range(2):
        url = _upstream(r, path)
        if url is None:
            raise HTTPException(404)
        try:
            resp = await ctx.fetcher.session.get(url, stream=True, timeout=ctx.store.current.request_timeout,
                                                 headers=headers_up)
        except Exception as e:
            raise HTTPException(502, f"CDN 请求失败：{e}") from None
        if resp.status_code in (403, 410) and attempt == 0 and not path.startswith("_x/"):
            await close_stream(resp)
            ctx.metrics.inc("play_token_expired")
            try:
                r = await ctx.resolver.ensure_source(source_id, stale=r.url)
            except (NotFound, VideoGone, Blocked, FetchError, ParseError) as e:
                raise HTTPException(502, f"CDN 地址失效，换新失败：{e}") from None
            continue
        if resp.status_code != 200:
            await close_stream(resp)
            raise HTTPException(502, f"CDN 返回 HTTP {resp.status_code}")
        cors = dict(CORS_HEADERS) if request.headers.get("origin") else {}
        if path.endswith(".m3u8"):
            body = b"".join([chunk async for chunk in resp.aiter_content()])
            await close_stream(resp)
            q = f"?t={quote(t)}" if t else ""
            up = "../" * path.count("/")  # 从当前子清单的位置回到 /hls/{源 id}/
            root = r.url.rsplit("/", 1)[0] + "/"
            text = rewrite_playlist(body.decode("utf-8", "replace"), url, root, up, q, r.traits.disguised_segments)
            return Response(text, media_type="application/vnd.apple.mpegurl", headers=cors)

        strip = r.traits.fake_header

        async def stream(resp=resp):
            try:
                head = b""
                async for chunk in resp.aiter_content():
                    if strip and head is not None:
                        head += chunk  # 攒够一段再找 TS 起点，剥掉前面的假文件头
                        if len(head) < 8192 + 188 * 5:
                            continue
                        chunk, head = head[max(ts_start(head), 0):], None
                    yield chunk
                if strip and head:
                    yield head[max(ts_start(head), 0):]
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
