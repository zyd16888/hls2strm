"""单影片所有已知线路并行测速；不选源、不转码、不写播放失败或健康排序。"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urljoin

from .errors import FetchError, PoolBusy
from .fetcher import close_stream
from .play import Resolved
from .quality import filter_master
from .runtime import traffic
from .sites import get_site
from .sites.hosts import HOST_LABELS


def event(kind, **data):
    return json.dumps({"event": kind, **data}, ensure_ascii=False) + "\n"


async def candidates(ctx, video, include_disabled):
    async def one(source):
        site = get_site(source["site"])
        cfg = ctx.store.current.site(site.name)
        lines = await ctx.db.get_lines(source["id"]) if site.multi_line else [None]
        discovery_error = ""
        discovery_ms = 0
        discovery_timeout = False
        if site.multi_line and not lines and (cfg.enabled or include_disabled) and source["status"] == "active":
            started = time.monotonic()
            try:
                async with asyncio.timeout(ctx.store.current.speed_test_timeout):
                    detail = await site.fetch_detail(ctx.fetcher.site(site.name), source["key"], priority=True)
                lines = [{"id": 0, "line": name, "link": link, "host": "", "stream_url": "", "stream_expires": None,
                          "referer": "", "height": None} for name, link in detail.lines]
            except Exception as error:
                discovery_timeout = isinstance(error, TimeoutError)
                discovery_error = "线路获取超时" if discovery_timeout else str(error)
            discovery_ms = (time.monotonic()-started)*1000
        rows = []
        for line in lines or [None]:
            name = line["line"] if line else ""
            spec = site.line_specs.get(name)
            enabled = cfg.enabled and (not line or cfg.line(name).enabled)
            reason = discovery_error
            if not reason and source["status"] != "active":
                reason = "源已下架或不可用"
            if not reason and site.multi_line and not line:
                reason = "没有可探测线路"
            if not reason and spec and not spec.supported:
                reason = "线路暂不支持"
            if not reason and not enabled and not include_disabled:
                reason = "已停用，可勾选包含停用线路后探测"
            rows.append((source, line, {"key": f"{source['id']}:{name}", "source_id": source["id"],
                "line_id": line["id"] if line else 0, "site": site.name, "label": site.label, "line": name,
                "enabled": enabled, "status": "timeout" if discovery_timeout else "failed" if discovery_error else "skipped" if reason else "waiting",
                "error": reason, "discovery_ms": discovery_ms}))
        return rows
    groups = await asyncio.gather(*(one(source) for source in await ctx.db.get_sources(video["id"])))
    return [row for group in groups for row in group]


async def resolve_candidate(ctx, video, source, line):
    site = get_site(source["site"])
    cfg = ctx.store.current.site(site.name)
    if line:
        row = dict(line)
        traits = site.line_traits(row["line"], row["host"])
        if not row["stream_url"] or (traits.expires and (row["stream_expires"] or 0) < time.time() + 60):
            stream = await site.resolve_line(ctx.fetcher, row["line"], row["link"])
            row.update(stream_url=stream.url, stream_expires=stream.expires, host=stream.host, referer=stream.referer)
        lc = cfg.line(row["line"])
        return Resolved(video, source, site, row, lc.proxy or lc.direct_mode == "proxy", lc.direct_mode == "allow")
    row = dict(source)
    if not row["stream_url"] or (site.stream.expires and (row["stream_expires"] or 0) < time.time() + 60):
        stream = await site.fetch_stream(ctx.fetcher.site(site.name), row["key"])
        row.update(stream_url=stream.url, stream_expires=stream.expires)
    return Resolved(video, row, site)


async def measure(ctx, resolved, row, settings):
    target = resolved.url
    headers = resolved.traits.headers
    started = time.monotonic()
    is_hls = target.split("?", 1)[0].lower().endswith(".m3u8")
    row.update(host=HOST_LABELS.get(resolved.host, resolved.host), media_type="HLS" if is_hls else "文件",
               mode="外部可直连" if resolved.traits.direct and not resolved.traits.ip_bound else "原样中转",
               height=(resolved.line or resolved.source).get("height") or 0)
    if is_hls:
        for _ in range(4):
            response = await ctx.fetcher.speed_session.get(target, headers=headers, timeout=settings.speed_test_timeout)
            if response.status_code != 200:
                row["http_status"] = response.status_code
                raise FetchError(f"清单返回 HTTP {response.status_code}")
            text = response.content.decode("utf-8", "replace")
            if not text.lstrip().startswith("#EXTM3U"):
                raise FetchError("没有返回有效的 HLS 清单")
            if "#EXT-X-STREAM-INF" in text:
                picked = filter_master(text, None)
                ref = picked[1] if picked else next((ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")), "")
            else:
                refs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
                ref = refs[len(refs) // 2] if refs else ""
            if not ref:
                raise FetchError("清单没有媒体地址")
            target = urljoin(target, ref)
            if "#EXT-X-STREAM-INF" not in text:
                break
        else:
            raise FetchError("播放清单嵌套过深")
    row["manifest_ms"] = round((time.monotonic() - started) * 1000, 1)
    limit = settings.speed_test_bytes * 1024
    started = time.monotonic()
    response = await ctx.fetcher.speed_session.get(target, stream=True,
        headers={**headers, "Range": f"bytes=0-{limit-1}"}, timeout=settings.speed_test_timeout)
    try:
        row["http_status"] = response.status_code
        if response.status_code not in (200, 206):
            raise FetchError(f"媒体返回 HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise FetchError("返回错误页面而非媒体")
        got, first = 0, None
        async for chunk in response.aiter_content():
            if not chunk:
                continue
            first = first or time.monotonic()
            got += min(len(chunk), limit - got)
            if got >= limit:
                break
        if not got:
            raise FetchError("媒体内容为空")
        finished = time.monotonic()
        row.update(sample_bytes=got, ttfb_ms=round((first-started)*1000, 1),
                   download_ms=round((finished-started)*1000, 1),
                   mbps=round(got*8/max(finished-started, .001)/1_000_000, 2), sample_limited=got >= limit)
    finally:
        await close_stream(response)


async def speed_events(ctx, video, include_disabled=False):
    token = traffic.set("speed")
    tasks = []
    try:
        settings = ctx.store.current.model_copy(deep=True)
        rows = await candidates(ctx, video, include_disabled)
        yield event("start", items=[row for _, _, row in rows], sample_kb=settings.speed_test_bytes)
        slots = asyncio.Semaphore(settings.speed_connections)

        async def one(source, line, row):
            if row["status"] != "waiting":
                return row
            queued = time.monotonic()
            async with slots:
                started = time.monotonic()
                row = {**row, "queue_ms": round((started-queued)*1000, 1)}
                try:
                    async with asyncio.timeout(max(0, settings.speed_test_timeout-row["discovery_ms"]/1000)):
                        resolved = await resolve_candidate(ctx, video, source, line)
                        row["resolve_ms"] = round(row["discovery_ms"]+(time.monotonic()-started)*1000, 1)
                        await measure(ctx, resolved, row, settings)
                    row["status"] = "ok"
                except PoolBusy as error:
                    row.update(status="busy", error=str(error))
                except TimeoutError:
                    row.update(status="timeout", error="线路探测超过时限")
                except Exception as error:
                    row.update(status="failed", error=str(error))
                row["total_ms"] = round(row["discovery_ms"]+(time.monotonic()-started)*1000, 1)
                return row

        tasks = [asyncio.create_task(one(*candidate)) for candidate in rows]
        for task in asyncio.as_completed(tasks):
            yield event("result", item=await task)
        yield event("done")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        traffic.reset(token)
