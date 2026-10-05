"""Web 控制台的 JSON API。"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ValidationError

from . import __version__, sources
from .config import Settings
from .engine import snapshot_path
from .fetcher import Blocked, FetchError, NotFound
from .observability import ring
from .parser import ParseError, VideoGone, slug_from_url

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
        "domains": [d.to_dict() for d in c.fetcher.domains],
        "rate": {"limit": c.fetcher.limiter.limit, "current": round(c.fetcher.limiter.rate, 3)},
        "solver": c.store.current.solver_url,
        "videos": await c.db.video_stats(),
        "queue": await c.db.queue_stats(),
        "metrics": c.metrics.snapshot(),
        "failures": await c.db.recent_failures(8),
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
async def fetcher_reset(request: Request):
    c = _ctx(request)
    c.fetcher.reset_cooldowns()
    c.engine.blocked_until = 0
    c.engine.notify()
    return {"ok": True}


@router.post("/fetcher/test")
async def fetcher_test(request: Request):
    c = _ctx(request)
    result = await c.fetcher.test_domains()
    if any(r["ok"] for r in result):
        c.engine.blocked_until = 0
        c.engine.notify()
    return result


# ---- job ----


class JobCreate(BaseModel):
    kind: Literal["full", "incremental", "list", "videos", "backfill", "rewrite"]
    source: str = ""
    sort: str = ""
    start_page: int = 1
    end_page: int = 0
    detail: bool | None = None
    urls: str = ""


def _parse_slugs(text: str) -> list[str]:
    out = []
    for line in re.split(r"[\s,，]+", text):
        line = line.strip()
        if not line:
            continue
        slug = slug_from_url(line) if "/" in line else line.lower()
        if not slug or not _SLUG_RE.fullmatch(slug):
            raise ValueError(f"无法识别的影片：{line}")
        out.append(slug)
    return out


@router.post("/jobs")
async def create_job(body: JobCreate, request: Request):
    e = _ctx(request).engine
    try:
        if body.kind == "full":
            job_id = await e.create_crawl(sources.LATEST, sort="post_date", detail=body.detail, name="全站：最新更新",
                                          full=True)
        elif body.kind == "incremental":
            job_id = await e.create_crawl(sources.LATEST, sort="post_date", incremental=True, name="增量：最新更新")
        elif body.kind == "list":
            job_id = await e.create_crawl(body.source, sort=body.sort, start_page=body.start_page,
                                          end_page=body.end_page, detail=body.detail)
        elif body.kind == "videos":
            job_id = await e.create_videos(_parse_slugs(body.urls))
        elif body.kind == "backfill":
            job_id = await e.create_backfill()
        else:
            job_id = await e.create_rewrite()
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


def _video_view(c, v: dict) -> dict:
    v = dict(v)
    v["play_url"] = c.writer.play_url(v)
    v["hls_left"] = max(0, (v.get("hls_expires") or 0) - int(time.time()))
    return v


@router.get("/videos")
async def list_videos(request: Request, q: str = "", filter: str = "", page: int = 1, size: int = 50):
    c = _ctx(request)
    size = max(1, min(size, 200))
    items, total = await c.db.search_videos(q, filter, (max(page, 1) - 1) * size, size)
    return {"items": [_video_view(c, v) for v in items], "total": total, "page": page, "size": size}


async def _get_video(c, slug: str) -> dict:
    v = await c.db.get_video(slug)
    if v is None:
        raise HTTPException(404, "影片不存在")
    return v


@router.post("/videos/{slug}/refresh")
async def refresh_video(slug: str, request: Request):
    c = _ctx(request)
    try:
        v = await c.engine.fetch_detail(slug.lower(), priority=True)
    except (NotFound, VideoGone):
        await c.db.mark_gone(slug.lower())
        raise HTTPException(404, "站点上已不存在该影片") from None
    except Blocked as err:
        raise HTTPException(503, str(err)) from None
    except (FetchError, ParseError) as err:
        raise HTTPException(502, str(err)) from None
    return _video_view(c, v)


@router.post("/videos/{slug}/rewrite")
async def rewrite_video(slug: str, request: Request):
    c = _ctx(request)
    v = await _get_video(c, slug.lower())
    strm = await asyncio.to_thread(c.writer.write, v)
    await c.db.set_output(v["id"], str(strm))
    return {"strm_path": str(strm)}


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
    return {"presets": sources.PRESETS, "sorts": sources.SORTS}


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
