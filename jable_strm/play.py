"""播放解析：strm 指向 /play/{slug}.m3u8，挑一个源换取新鲜的播放地址，302 过去或由本服务中转。

一部作品可以有多个源（不同站点）。挑源顺序见 Resolver.rank；某个源取地址失败（下架、被拦、解析失败）就换下一个。
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
from dataclasses import dataclass
from urllib.parse import quote, urljoin

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from .codes import code_key
from .config import SettingsStore
from .db import Database, source_cooldown
from .fetcher import Blocked, FetchError, Fetcher, NotFound
from .observability import Metrics
from .parser import ParseError, VideoGone
from .sites import SITES, Site, get_site

log = logging.getLogger(__name__)

DEFAULT_DURATION = 2 * 3600
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
HLS_PATH_RE = re.compile(r"^(?:_x/[0-9a-f]{16}/[A-Za-z0-9_-]+|[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+){0,5})$")
DISGUISE_EXTS = (".jpeg", ".jpg", ".png", ".gif", ".webp", ".html", ".txt", ".js", ".css")
SUBTITLE_CODES = {"zh": "zh", "none": "", "en": "en"}
_SIGN_KEY = secrets.token_bytes(16)  # _x/ 地址的签名密钥，进程重启后旧地址失效（播放器会重新请求 /play）


class NoDirectSource(Exception):
    """作品有源，但没有播放器能直连的（都要中转）。"""


@dataclass
class Resolved:
    video: dict
    source: dict
    site: Site

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
            if direct_only and not st.stream.direct:
                continue
            out.append(src)

        def tier(src: dict) -> int:
            return pref.index(src["subtitle"]) if src["subtitle"] in pref else len(pref)

        if out and not s.subtitle_fallback:
            best = min(tier(x) for x in out)
            out = [x for x in out if tier(x) == best]
        return sorted(out, key=lambda x: (tier(x), source_cooldown(x) > t, s.site_rank(x["site"]),
                                          not self._fresh(x, 60), -(x["height"] or 0)))

    def _fresh(self, src: dict, need: int) -> bool:
        if not src["stream_url"]:
            return False
        if not get_site(src["site"]).stream.expires:
            return True
        return (src["stream_expires"] or 0) - time.time() >= need

    def _need(self, v: dict, min_remaining: int | None) -> int:
        if min_remaining is not None:
            return min_remaining
        return (v.get("duration") or DEFAULT_DURATION) + self.store.current.hls_margin * 60

    # ---- 取地址 ----

    async def resolve(self, slug: str, *, min_remaining: int | None = None, site: str | None = None,
                      direct_only: bool = False) -> Resolved:
        """挑一个能用的源并返回它的新鲜地址；依次尝试，全部失败才报错。"""
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
                r = await asyncio.wait_for(self._ensure(v, src, self._need(v, min_remaining)), left)
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
            if name in have or not s.site(name).enabled or (direct_only and not site.stream.direct):
                continue
            check = await self.db.get_source_check(v["id"], name)
            if check and not check["found"] and check["checked_at"] > cutoff:
                continue
            key = site.key_for(v["code"], uncensored=bool(v["uncensored"]))
            left = deadline - time.monotonic()
            if not key or left <= 0:
                continue
            try:
                d = await asyncio.wait_for(site.fetch_detail(self.fetcher.site(name), key, priority=True), left)
            except (NotFound, VideoGone):
                await self.db.set_source_check(v["id"], name, False)
                continue
            except (Blocked, FetchError, ParseError, TimeoutError) as e:
                log.info("现场找源 %s：%s 失败：%s", v["slug"], site.label, e)
                continue
            if code_key(d.code) != v["code_key"] or d.uncensored != bool(v["uncensored"]) or not d.stream_url:
                await self.db.set_source_check(v["id"], name, False)
                continue
            await self.db.upsert_detail(name, d, v["slug"], s.site_rank, video_id=v["id"])
            await self.db.set_source_check(v["id"], name, True)
            self.metrics.inc("play_discovered")
            src = await self.db.find_source(name, d.key)
            if not self.rank([*(await self.db.get_sources(v["id"]))], site=name):
                log.info("现场找源 %s：在 %s 找到了，但字幕不合偏好（字幕不回退），不用", v["slug"], site.label)
                continue
            log.info("现场找源 %s：已知的源都不能用，在 %s 找到 %s", v["slug"], site.label, d.key)
            return Resolved(await self.db.get_video_by_id(v["id"]) or v, src, site)
        return None

    async def discover(self, slug: str) -> dict:
        """库里没有的影片（比如别的工具生成的 strm）：把 slug 当番号，按站点优先顺序到各站找，找到就入库。"""
        s = self.store.current
        errors: list[Exception] = []
        for name in s.site_priority:
            site = SITES[name]
            key = site.key_for(slug)
            if not key or not s.site(name).enabled or (name != "jable" and not s.play_discover):
                continue
            try:
                d = await site.fetch_detail(self.fetcher.site(name), key, priority=True)
            except (NotFound, VideoGone) as e:
                errors.append(e)
                continue
            except (Blocked, FetchError, ParseError) as e:
                errors.append(e)
                log.info("在 %s 找 %s 失败：%s", site.label, slug, e)
                continue
            vid = await self.db.upsert_detail(name, d, slug, s.site_rank)
            log.info("库里没有 %s，在 %s 找到了，已入库", slug, site.label)
            return await self.db.get_video_by_id(vid)
        hard = [e for e in errors if not isinstance(e, (NotFound, VideoGone))]
        if hard:
            raise hard[0]
        raise NotFound(slug)

    async def _ensure(self, v: dict, src: dict, need: int, stale: str | None = None) -> Resolved:
        """保证这个源有剩余有效期 ≥ need 的地址；stale 是调用方确认已失效的地址，缓存仍是它时强制刷新。"""
        site = get_site(src["site"])
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

    async def ensure_source(self, source_id: int, *, min_remaining: int = 60, stale: str | None = None) -> Resolved:
        """中转分片时用：只认这一个源（不同站点切片不一样，播到一半不能换站）。"""
        src = await self.db.get_source(source_id)
        if src is None:
            raise NotFound(f"源 #{source_id}")
        v = await self.db.get_video_by_id(src["video_id"])
        if v is None:
            raise NotFound(f"源 #{source_id} 的作品")
        return await self._ensure(v, src, min_remaining, stale)


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
    if not r.site.stream.expires:
        return "地址长期有效"
    if not r.expires:
        return "地址有效期未知"  # 地址格式不认识，取不到过期时间
    return f"地址剩余 {int(r.expires - time.time()) // 60} 分钟"


def _label(r: Resolved) -> str:
    return f"{r.site.label} {r.source['key']}"


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
async def play(name: str, request: Request, t: str = "", proxy: int = 0, src: str = ""):
    """proxy=1 强制中转（网页试播用）；src=站点 只用这个站点的源。浏览器跨域请求（带 Origin）也走中转并返回 CORS 头。"""
    ctx = _ctx(request)
    _check_token(request, t)
    slug = name.lower().removesuffix(".m3u8")
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(404)
    ctx.metrics.inc("play_requests")
    r = await _resolve_or_http(request, slug, site=src or None)
    s = ctx.store.current
    ua = request.headers.get("user-agent", "")
    origin = request.headers.get("origin", "")
    fetch_mode = request.headers.get("sec-fetch-mode", "")
    proxied = (bool(proxy) or s.play_mode == "proxy" or not r.site.stream.direct
               or bool(direct_blocker(s.proxy_user_agents, ua, origin, fetch_mode)))
    log.info("播放 %s：%s %s（%s，客户端 %s，UA %s）", slug, _label(r), "中转" if proxied else "302", _left(r),
             request.client.host if request.client else "?", ua[:60])
    if proxied:
        ctx.metrics.inc("play_proxy")
        resp = await _proxy_playlist(request, r, t)
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
    log.info("resolve %s：返回 %s 的 CDN 地址（%s，UA %s%s）", slug, r.site.label, _left(r), ua[:60], extra)
    return {"slug": slug, "url": r.url, "expires_at": expires,
            "ttl": max(0, int(expires - time.time())), "duration": r.video.get("duration")}


# ---- 中转 ----


def _sign(url: str) -> str:
    return hmac.new(_SIGN_KEY, url.encode(), hashlib.sha256).hexdigest()[:16]


def _encode_x(url: str) -> str:
    return f"_x/{_sign(url)}/" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _decode_x(path: str) -> str | None:
    _, sig, data = path.split("/", 2)
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
        if absolute.startswith(root) and "?" not in absolute:
            rel = absolute[len(root):]
            if not HLS_PATH_RE.fullmatch(rel) or ".." in rel.split("/"):
                rel = _encode_x(absolute)
        else:
            rel = _encode_x(absolute)
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
    if r.site.stream.disguised_segments and path.endswith(".ts") and path[:-3].lower().endswith(DISGUISE_EXTS):
        path = path[:-3]
    return r.url.rsplit("/", 1)[0] + "/" + path


async def _proxy_playlist(request: Request, r: Resolved, t: str) -> Response:
    ctx = _ctx(request)
    try:
        raw = await ctx.fetcher.get_bytes(r.url, headers=r.site.stream.headers)
    except (FetchError, NotFound) as e:
        raise HTTPException(502, f"获取播放列表失败：{e}") from None
    q = f"?t={quote(t)}" if t else ""
    root = r.url.rsplit("/", 1)[0] + "/"
    body = raw.decode("utf-8", "replace")
    heights = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", body)]
    if heights and max(heights) != r.source["height"]:
        await ctx.db.update_source(r.source["id"], height=max(heights))  # 多码率的源记下最高分辨率，挑源时用
    # /play/{slug}.m3u8 回到 /hls/{源 id}/：相对地址解析，不依赖对外地址的写法
    text = rewrite_playlist(body, r.url, root, f"../hls/{r.source['id']}/", q, r.site.stream.disguised_segments)
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
    headers_up = r.site.stream.headers or None
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
            await resp.aclose()
            ctx.metrics.inc("play_token_expired")
            try:
                r = await ctx.resolver.ensure_source(source_id, stale=r.url)
            except (NotFound, VideoGone, Blocked, FetchError, ParseError) as e:
                raise HTTPException(502, f"CDN 地址失效，换新失败：{e}") from None
            continue
        if resp.status_code != 200:
            await resp.aclose()
            raise HTTPException(502, f"CDN 返回 HTTP {resp.status_code}")
        cors = dict(CORS_HEADERS) if request.headers.get("origin") else {}
        if path.split("?", 1)[0].endswith(".m3u8") or (path.startswith("_x/") and url.split("?", 1)[0].endswith(".m3u8")):
            body = b"".join([chunk async for chunk in resp.aiter_content()])
            await resp.aclose()
            q = f"?t={quote(t)}" if t else ""
            up = "../" * path.count("/")  # 从当前子清单的位置回到 /hls/{源 id}/
            root = r.url.rsplit("/", 1)[0] + "/"
            text = rewrite_playlist(body.decode("utf-8", "replace"), url, root, up, q, r.site.stream.disguised_segments)
            return Response(text, media_type="application/vnd.apple.mpegurl", headers=cors)

        async def stream(resp=resp):
            try:
                async for chunk in resp.aiter_content():
                    yield chunk
            finally:
                await resp.aclose()

        headers = cors
        if cl := resp.headers.get("content-length"):
            headers["Content-Length"] = cl
        media = resp.headers.get("content-type") or "video/mp2t"
        if path.endswith(".ts"):
            media = "video/mp2t"
        return StreamingResponse(stream(), media_type=media, headers=headers)
    raise HTTPException(502, "CDN 地址刷新后仍不可用")
