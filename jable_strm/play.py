"""播放解析：strm 指向 /play/{slug}.m3u8，按需换取新鲜的 CDN 地址后 302（或由本服务中转）。"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from .config import SettingsStore
from .db import Database
from .fetcher import Blocked, FetchError, Fetcher, NotFound
from .observability import Metrics
from .parser import ParseError, VideoGone, parse_detail

log = logging.getLogger(__name__)

DEFAULT_DURATION = 2 * 3600
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
HLS_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class Resolver:
    """维护每部影片的 hlsUrl，过期前自动换新；同一影片的并发请求只抓一次。"""

    def __init__(self, db: Database, fetcher: Fetcher, store: SettingsStore, metrics: Metrics) -> None:
        self.db = db
        self.fetcher = fetcher
        self.store = store
        self.metrics = metrics
        self._locks: dict[str, asyncio.Lock] = {}

    def _fresh(self, v: dict | None, need: int) -> bool:
        return bool(v and v.get("hls_url") and (v.get("hls_expires") or 0) - time.time() >= need)

    async def resolve(self, slug: str, *, min_remaining: int | None = None, stale: str | None = None) -> dict:
        """返回带可用 hls_url 的影片记录。

        min_remaining：要求的剩余有效期（秒），默认 时长 + hls_margin。
        stale：调用方确认已失效的地址；缓存仍是它时强制刷新。
        """
        slug = slug.lower()
        v = await self.db.get_video(slug)
        need = min_remaining
        if need is None:
            need = (v and v.get("duration") or DEFAULT_DURATION) + self.store.current.hls_margin * 60
        if stale is None and self._fresh(v, need):
            self.metrics.inc("play_cache_hit")
            return v
        lock = self._locks.setdefault(slug, asyncio.Lock())
        async with lock:
            v = await self.db.get_video(slug)
            if stale is not None:
                if v and v.get("hls_url") and v["hls_url"] != stale:
                    return v
            elif self._fresh(v, need):
                return v
            page = await self.fetcher.get_page(f"/videos/{slug}/", priority=True)
            await self.db.upsert_detail(parse_detail(page.html, slug))
            self.metrics.inc("play_refresh")
            v = await self.db.get_video(slug)
        if len(self._locks) > 2000:
            self._locks = {k: l for k, l in self._locks.items() if l.locked()}
        return v


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


async def _resolve_or_http(request: Request, slug: str, **kw) -> dict:
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
async def play(name: str, request: Request, t: str = "", proxy: int = 0):
    """proxy=1 强制中转（网页试播用）。浏览器跨域请求（带 Origin）也走中转并返回 CORS 头。"""
    ctx = _ctx(request)
    _check_token(request, t)
    slug = name.lower().removesuffix(".m3u8")
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(404)
    ctx.metrics.inc("play_requests")
    v = await _resolve_or_http(request, slug)
    s = ctx.store.current
    ua = request.headers.get("user-agent", "")
    origin = request.headers.get("origin", "")
    fetch_mode = request.headers.get("sec-fetch-mode", "")
    proxied = bool(proxy) or s.play_mode == "proxy" or bool(direct_blocker(s.proxy_user_agents, ua, origin, fetch_mode))
    left = int((v.get("hls_expires") or 0) - time.time())
    log.info("播放 %s：%s（地址剩余 %d 分钟，客户端 %s，UA %s）", slug, "中转" if proxied else "302",
             left // 60, request.client.host if request.client else "?", ua[:60])
    if proxied:
        ctx.metrics.inc("play_proxy")
        resp = await _proxy_playlist(request, v, t)
    else:
        ctx.metrics.inc("play_redirect")
        resp = RedirectResponse(v["hls_url"], status_code=302)
    if origin:
        resp.headers.update(CORS_HEADERS)
    return resp


@router.options("/play/{name}")
@router.options("/hls/{slug}/{file}")
async def cors_preflight():
    return Response(status_code=204, headers=CORS_HEADERS)


@router.get("/api/resolve/{name:path}")
async def resolve_for_gateway(name: str, request: Request, ua: str = "", origin: str = "", fetch_mode: str = "",
                              min_remaining: int | None = None):
    """给 embyGateway 的 http_resolver 后端用：返回可直连的 CDN 地址；客户端不能直连时返回 409，由网关回退。

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
    reason = direct_blocker(s.proxy_user_agents, ua, origin, fetch_mode)
    if reason:
        ctx.metrics.inc("resolve_fallback")
        log.info("resolve %s：%s，让网关回退（UA %s）", slug, reason, ua[:60])
        return JSONResponse({"slug": slug, "reason": reason}, status_code=409)
    v = await _resolve_or_http(request, slug, min_remaining=min_remaining)
    ctx.metrics.inc("resolve_direct")
    expires = v.get("hls_expires") or 0
    log.info("resolve %s：直连 CDN（地址剩余 %d 分钟，UA %s）", slug, (expires - time.time()) // 60, ua[:60])
    return {"slug": slug, "url": v["hls_url"], "expires_at": expires,
            "ttl": max(0, int(expires - time.time())), "duration": v.get("duration")}


async def _proxy_playlist(request: Request, v: dict, t: str) -> Response:
    ctx = _ctx(request)
    try:
        text = (await ctx.fetcher.get_bytes(v["hls_url"])).decode("utf-8", "replace")
    except (FetchError, NotFound) as e:
        raise HTTPException(502, f"获取播放列表失败：{e}") from None
    base = f"../hls/{v['slug']}/"  # 相对播放列表地址解析，不依赖对外地址的写法
    q = f"?t={quote(t)}" if t else ""

    def local(ref: str) -> str:
        return base + ref.rsplit("/", 1)[-1].split("?", 1)[0] + q

    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = local(line.strip())
        elif 'URI="' in line:
            line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{local(m.group(1))}"', line)
        lines.append(line)
    return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{slug}/{file}")
async def hls_file(slug: str, file: str, request: Request, t: str = ""):
    """中转模式：转发分片 / key；遇到 403 自动换新地址重试一次。"""
    ctx = _ctx(request)
    _check_token(request, t)
    slug = slug.lower()
    if not SLUG_RE.fullmatch(slug) or not HLS_FILE_RE.fullmatch(file):
        raise HTTPException(404)
    v = await _resolve_or_http(request, slug, min_remaining=60)
    for attempt in range(2):
        url = v["hls_url"].rsplit("/", 1)[0] + "/" + file
        try:
            resp = await ctx.fetcher.session.get(url, stream=True, timeout=ctx.store.current.request_timeout)
        except Exception as e:
            raise HTTPException(502, f"CDN 请求失败：{e}") from None
        if resp.status_code in (403, 410) and attempt == 0:
            await resp.aclose()
            ctx.metrics.inc("play_token_expired")
            v = await _resolve_or_http(request, slug, stale=v["hls_url"])
            continue
        if resp.status_code != 200:
            await resp.aclose()
            raise HTTPException(502, f"CDN 返回 HTTP {resp.status_code}")

        async def body(r=resp):
            try:
                async for chunk in r.aiter_content():
                    yield chunk
            finally:
                await r.aclose()

        headers = dict(CORS_HEADERS) if request.headers.get("origin") else {}
        if cl := resp.headers.get("content-length"):
            headers["Content-Length"] = cl
        return StreamingResponse(body(), media_type=resp.headers.get("content-type") or "video/mp2t", headers=headers)
    raise HTTPException(502, "CDN 地址刷新后仍不可用")
