"""播放解析：strm 指向 /play/{slug}.m3u8，按需换取新鲜的 CDN 地址后 302（或由本服务中转）。"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

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


def _check_token(request: Request, t: str) -> None:
    token = _ctx(request).store.current.play_token
    if token and not secrets.compare_digest(t.encode(), token.encode()):
        raise HTTPException(403, "播放令牌错误")


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
    """proxy=1 强制中转（网页试播用：CDN 不带 CORS 头）。"""
    ctx = _ctx(request)
    _check_token(request, t)
    slug = name.lower().removesuffix(".m3u8")
    if not SLUG_RE.fullmatch(slug):
        raise HTTPException(404)
    ctx.metrics.inc("play_requests")
    v = await _resolve_or_http(request, slug)
    s = ctx.store.current
    ua = request.headers.get("user-agent", "")
    proxied = bool(proxy) or s.play_mode == "proxy" or any(p and p in ua for p in s.proxy_user_agents)
    left = int((v.get("hls_expires") or 0) - time.time())
    log.info("播放 %s：%s（地址剩余 %d 分钟，客户端 %s，UA %s）", slug, "中转" if proxied else "302",
             left // 60, request.client.host if request.client else "?", ua[:60])
    if proxied:
        ctx.metrics.inc("play_proxy")
        return await _proxy_playlist(request, v, t)
    ctx.metrics.inc("play_redirect")
    return RedirectResponse(v["hls_url"], status_code=302)


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

        headers = {}
        if cl := resp.headers.get("content-length"):
            headers["Content-Length"] = cl
        return StreamingResponse(body(), media_type=resp.headers.get("content-type") or "video/mp2t", headers=headers)
    raise HTTPException(502, "CDN 地址刷新后仍不可用")
