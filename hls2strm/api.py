"""Web 控制台的 JSON API。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from . import auth as auth_module
from .auth import COOKIE, REMEMBER_TTL, SESSION_TTL
from .config import Settings
from .db import DEFAULT_LIBRARY_ID, FACET_FIELDS, VIDEO_SORTS, VideoQuery, source_cooldown
from .engine import snapshot_path
from .fetcher import Blocked, FetchError, NotFound, ping_solver
from .observability import get_level, ring, set_level
from .parser import ParseError, VideoGone
from .rules import describe_rule
from .sites import SITES, get_site

log = logging.getLogger(__name__)
_basic = HTTPBasic(auto_error=False)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


def require_auth(request: Request, cred: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """登录页给的会话 cookie，或者 HTTP Basic（脚本、curl -u 用）。
    401 不带 WWW-Authenticate：浏览器不弹自己的登录框，由页面显示登录页。"""
    auth = request.app.state.ctx.auth
    if not auth.required or auth.verify(request.cookies.get(COOKIE)):
        return
    if cred is not None and auth.check_password(cred.username, cred.password):
        return
    raise HTTPException(401, "需要登录")


router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
public = APIRouter(prefix="/api")  # 不用登录的：登录、退出、查登录状态


class LoginBody(BaseModel):
    username: str
    password: str
    remember: bool = True


@public.get("/session")
async def session(request: Request):
    """要不要登录、当前登录的是谁（没登录为 null）。"""
    auth = _ctx(request).auth
    if not auth.required:
        return {"required": False, "user": None}
    return {"required": True, "user": auth.verify(request.cookies.get(COOKIE))}


@public.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    auth = _ctx(request).auth
    ip = request.client.host if request.client else "?"
    if not auth.required:
        return {"user": None}
    if wait := auth.blocked_for(ip):
        raise HTTPException(429, f"密码输错次数太多，{wait // 60 + 1} 分钟后再试")
    if not auth.check_password(body.username.strip(), body.password):
        auth.failed(ip)
        log.warning("登录失败：用户名 %s（来自 %s）", body.username.strip()[:40], ip)
        await asyncio.sleep(auth_module.FAILURE_DELAY)
        raise HTTPException(401, "用户名或密码不对")
    auth.succeeded(ip)
    ttl = REMEMBER_TTL if body.remember else SESSION_TTL
    response.set_cookie(COOKIE, auth.issue(ttl), max_age=ttl if body.remember else None, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")
    log.info("登录：%s（来自 %s）", auth.boot.ui_user, ip)
    return {"user": auth.boot.ui_user}


@public.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


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
        "missing": sum(c.engine.missing.values()),
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
    kind: Literal["list", "videos", "backfill", "rewrite", "probe", "verify", "quality"]
    site: str = "jable"
    source: str = ""
    sort: str = ""
    start_page: int = 1
    end_page: int = 0
    detail: bool | None = None  # list：抓详情；verify：没详情的排队抓详情（要联网）
    urls: str = ""
    library_id: int | None = None
    repair: bool = True  # verify：发现问题就补回；关掉只检查
    covers: bool = True  # verify：补封面（要下载）
    force_external: bool = False  # verify：外部整理目录不存在或是空的也把找不到的写回收件目录


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
        elif body.kind == "verify":
            job_id = await e.create_verify(body.library_id or None, repair=body.repair, covers=body.covers,
                                           force_external=body.force_external, details=bool(body.detail))
        elif body.kind == "quality":
            job_id = await e.create_quality(body.library_id or None)
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


def _line_view(c, site, ln: dict) -> dict:
    from .sites.hosts import HOST_LABELS

    cfg = c.store.current.site(site.name).line(ln["line"])
    t = site.line_traits(ln["line"], ln["host"])
    spec = site.line_specs.get(ln["line"])
    return {**ln, "host_label": HOST_LABELS.get(ln["host"] or (spec.host if spec else ""), ln["host"]),
            "enabled": cfg.enabled, "supported": spec is None or spec.supported,
            "direct": t.direct and not cfg.proxy, "ip_bound": t.ip_bound, "expires_stream": t.expires,
            "cooldown_until": source_cooldown(ln)}


def _source_view(c, src: dict, lines: list[dict] | None = None) -> dict:
    site = SITES.get(src["site"])
    out = dict(src)
    out["label"] = site.label if site else src["site"]
    out["direct"] = bool(site and site.stream.direct)
    out["expires_stream"] = bool(site and site.stream.expires)
    out["cooldown_until"] = source_cooldown(src)
    if site and site.multi_line:
        cfg = c.store.current.site(site.name)
        out["lines"] = sorted((_line_view(c, site, ln) for ln in lines or []),
                              key=lambda ln: (not ln["enabled"], cfg.line_rank(ln["line"])))
        traits = site.line_traits(src["line"]) if src["line"] else None
        if traits is not None:
            out["direct"], out["expires_stream"] = traits.direct, traits.expires
    if site:
        domains = c.store.current.site(src["site"]).domains or site.default_domains
        out["page_url"] = domains[0] + site.detail_path(src["key"]) if domains else ""
    return out


def _video_view(c, v: dict, sources: list[dict] | None = None, lines: dict[int, list[dict]] | None = None) -> dict:
    v = dict(v)
    v["play_url"] = c.writer.play_url(v)
    ranked = c.resolver.rank(sources or [])
    order = {s["id"]: i for i, s in enumerate(ranked)}
    v["sources"] = sorted((_source_view(c, s, (lines or {}).get(s["id"])) for s in sources or []),
                          key=lambda s: (order.get(s["id"], len(order)), s["id"]))
    for s in v["sources"]:
        s["rank"] = order.get(s["id"])
    return v


@router.get("/videos")
async def list_videos(
    request: Request,
    q: str = "",
    filter: Literal["", "active", "no_detail", "no_output", "gone"] = "",
    library_id: int | None = None,
    has_site: list[str] = Query([]),
    lacks_site: list[str] = Query([]),
    sources: Literal["", "single", "multi", "failing", "none"] = "",
    subtitle: Literal["", "zh", "en", "none"] = "",
    uncensored: bool | None = None,
    model: list[str] = Query([]),
    category: list[str] = Query([]),
    tag: list[str] = Query([]),
    maker: list[str] = Query([]),
    quality: list[str] = Query([]),
    release_from: str = "",
    release_to: str = "",
    added_from: int | None = None,
    added_to: int | None = None,
    duration_min: int | None = None,
    duration_max: int | None = None,
    sort: str = "created",
    order: Literal["asc", "desc"] = "desc",
    page: int = 1,
    size: int = 50,
):
    """影片库：筛选条件见 VideoQuery；列表参数可以重复（has_site=jable&has_site=missav），组内任一满足。"""
    c = _ctx(request)
    size = max(1, min(size, 200))
    if sort not in VIDEO_SORTS:
        raise HTTPException(422, f"不支持的排序：{sort}")
    query = VideoQuery(
        q=q, status=filter, library_id=library_id, has_site=has_site, lacks_site=lacks_site, sources=sources,
        subtitle=subtitle, uncensored=uncensored, models=model, categories=category, tags=tag, makers=maker,
        quality=quality, release_from=release_from, release_to=release_to, added_from=added_from,
        added_to=added_to, duration_min=duration_min, duration_max=duration_max, sort=sort, desc=order == "desc",
    )
    items, total = await c.db.search_videos(query, (max(page, 1) - 1) * size, size)
    ids = [v["id"] for v in items]
    outputs = await c.db.outputs_for(ids)
    srcs = await c.db.sources_for(ids)
    lines = await c.db.lines_for([s["id"] for ss in srcs.values() for s in ss])
    views = []
    for v in items:
        view = _video_view(c, v, srcs.get(v["id"], []), lines)
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
    return await _full_view(c, v)


class VideoBatch(BaseModel):
    action: Literal["probe", "refresh", "add", "remove"]
    ids: list[int] = Field(min_length=1, max_length=500)
    library_id: int | None = None  # add / remove：哪个输出库


@router.post("/videos/batch")
async def videos_batch(body: VideoBatch, request: Request):
    """影片库多选后的批量操作：probe、refresh 排成任务（按站点限速）；add、remove 当场改输出库。"""
    e = _ctx(request).engine
    try:
        if body.action == "probe":
            return {"job_id": await e.create_probe_videos(body.ids)}
        if body.action == "refresh":
            return {"job_id": await e.create_refresh(body.ids)}
        if not body.library_id:
            raise ValueError("要选一个输出库")
        if body.action == "add":
            return {"added": await e.add_to_library(body.ids, body.library_id)}
        return {"removed": await e.remove_from_library(body.ids, body.library_id)}
    except ValueError as err:
        raise HTTPException(400, str(err)) from None


async def _full_view(c, v: dict) -> dict:
    sources = await c.db.get_sources(v["id"])
    view = _video_view(c, v, sources, await c.db.lines_for([s["id"] for s in sources]))
    view["outputs"] = await c.db.get_outputs(v["id"])
    return view


@router.post("/videos/{slug}/probe")
async def probe_video(slug: str, request: Request):
    """到每个启用、还没有这部影片源的站点按番号找一次。"""
    c = _ctx(request)
    try:
        v, results = await c.engine.probe_video(slug.lower())
    except NotFound:
        raise HTTPException(404, "影片不存在") from None
    view = await _full_view(c, v)
    view["probe"] = results
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
        lib["missing"] = c.engine.missing.get(lib["id"], 0)
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
async def facets(request: Request, field: str = "", q: str = "", limit: int = 300):
    """库里已有的分类、标签、女优、发行商、画质及影片数；field 只取一种，q 按名称或 id 筛。"""
    db = _ctx(request).db
    limit = max(1, min(limit, 1000))
    if not field:
        return await db.facets(limit)
    if field not in (*FACET_FIELDS, "makers", "quality"):
        raise HTTPException(422, f"没有这种候选：{field}")
    return await db.facet(field, q, limit)


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


def _line_specs(site) -> list[dict]:
    from .sites.hosts import HOST_LABELS, HOST_TRAITS

    out = []
    for name, spec in site.line_specs.items():
        t = HOST_TRAITS.get(spec.host)
        out.append({"name": name, "host": spec.host, "host_label": HOST_LABELS.get(spec.host, ""), "note": spec.note,
                    "supported": spec.supported, "direct": bool(t and t.direct), "ip_bound": bool(t and t.ip_bound)})
    return out


@router.get("/meta")
async def meta():
    return {"sites": {name: {"label": s.label, "presets": s.presets, "sorts": s.sorts, "default_sort": s.default_sort,
                             "hint": s.source_hint, "direct": s.stream.direct, "ip_bound": s.stream.ip_bound,
                             "lines": _line_specs(s)}
                      for name, s in SITES.items()}}


# ---- 日志 ----


@router.get("/logs")
async def logs(after: int = 0, limit: int = 500):
    return ring.since(after, min(limit, 2000))


class LogLevel(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING"]


@router.get("/logs/level")
async def log_level(request: Request):
    return {"level": get_level(), "default": _ctx(request).boot.log_level}


@router.put("/logs/level")
async def set_log_level(body: LogLevel, request: Request):
    """现场切换日志级别（DEBUG 会记下每个请求），不保存，重启后回到 HLS2STRM_LOG_LEVEL。"""
    set_level(body.level)
    log.warning("日志级别改为 %s（重启后回到 %s）", body.level, _ctx(request).boot.log_level)
    return {"level": get_level(), "default": _ctx(request).boot.log_level}


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
