"""Web 控制台的 JSON API。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .config import Settings
from .db import DEFAULT_LIBRARY_ID, source_cooldown
from .engine import snapshot_path
from .fetcher import Blocked, FetchError, NotFound, ping_solver
from .observability import ring
from .parser import ParseError, VideoGone
from .rules import describe_rule
from .sites import SITES, get_site

_basic = HTTPBasic(auto_error=False)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


def require_auth(request: Request, cred: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    boot = request.app.state.ctx.boot
    if not boot.ui_password:
        return
    if cred is None or not (
        secrets.compare_digest(cred.username.encode(), boot.ui_user.encode())
        and secrets.compare_digest(cred.password.encode(), boot.ui_password.encode())
    ):
        raise HTTPException(401, "需要登录", headers={"WWW-Authenticate": 'Basic realm="jable-strm"'})


router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _ctx(request: Request):
    return request.app.state.ctx


# ---- 状态 ----


@router.get("/status")
async def status(request: Request):
    c = _ctx(request)
    return {
        "version": __version__,
        "engine": c.engine.status(),
        "sites": [sf.status() for sf in c.fetcher.sites.values()],
        "solver": c.store.current.solver_url,
        "videos": await c.db.video_stats(),
        "queue": await c.db.queue_stats(),
        "metrics": c.metrics.snapshot(),
        "failures": await c.db.recent_failures(8),
        "subscriptions": await c.db.list_subscriptions(),
        "output_dir": str(c.store.output_dir),
        "public_base_url": c.store.public_base_url,
        "play_mode": c.store.current.play_mode,
        "now": int(time.time()),
    }


@router.post("/engine/{action}")
async def engine_action(action: Literal["pause", "resume"], request: Request):
    c = _ctx(request)
    c.engine.pause() if action == "pause" else c.engine.resume()
    return {"ok": True}


@router.post("/fetcher/reset")
async def fetcher_reset(request: Request, site: str | None = None):
    c = _ctx(request)
    c.fetcher.reset_cooldowns(site)
    c.engine.clear_blocked(site)
    return {"ok": True}


@router.post("/fetcher/test")
async def fetcher_test(request: Request, site: str | None = None):
    c = _ctx(request)
    result = await c.fetcher.test_domains(site)
    for name in {r["site"] for r in result if r["ok"]}:
        c.engine.clear_blocked(name)
    return result


class SolverTestBody(BaseModel):
    url: str = ""  # 留空用已保存的设置；设置页传输入框里的值，不用先保存
    mode: Literal["ping", "solve"] = "ping"
    site: str = "jable"  # solve 时打开哪个站点的首选域名


@router.post("/solver/test")
async def solver_test(body: SolverTestBody, request: Request):
    """ping：只看能不能连上；solve：让解题服务实际打开一次某个站点的首选域名。"""
    c = _ctx(request)
    url = (body.url or c.store.current.solver_url).strip().rstrip("/")
    if not url:
        raise HTTPException(400, "还没有填写解题服务地址")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "地址要以 http:// 或 https:// 开头，如 http://byparr:8191")
    if body.mode == "solve":
        try:
            return await c.fetcher.site(body.site).try_solver(url)
        except ValueError as err:
            raise HTTPException(400, str(err)) from None
    return await ping_solver(url)


# ---- job ----


class JobCreate(BaseModel):
    kind: Literal["list", "videos", "backfill", "rewrite", "probe"]
    site: str = "jable"
    source: str = ""
    sort: str = ""
    start_page: int = 1
    end_page: int = 0
    detail: bool | None = None
    urls: str = ""
    library_id: int | None = None


def _site_of_url(c, url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for name, site in SITES.items():
        bases = c.store.current.site(name).domains + site.default_domains
        if any(host == (urlsplit(b).hostname or "").lower() for b in bases):
            return name
    return None


def _parse_videos(c, text: str, default_site: str) -> list[tuple[str, str]]:
    """每行一个影片网址或站内 key；网址按域名认站点，key 用 default_site。"""
    out = []
    for line in re.split(r"[\s,，]+", text):
        line = line.strip()
        if not line:
            continue
        if "/" in line:
            site = _site_of_url(c, line if "://" in line else "https://" + line)
            key = get_site(site).key_from_url(line) if site else None
        else:
            site, key = default_site, line.lower()
        if not site or not key or not _SLUG_RE.fullmatch(key):
            raise ValueError(f"无法识别的影片：{line}")
        out.append((site, key))
    return out


@router.post("/jobs")
async def create_job(body: JobCreate, request: Request):
    e = _ctx(request).engine
    try:
        lib_id = body.library_id or DEFAULT_LIBRARY_ID
        if body.kind == "list":
            job_id = await e.create_crawl(body.source, site=body.site, sort=body.sort, start_page=body.start_page,
                                          end_page=body.end_page, detail=body.detail, library_id=lib_id)
        elif body.kind == "videos":
            job_id = await e.create_videos(_parse_videos(_ctx(request), body.urls, body.site), library_id=lib_id)
        elif body.kind == "backfill":
            job_id = await e.create_backfill()
        elif body.kind == "probe":
            job_id = await e.create_probe(body.site, body.library_id or None)
        else:
            job_id = await e.create_rewrite(body.library_id)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    return {"id": job_id}


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 50, offset: int = 0):
    return await _ctx(request).db.list_jobs(limit=min(limit, 200), offset=offset)


@router.get("/jobs/{job_id}/tasks")
async def job_tasks(job_id: int, request: Request, status: str = "", limit: int = 100, offset: int = 0):
    return await _ctx(request).db.list_tasks(job_id, status, min(limit, 500), offset)


@router.post("/jobs/{job_id}/{action}")
async def job_action(job_id: int, action: Literal["pause", "resume", "cancel", "retry"], request: Request):
    try:
        await _ctx(request).engine.set_job_status(job_id, action)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    return {"ok": True}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: int, request: Request):
    db = _ctx(request).db
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job["status"] in ("running", "paused"):
        raise HTTPException(400, "先取消任务再删除")
    await db.delete_job(job_id)
    return {"ok": True}


@router.get("/snapshots/{name}")
async def snapshot(name: str, request: Request):
    p = snapshot_path(_ctx(request).boot, name)
    if p is None:
        raise HTTPException(404)
    return FileResponse(p, media_type="text/plain; charset=utf-8")


# ---- 影片 ----


def _source_view(c, src: dict) -> dict:
    site = SITES.get(src["site"])
    out = dict(src)
    out["label"] = site.label if site else src["site"]
    out["direct"] = bool(site and site.stream.direct)
    out["expires_stream"] = bool(site and site.stream.expires)
    out["cooldown_until"] = source_cooldown(src)
    if site:
        domains = c.store.current.site(src["site"]).domains or site.default_domains
        out["page_url"] = domains[0] + site.detail_path(src["key"]) if domains else ""
    return out


def _video_view(c, v: dict, sources: list[dict] | None = None) -> dict:
    v = dict(v)
    v["play_url"] = c.writer.play_url(v)
    ranked = c.resolver.rank(sources or [])
    order = {s["id"]: i for i, s in enumerate(ranked)}
    v["sources"] = sorted((_source_view(c, s) for s in sources or []),
                          key=lambda s: (order.get(s["id"], len(order)), s["id"]))
    for s in v["sources"]:
        s["rank"] = order.get(s["id"])
    return v


@router.get("/videos")
async def list_videos(request: Request, q: str = "", filter: str = "", page: int = 1, size: int = 50,
                      library_id: int | None = None):
    c = _ctx(request)
    size = max(1, min(size, 200))
    items, total = await c.db.search_videos(q, filter, (max(page, 1) - 1) * size, size, library_id)
    ids = [v["id"] for v in items]
    outputs = await c.db.outputs_for(ids)
    srcs = await c.db.sources_for(ids)
    views = []
    for v in items:
        view = _video_view(c, v, srcs.get(v["id"], []))
        view["outputs"] = outputs.get(v["id"], [])
        views.append(view)
    return {"items": views, "total": total, "page": page, "size": size}


@router.post("/videos/{slug}/refresh")
async def refresh_video(slug: str, request: Request):
    """重抓这部影片每个可用源的详情。"""
    c = _ctx(request)
    try:
        v = await c.engine.refresh_video(slug.lower())
    except (NotFound, VideoGone):
        raise HTTPException(404, "站点上已不存在该影片") from None
    except Blocked as err:
        raise HTTPException(503, str(err)) from None
    except (FetchError, ParseError) as err:
        raise HTTPException(502, str(err)) from None
    view = _video_view(c, v, await c.db.get_sources(v["id"]))
    view["outputs"] = await c.db.get_outputs(v["id"])
    return view


@router.post("/videos/{slug}/probe")
async def probe_video(slug: str, request: Request):
    """到每个启用、还没有这部影片源的站点按番号找一次。"""
    c = _ctx(request)
    try:
        v = await c.engine.probe_video(slug.lower())
    except NotFound:
        raise HTTPException(404, "影片不存在") from None
    except Blocked as err:
        raise HTTPException(503, str(err)) from None
    except (FetchError, ParseError) as err:
        raise HTTPException(502, str(err)) from None
    view = _video_view(c, v, await c.db.get_sources(v["id"]))
    view["outputs"] = await c.db.get_outputs(v["id"])
    return view


# ---- 输出库 ----


class LibraryBody(BaseModel):
    name: str
    dir: str
    path_template: str = ""
    rule: dict | None = None
    external_dir: str = ""
    sources: list[int] = []
    excludes: list[int] = []


@router.get("/libraries")
async def list_libraries(request: Request):
    c = _ctx(request)
    libs = await c.db.list_libraries()
    for lib in libs:
        lib["root"] = str(c.writer.library_root(lib))
        lib["external_root"] = str(c.writer.external_root(lib) or "")
        lib["rule_text"] = describe_rule(lib["rule"])
    return libs


@router.post("/libraries")
async def create_library(body: LibraryBody, request: Request):
    try:
        return await _ctx(request).engine.create_library(body.name, body.dir, body.path_template, body.rule,
                                                         body.external_dir, body.sources, body.excludes)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(400, f"库名「{body.name}」已存在") from None


@router.put("/libraries/{lib_id}")
async def update_library(lib_id: int, body: LibraryBody, request: Request):
    try:
        return await _ctx(request).engine.update_library(lib_id, body.name, body.dir, body.path_template, body.rule,
                                                         body.external_dir, body.sources, body.excludes)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(400, f"库名「{body.name}」已存在") from None


@router.post("/libraries/{lib_id}/reclassify")
async def reclassify_library(lib_id: int, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.create_reclassify(lib_id)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


@router.post("/libraries/{lib_id}/locate")
async def locate_library(lib_id: int, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.strm.create_locate(lib_id)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


@router.get("/facets")
async def facets(request: Request):
    return await _ctx(request).db.facets()


@router.delete("/libraries/{lib_id}")
async def delete_library(lib_id: int, request: Request, delete_files: bool = False):
    try:
        return {"job_id": await _ctx(request).engine.delete_library(lib_id, delete_files)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


# ---- 订阅 ----


class SubscriptionBody(BaseModel):
    name: str
    site: str = "jable"
    source: str
    sort: str = ""
    library_id: int = DEFAULT_LIBRARY_ID
    detail: bool = True
    interval: int = Field(60, ge=0)
    stop_after_known: int = Field(48, ge=1)
    max_pages: int = Field(20, ge=1)
    enabled: bool = True
    initial_full: bool = True  # 仅新建时有效：false 表示跳过首轮全量，只跟进以后的更新


def _subscription_fields(c, body: SubscriptionBody) -> dict:
    if not body.name.strip():
        raise ValueError("订阅名称不能为空")
    if body.library_id not in c.engine.libs:
        raise ValueError(f"输出库 #{body.library_id} 不存在")
    site = get_site(body.site)
    return {
        "name": body.name.strip(),
        "site": site.name,
        "source": site.normalize_source(body.source),
        "sort": body.sort,
        "library_id": body.library_id,
        "detail": int(body.detail),
        "interval": body.interval,
        "stop_after_known": body.stop_after_known,
        "max_pages": body.max_pages,
        "enabled": int(body.enabled),
    }


@router.get("/subscriptions")
async def list_subscriptions(request: Request):
    return await _ctx(request).db.list_subscriptions()


@router.post("/subscriptions")
async def create_subscription(body: SubscriptionBody, request: Request):
    c = _ctx(request)
    try:
        fields = _subscription_fields(c, body)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    sub_id = await c.db.create_subscription(**fields, initialized=int(not body.initial_full))
    return {"id": sub_id}


@router.put("/subscriptions/{sub_id}")
async def update_subscription(sub_id: int, body: SubscriptionBody, request: Request):
    c = _ctx(request)
    if await c.db.get_subscription(sub_id) is None:
        raise HTTPException(404, "订阅不存在")
    try:
        await c.db.update_subscription(sub_id, **_subscription_fields(c, body))
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    return {"ok": True}


@router.post("/subscriptions/{sub_id}/initialized")
async def mark_subscription_initialized(sub_id: int, request: Request):
    """不跑首轮全量，直接改为定时增量（库里已经用别的任务抓全了）。"""
    c = _ctx(request)
    if await c.db.get_subscription(sub_id) is None:
        raise HTTPException(404, "订阅不存在")
    await c.db.update_subscription(sub_id, initialized=1)
    return {"ok": True}


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int, request: Request):
    c = _ctx(request)
    if await c.db.subscription_active_job(sub_id):
        raise HTTPException(400, "订阅有任务在执行，先取消任务")
    await c.db.delete_subscription(sub_id)
    return {"ok": True}


@router.post("/subscriptions/{sub_id}/run")
async def run_subscription(sub_id: int, request: Request, mode: Literal["auto", "full", "incremental"] = "auto"):
    try:
        return {"job_id": await _ctx(request).engine.run_subscription(sub_id, mode)}
    except KeyError:
        raise HTTPException(404, "订阅不存在") from None
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


# ---- strm 扫描 / 纳管 / 改前缀 ----


class ScanBody(BaseModel):
    dir: str = ""


class AdoptBody(BaseModel):
    scan_id: int
    library_id: int | None = None
    fetch_missing: bool = True
    kinds: list[str] = ["ours", "cdn"]
    prefix: str = ""


class PrefixBody(BaseModel):
    scan_id: int
    old: str
    new: str


async def _scan_job(c, scan_id: int) -> dict:
    job = await c.db.get_job(scan_id)
    if job is None or job["kind"] != "scan":
        raise HTTPException(404, "扫描记录不存在")
    return job


@router.post("/strm/scan")
async def strm_scan(body: ScanBody, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.strm.create_scan(body.dir)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


@router.get("/strm/scans")
async def strm_scans(request: Request):
    jobs = await _ctx(request).db.list_jobs(limit=200)
    return [j for j in jobs if j["kind"] == "scan"]


@router.get("/strm/scans/{scan_id}/summary")
async def strm_scan_summary(scan_id: int, request: Request):
    c = _ctx(request)
    job = await _scan_job(c, scan_id)
    summary = await c.db.strm_summary(scan_id)
    summary["job"] = job
    summary["public_base_url"] = c.store.public_base_url
    return summary


@router.get("/strm/scans/{scan_id}/files")
async def strm_scan_files(scan_id: int, request: Request, kind: str = "", managed: str = "", prefix: str = "",
                          q: str = "", page: int = 1, size: int = 50):
    size = max(1, min(size, 200))
    items, total = await _ctx(request).db.list_strm_files(scan_id, kind, managed, prefix, q,
                                                          (max(page, 1) - 1) * size, size)
    return {"items": items, "total": total}


@router.get("/strm/scans/{scan_id}/missing")
async def strm_scan_missing(scan_id: int, request: Request, page: int = 1, size: int = 50):
    c = _ctx(request)
    job = await _scan_job(c, scan_id)
    size = max(1, min(size, 200))
    items, total = await c.db.missing_outputs(job["params"]["dir"] + os.sep, (max(page, 1) - 1) * size, size)
    return {"items": items, "total": total}


@router.post("/strm/adopt")
async def strm_adopt(body: AdoptBody, request: Request):
    try:
        job_id = await _ctx(request).engine.strm.create_adopt(
            body.scan_id, library_id=body.library_id or None, fetch_missing=body.fetch_missing,
            kinds=tuple(body.kinds), prefix=body.prefix)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    return {"job_id": job_id}


@router.post("/strm/prefix/preview")
async def strm_prefix_preview(body: PrefixBody, request: Request):
    try:
        return await _ctx(request).engine.strm.preview_prefix(body.scan_id, body.old, body.new)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


@router.post("/strm/prefix/apply")
async def strm_prefix_apply(body: PrefixBody, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.strm.create_prefix(body.scan_id, body.old, body.new)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


@router.get("/strm/changes")
async def strm_changes(request: Request):
    return await _ctx(request).db.list_change_sets()


@router.post("/strm/changes/{change_set}/revert")
async def strm_revert(change_set: int, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.strm.create_revert(change_set)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


# ---- 设置 ----


@router.get("/settings")
async def get_settings(request: Request):
    c = _ctx(request)
    return {
        "values": c.store.current.model_dump(),
        "schema": Settings.model_json_schema()["properties"],
        "effective": {"output_dir": str(c.store.output_dir), "public_base_url": c.store.public_base_url},
    }


@router.put("/settings")
async def put_settings(patch: dict, request: Request):
    c = _ctx(request)
    try:
        new = await c.store.update(patch)
    except ValidationError as err:
        msgs = [f"{'.'.join(str(x) for x in e['loc'])}：{e['msg']}" for e in err.errors()]
        raise HTTPException(422, "；".join(msgs)) from None
    c.engine.notify()
    return {"values": new.model_dump(),
            "effective": {"output_dir": str(c.store.output_dir), "public_base_url": c.store.public_base_url}}


@router.get("/meta")
async def meta():
    return {"sites": {name: {"label": s.label, "presets": s.presets, "sorts": s.sorts, "default_sort": s.default_sort,
                             "hint": s.source_hint, "direct": s.stream.direct,
                             "ip_bound": s.stream.ip_bound}
                      for name, s in SITES.items()}}


# ---- 日志 ----


@router.get("/logs")
async def logs(after: int = 0, limit: int = 500):
    return ring.since(after, min(limit, 2000))


@router.get("/logs/stream")
async def logs_stream(request: Request, after: int = 0):
    async def gen():
        q = ring.subscribe()
        try:
            for item in ring.since(after, 300):
                yield f"id: {item['id']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            while not await request.is_disconnected():
                try:
                    item = await asyncio.wait_for(q.get(), 15)
                    yield f"id: {item['id']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            ring.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
