"""任务引擎：从 SQLite 领取子任务执行，失败按策略重试；支持暂停/恢复、断点续跑与定时增量。

job 种类：
  crawl        翻某个列表来源（全站 = 最新更新的全部页）
  incremental  从最新更新第 1 页往后翻，连续遇到已入库影片就停
  videos       抓指定影片的详情（手动添加、补全缺失详情）
  rewrite      按当前设置重写全部 strm / nfo
子任务种类：list（目标=页码）、detail（目标=slug）、rewrite（目标=all）
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from . import sources
from .config import BootConfig, SettingsStore
from .db import Database
from .fetcher import Blocked, FetchError, Fetcher, NotFound
from .observability import Metrics
from .parser import ParseError, VideoGone, m3u8_duration, parse_detail, parse_list
from .writer import OutputWriter

log = logging.getLogger(__name__)

PRIORITY_USER = 20
PRIORITY_LIST = 10
PRIORITY_DETAIL = 0
MAX_RETRY_DELAY = 7200
MAX_SNAPSHOTS = 200


class Engine:
    def __init__(
        self,
        db: Database,
        fetcher: Fetcher,
        writer: OutputWriter,
        store: SettingsStore,
        metrics: Metrics,
        boot: BootConfig,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.writer = writer
        self.store = store
        self.metrics = metrics
        self.snapshot_dir = boot.data_dir / "snapshots"
        self.paused = False
        self.blocked_until = 0.0
        self.running: dict[int, dict] = {}
        self._workers: list[asyncio.Task] = []
        self._scheduler: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # ---- 生命周期 ----

    async def start(self) -> None:
        n = await self.db.reset_running_tasks()
        if n:
            log.info("上次退出时有 %d 个子任务未完成，已放回队列", n)
        for job in await self.db.list_jobs(limit=1000):
            if job["status"] == "running":
                await self._maybe_finish_job(job["id"])
        self._resize_workers()
        self.store.on_change(lambda old, new: self._resize_workers())
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="scheduler")

    async def stop(self) -> None:
        self._stopping = True
        tasks = [t for t in self._workers if not t.done()]
        if self._scheduler:
            tasks.append(self._scheduler)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _resize_workers(self) -> None:
        self._workers = [t for t in self._workers if not t.done()]
        want = self.store.current.concurrency
        for i in range(len(self._workers), want):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"worker-{i}"))

    def notify(self) -> None:
        self._wake.set()

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass
        self._wake.clear()

    def status(self) -> dict:
        return {
            "paused": self.paused,
            "blocked_for": max(0, int(self.blocked_until - time.time())),
            "workers": len([t for t in self._workers if not t.done()]),
            "running": list(self.running.values()),
        }

    def pause(self) -> None:
        self.paused = True
        log.info("引擎已暂停")

    def resume(self) -> None:
        self.paused = False
        self.blocked_until = 0
        self.notify()
        log.info("引擎已恢复")

    # ---- worker ----

    async def _worker(self, idx: int) -> None:
        while not self._stopping:
            if idx >= self.store.current.concurrency:
                return
            if self.paused:
                await self._idle(2)
                continue
            wait = self.blocked_until - time.time()
            if wait > 0:
                await self._idle(min(wait, 5))
                continue
            task = await self.db.claim_task()
            if task is None:
                await self._idle(2)
                continue
            await self._run(task)

    async def _run(self, task: dict) -> None:
        tid = task["id"]
        t0 = time.monotonic()
        self.running[tid] = {"id": tid, "job_id": task["job_id"], "kind": task["kind"], "target": task["target"],
                             "attempt": task["attempts"], "started": time.time()}
        finished = False
        try:
            job = await self.db.get_job(task["job_id"])
            if job is None or job["status"] != "running":
                await self.db.finish_task(tid, "pending", refund_attempt=True)
                return
            handler = {"list": self._do_list, "detail": self._do_detail, "rewrite": self._do_rewrite}[task["kind"]]
            await handler(job, task)
            await self.db.finish_task(tid, "done", duration_ms=int((time.monotonic() - t0) * 1000))
            self.metrics.inc(f"task_{task['kind']}_done")
            finished = True
        except asyncio.CancelledError:
            await self.db.finish_task(tid, "pending", "进程退出时中断", refund_attempt=True)
            raise
        except (NotFound, VideoGone) as e:
            await self.db.finish_task(tid, "gone", f"已下架：{e}")
            if task["kind"] == "detail":
                await self.db.mark_gone(task["target"])
            self.metrics.inc("task_gone")
            log.info("%s %s 已下架：%s", task["kind"], task["target"], e)
            finished = True
        except Blocked as e:
            first = self.blocked_until < time.time()
            self.blocked_until = max(self.blocked_until, time.time() + e.retry_after)
            await self.db.finish_task(tid, "pending", f"被拦截：{e}",
                                      next_run_at=int(self.blocked_until), refund_attempt=True)
            if first:
                log.warning("%s，暂停抓取 %d 秒后自动重试", e, int(e.retry_after))
        except ParseError as e:
            snap = self._save_snapshot(task, getattr(e, "html", ""))
            await self.db.finish_task(tid, "failed", f"解析失败：{e}" + (f"（快照 {snap}）" if snap else ""))
            self.metrics.inc("task_failed")
            log.error("%s %s 解析失败：%s %s", task["kind"], task["target"], e, snap or "")
            finished = True
        except Exception as e:
            finished = await self._retry_or_fail(task, e)
        finally:
            self.running.pop(tid, None)
        if finished:
            await self._maybe_finish_job(task["job_id"])

    async def _retry_or_fail(self, task: dict, e: Exception) -> bool:
        s = self.store.current
        msg = f"{type(e).__name__}: {e}"
        if task["attempts"] >= s.max_attempts:
            await self.db.finish_task(task["id"], "failed", msg)
            self.metrics.inc("task_failed")
            log.error("%s %s 第 %d 次失败，放弃：%s", task["kind"], task["target"], task["attempts"], msg,
                      exc_info=not isinstance(e, FetchError))
            return True
        delay = min(MAX_RETRY_DELAY, s.retry_base_delay * 4 ** (task["attempts"] - 1))
        await self.db.finish_task(task["id"], "pending", msg, next_run_at=int(time.time() + delay))
        self.metrics.inc("task_retry")
        log.warning("%s %s 第 %d 次失败，%d 秒后重试：%s", task["kind"], task["target"], task["attempts"], delay, msg)
        return False

    def _save_snapshot(self, task: dict, html: str) -> str:
        if not html:
            return ""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        name = f"{task['id']}_{task['kind']}_{task['target'].replace('/', '_')[:60]}.html"
        (self.snapshot_dir / name).write_text(html, encoding="utf-8")
        snaps = sorted(self.snapshot_dir.glob("*.html"), key=lambda p: p.stat().st_mtime)
        for old in snaps[:-MAX_SNAPSHOTS]:
            old.unlink(missing_ok=True)
        return name

    async def _maybe_finish_job(self, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None or job["status"] != "running":
            return
        if await self.db.open_task_count(job_id):
            return
        counts = await self.db.task_counts(job_id)
        await self.db.update_job(job_id, status="done", finished_at=int(time.time()))
        log.info("任务 #%d「%s」完成：%s", job_id, job["name"],
                 "，".join(f"{k} {v}" for k, v in sorted(counts.items())))

    # ---- 子任务处理 ----

    async def _output(self, v: dict, *, cover: bool) -> None:
        strm = await asyncio.to_thread(self.writer.write, v)
        cover_done = await self.writer.write_cover(self.fetcher, v, strm) if cover else None
        await self.db.set_output(v["id"], str(strm), cover_done)

    async def _do_list(self, job: dict, task: dict) -> None:
        p = job["params"]
        page = int(task["target"])
        url = sources.page_url(p["source"], page, p.get("sort", ""), p.get("block_id"))
        pg = await self.fetcher.get_page(url)
        lp = parse_list(pg.html)
        if not lp.items and page == p["start_page"]:
            e = ParseError("列表为空，检查列表地址是否正确")
            e.html = pg.html
            raise e

        new_count = 0
        detail_slugs = []
        state = (await self.db.get_job(job["id"]))["state"]
        for it in lp.items:
            is_new = await self.db.upsert_list_item(it)
            v = await self.db.get_video(it.slug)
            if is_new or not v["strm_path"]:
                await self._output(v, cover=False)
            if p.get("detail", True) and v["detail_at"] is None:
                detail_slugs.append(it.slug)
            new_count += is_new
            state["known_streak"] = 0 if is_new else state.get("known_streak", 0) + 1
        if detail_slugs:
            await self.db.add_tasks(job["id"], "detail", detail_slugs, PRIORITY_DETAIL)
        self.metrics.inc("videos_new", new_count)

        last = lp.last_page or page
        end = min(p.get("end_page") or last, last)
        if p.get("incremental"):
            pages_done = page - p["start_page"] + 1
            if state["known_streak"] >= p["stop_after_known"]:
                log.info("增量：连续 %d 部已入库，停止翻页", state["known_streak"])
            elif page < end and pages_done < p["max_pages"]:
                await self.db.add_tasks(job["id"], "list", [page + 1], PRIORITY_LIST)
        elif page == p["start_page"] and not state.get("pages_enqueued"):
            n = await self.db.add_tasks(job["id"], "list", range(page + 1, end + 1), PRIORITY_LIST)
            state["pages_enqueued"] = True
            state["last_page"] = last
            if n:
                log.info("任务 #%d：共 %d 页，已排队第 %d-%d 页", job["id"], last, page + 1, end)
        await self.db.update_job(job["id"], state=state)
        log.info("列表 %s 第 %d/%d 页：%d 部，新增 %d，排队详情 %d",
                 p["source"], page, last, len(lp.items), new_count, len(detail_slugs))

    async def _do_detail(self, job: dict, task: dict) -> None:
        await self.fetch_detail(task["target"])

    async def fetch_detail(self, slug: str, *, priority: bool = False) -> dict:
        pg = await self.fetcher.get_page(f"/videos/{slug}/", priority=priority)
        try:
            d = parse_detail(pg.html, slug)
        except ParseError as e:
            e.html = pg.html
            raise
        await self.db.upsert_detail(d)
        v = await self.db.get_video(slug)
        if not v["duration"]:
            await self._fill_duration(v)
        await self._output(v, cover=True)
        self.metrics.inc("videos_detail")
        log.info("详情 %s：%s，女优 %s，%d 个标签", slug, v["release_date"] or "无日期",
                 "、".join(m["name"] for m in v["models"]) or "无", len(v["tags"]))
        return await self.db.get_video(slug)

    async def _fill_duration(self, v: dict) -> None:
        """没走过列表页的影片（指定影片任务）没有时长：从 m3u8 分片时长累加。"""
        try:
            duration = m3u8_duration((await self.fetcher.get_bytes(v["hls_url"])).decode("utf-8", "replace"))
        except Exception as e:
            log.info("%s 获取时长失败：%s", v["slug"], e)
            return
        if duration:
            await self.db.set_duration(v["id"], duration)
            v["duration"] = duration

    async def _do_rewrite(self, job: dict, task: dict) -> None:
        n = 0
        async for v in self.db.iter_videos():
            await self._output(v, cover=False)
            n += 1
            if n % 1000 == 0:
                log.info("重写输出：已完成 %d 部", n)
        log.info("重写输出完成：共 %d 部", n)

    # ---- 创建 job ----

    async def create_crawl(
        self,
        source: str,
        *,
        sort: str = "",
        start_page: int = 1,
        end_page: int = 0,
        detail: bool | None = None,
        name: str = "",
        incremental: bool = False,
        full: bool = False,
    ) -> int:
        source = sources.normalize_source(source)
        s = self.store.current
        params = {
            "source": source,
            "sort": sort,
            "start_page": max(1, start_page),
            "end_page": max(0, end_page),
            "detail": s.fetch_detail if detail is None else detail,
        }
        if full:
            params["full"] = True
        if incremental:
            params |= {"incremental": True, "stop_after_known": s.incremental_stop_after_known,
                       "max_pages": s.incremental_max_pages}
        kind = "incremental" if incremental else "crawl"
        if not name:
            pages = f"第 {params['start_page']}-{end_page} 页" if end_page else f"第 {params['start_page']} 页起"
            name = f"{'增量' if incremental else '列表'} {source} {pages}"
        job_id = await self.db.create_job(kind, name, params)
        await self.db.add_tasks(job_id, "list", [params["start_page"]], PRIORITY_LIST)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def create_videos(self, slugs: list[str], *, name: str = "", priority: int = PRIORITY_USER) -> int:
        slugs = list(dict.fromkeys(s.lower() for s in slugs))
        if not slugs:
            raise ValueError("没有可抓取的影片")
        name = name or f"影片 {slugs[0]}" + (f" 等 {len(slugs)} 部" if len(slugs) > 1 else "")
        job_id = await self.db.create_job("videos", name, {"count": len(slugs)})
        await self.db.add_tasks(job_id, "detail", slugs, priority)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def create_backfill(self) -> int:
        slugs = await self.db.slugs_missing_detail()
        return await self.create_videos(slugs, name=f"补全缺失详情（{len(slugs)} 部）", priority=PRIORITY_DETAIL)

    async def create_rewrite(self) -> int:
        job_id = await self.db.create_job("rewrite", "重写全部输出", {})
        await self.db.add_tasks(job_id, "rewrite", ["all"], PRIORITY_USER)
        log.info("新建任务 #%d「重写全部输出」", job_id)
        self.notify()
        return job_id

    # ---- job 控制 ----

    async def set_job_status(self, job_id: int, action: str) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if action == "pause" and job["status"] == "running":
            await self.db.update_job(job_id, status="paused")
        elif action == "resume" and job["status"] == "paused":
            await self.db.update_job(job_id, status="running")
        elif action == "cancel" and job["status"] in ("running", "paused"):
            await self.db.cancel_open_tasks(job_id)
            await self.db.update_job(job_id, status="cancelled", finished_at=int(time.time()))
        elif action == "retry":
            n = await self.db.retry_failed_tasks(job_id)
            await self.db.update_job(job_id, status="running", finished_at=None)
            log.info("任务 #%d：%d 个失败子任务重新排队", job_id, n)
            await self._maybe_finish_job(job_id)
        else:
            raise ValueError(f"任务当前状态为 {job['status']}，不能执行 {action}")
        log.info("任务 #%d：%s", job_id, action)
        self.notify()

    # ---- 定时增量 ----

    async def _schedule_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._maybe_incremental()
            except Exception:
                log.exception("定时增量调度出错")

    async def _maybe_incremental(self) -> None:
        s = self.store.current
        if s.incremental_interval <= 0 or self.paused:
            return
        if await self.db.active_job("incremental") or await self.db.active_job("crawl"):
            return
        if not await self.db.incremental_armed():
            return
        last = await self.db.last_job_time("incremental") or 0
        if time.time() - last >= s.incremental_interval * 60:
            await self.create_crawl(sources.LATEST, sort="post_date", incremental=True, name="定时增量：最新更新")


def snapshot_path(boot: BootConfig, name: str) -> Path | None:
    p = (boot.data_dir / "snapshots" / name).resolve()
    return p if p.parent == (boot.data_dir / "snapshots").resolve() and p.exists() else None
