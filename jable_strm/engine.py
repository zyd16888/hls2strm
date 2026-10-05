"""任务引擎：从 SQLite 领取子任务执行，失败按策略重试；支持暂停/恢复、断点续跑；管理输出库与订阅。

job 种类：
  crawl        翻某个列表来源，输出到指定输出库（订阅的首轮全量也是它）
  incremental  从第 1 页往后翻，连续遇到库里已有的影片就停（订阅的定时增量）
  videos       抓指定影片的详情（手动添加、补全缺失详情）
  rewrite      按当前设置重写输出（可限定某个库），路径变化时搬动文件
  purge        删除输出库及其文件
子任务种类：list（目标=页码）、detail（目标=slug）、rewrite（目标=all）、purge（目标=库 id）
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from . import sources
from .config import BootConfig, SettingsStore, check_path_template
from .db import DEFAULT_LIBRARY_ID, Database
from .fetcher import Blocked, FetchError, Fetcher, NotFound
from .observability import Metrics
from .parser import ParseError, VideoGone, m3u8_duration, parse_detail, parse_list
from .rules import describe_rule, match_rule, normalize_rule
from .strm_manage import StrmManager
from .writer import OutputWriter

log = logging.getLogger(__name__)

PRIORITY_USER = 20
PRIORITY_LIST = 10
PRIORITY_DETAIL = 0
MAX_RETRY_DELAY = 7200
MAX_SNAPSHOTS = 200
DEFAULT_STOP_AFTER_KNOWN = 48
DEFAULT_MAX_PAGES = 20


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
        self.libs: dict[int, dict] = {}
        self.strm = StrmManager(self)
        self._workers: list[asyncio.Task] = []
        self._scheduler: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # ---- 生命周期 ----

    async def start(self) -> None:
        await self.reload_libraries()
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
            handler = {"list": self._do_list, "detail": self._do_detail, "rewrite": self._do_rewrite,
                       "purge": self._do_purge, "reclassify": self._do_reclassify,
                       "scan": self.strm.do_scan, "adopt": self.strm.do_adopt,
                       "prefix": self.strm.do_prefix, "revert": self.strm.do_revert}[task["kind"]]
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
        p = job["params"]
        if p.get("subscription_id") and not p.get("incremental"):
            await self.db.update_subscription(p["subscription_id"], initialized=1)
            log.info("订阅 #%d 首轮全量完成，之后按周期增量", p["subscription_id"])
        log.info("任务 #%d「%s」完成：%s", job_id, job["name"],
                 "，".join(f"{k} {v}" for k, v in sorted(counts.items())))

    # ---- 输出 ----

    async def reload_libraries(self) -> None:
        self.libs = {lib["id"]: lib for lib in await self.db.list_libraries()}

    def keep_dirs(self) -> frozenset[Path]:
        return frozenset(self.writer.library_root(lib) for lib in self.libs.values())

    def _library(self, library_id: int) -> dict:
        lib = self.libs.get(library_id)
        if lib is None:
            raise ValueError(f"输出库 #{library_id} 不存在")
        return lib

    async def _output_one(self, v: dict, library_id: int, *, cover: bool, old_strm: str | None = None) -> None:
        lib = self._library(library_id)
        if old_strm is None:
            out = await self.db.get_output(v["id"], library_id)
            old_strm = out["strm_path"] if out else ""
        strm = await asyncio.to_thread(self.writer.write, v, lib, old_strm, self.keep_dirs())
        cover_done = None
        if cover:
            siblings = [Path(o["strm_path"]) for o in await self.db.get_outputs(v["id"])
                        if o["library_id"] != library_id and o["strm_path"]]
            cover_done = await self.writer.write_cover(self.fetcher, v, strm, siblings)
        await self.db.set_output(v["id"], library_id, str(strm), cover_done)

    async def _output_all(self, v: dict, *, cover: bool) -> None:
        for out in await self.db.get_outputs(v["id"]):
            await self._output_one(v, out["library_id"], cover=cover, old_strm=out["strm_path"])

    # ---- 子任务处理 ----

    async def _do_list(self, job: dict, task: dict) -> None:
        p = job["params"]
        lib_id = p.get("library_id") or DEFAULT_LIBRARY_ID
        self._library(lib_id)
        page = int(task["target"])
        url = sources.page_url(p["source"], page, p.get("sort", ""), p.get("block_id"))
        pg = await self.fetcher.get_page(url)
        lp = parse_list(pg.html)
        if not lp.items and page == p["start_page"]:
            e = ParseError("列表为空，检查列表地址是否正确")
            e.html = pg.html
            raise e

        added_count = 0
        detail_slugs = []
        state = (await self.db.get_job(job["id"]))["state"]
        for it in lp.items:
            await self.db.upsert_list_item(it)
            v = await self.db.get_video(it.slug)
            added = await self.db.ensure_output(v["id"], lib_id)
            if added or not (await self.db.get_output(v["id"], lib_id))["strm_path"]:
                # 已有详情的影片（别的库抓过）直接带上 nfo 和封面
                await self._output_one(v, lib_id, cover=bool(v["detail_at"]))
            if p.get("detail", True) and v["detail_at"] is None:
                detail_slugs.append(it.slug)
            added_count += added
            state["known_streak"] = 0 if added else state.get("known_streak", 0) + 1
        if detail_slugs:
            await self.db.add_tasks(job["id"], "detail", detail_slugs, PRIORITY_DETAIL)
        self.metrics.inc("videos_new", added_count)

        last = lp.last_page or page
        end = min(p.get("end_page") or last, last)
        if p.get("incremental"):
            pages_done = page - p["start_page"] + 1
            if state["known_streak"] >= p["stop_after_known"]:
                log.info("增量：连续 %d 部已在库里，停止翻页", state["known_streak"])
            elif page < end and pages_done < p["max_pages"]:
                await self.db.add_tasks(job["id"], "list", [page + 1], PRIORITY_LIST)
        elif page == p["start_page"] and not state.get("pages_enqueued"):
            n = await self.db.add_tasks(job["id"], "list", range(page + 1, end + 1), PRIORITY_LIST)
            state["pages_enqueued"] = True
            state["last_page"] = last
            if n:
                log.info("任务 #%d：共 %d 页，已排队第 %d-%d 页", job["id"], last, page + 1, end)
        await self.db.update_job(job["id"], state=state)
        log.info("列表 %s 第 %d/%d 页 → 库「%s」：%d 部，新加入 %d，排队详情 %d",
                 p["source"], page, last, self.libs[lib_id]["name"], len(lp.items), added_count, len(detail_slugs))

    async def _do_detail(self, job: dict, task: dict) -> None:
        await self.fetch_detail(task["target"], library_id=job["params"].get("library_id"))

    async def fetch_detail(self, slug: str, *, library_id: int | None = None, priority: bool = False) -> dict:
        """抓详情、更新元数据；library_id 不为空时把影片加入该库。影片所在的每个库都会重写输出。"""
        if library_id:
            self._library(library_id)
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
        if library_id:
            await self.db.ensure_output(v["id"], library_id)
        await self._output_all(v, cover=True)
        await self.apply_rules(v)
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
        lib_id = job["params"].get("library_id")
        n = 0
        async for v, out in self.db.iter_outputs(lib_id):
            if v["status"] != "active" or out["library_id"] not in self.libs:
                continue
            await self._output_one(v, out["library_id"], cover=False, old_strm=out["strm_path"])
            n += 1
            if n % 1000 == 0:
                log.info("重写输出：已完成 %d 个", n)
        log.info("重写输出完成：共 %d 个", n)

    async def apply_rules(self, v: dict, only_library: int | None = None) -> tuple[int, int]:
        """按规则库调整影片归属：命中就加入（via=rule），不再命中就移除规则加入的输出。返回 (加入, 移除)。"""
        added = removed = 0
        for lib in list(self.libs.values()):
            if not lib["rule"] or (only_library and lib["id"] != only_library):
                continue
            should = v["status"] == "active" and match_rule(lib["rule"], v)
            out = await self.db.get_output(v["id"], lib["id"])
            if should and out is None:
                await self.db.ensure_output(v["id"], lib["id"], via="rule")
                await self._output_one(v, lib["id"], cover=bool(v["detail_at"]), old_strm="")
                added += 1
            elif not should and out is not None and out["via"] == "rule":
                if out["strm_path"]:
                    await asyncio.to_thread(self.writer.remove, Path(out["strm_path"]), self.keep_dirs())
                await self.db.delete_output(v["id"], lib["id"])
                removed += 1
        return added, removed

    async def _do_reclassify(self, job: dict, task: dict) -> None:
        lib_id = job["params"].get("library_id")
        n = added = removed = 0
        async for v in self.db.iter_videos():
            a, r = await self.apply_rules(v, only_library=lib_id)
            added, removed, n = added + a, removed + r, n + 1
            if n % 2000 == 0:
                log.info("重新归库：已检查 %d 部，加入 %d，移除 %d", n, added, removed)
        await self.db.update_job(job["id"], state={"checked": n, "added": added, "removed": removed})
        log.info("重新归库完成：检查 %d 部，加入 %d，移除 %d", n, added, removed)

    async def create_reclassify(self, library_id: int | None = None) -> int:
        name = f"重新归库：{self._library(library_id)['name']}" if library_id else "重新归库：全部规则库"
        job_id = await self.db.create_job("reclassify", name, {"library_id": library_id})
        await self.db.add_tasks(job_id, "reclassify", ["all"], PRIORITY_USER)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def _do_purge(self, job: dict, task: dict) -> None:
        lib_id = int(task["target"])
        lib = await self.db.get_library(lib_id)
        if lib is None:
            return
        if job["params"].get("delete_files"):
            keep = self.keep_dirs() - {self.writer.library_root(lib)}
            n = 0
            async for _, out in self.db.iter_outputs(lib_id):
                if out["strm_path"]:
                    await asyncio.to_thread(self.writer.remove, Path(out["strm_path"]), keep)
                    n += 1
            log.info("输出库「%s」：已删除 %d 部影片的文件", lib["name"], n)
        await self.db.delete_library(lib_id)
        await self.reload_libraries()
        log.info("输出库「%s」已删除", lib["name"])

    # ---- 创建 job ----

    async def create_crawl(
        self,
        source: str,
        *,
        sort: str = "",
        start_page: int = 1,
        end_page: int = 0,
        detail: bool | None = None,
        library_id: int = DEFAULT_LIBRARY_ID,
        name: str = "",
        incremental: bool = False,
        stop_after_known: int = DEFAULT_STOP_AFTER_KNOWN,
        max_pages: int = DEFAULT_MAX_PAGES,
        subscription_id: int | None = None,
    ) -> int:
        source = sources.normalize_source(source)
        lib = self._library(library_id)
        params = {
            "source": source,
            "sort": sort,
            "start_page": max(1, start_page),
            "end_page": max(0, end_page),
            "detail": self.store.current.fetch_detail if detail is None else detail,
            "library_id": library_id,
        }
        if incremental:
            params |= {"incremental": True, "stop_after_known": stop_after_known, "max_pages": max_pages}
        if subscription_id:
            params["subscription_id"] = subscription_id
        kind = "incremental" if incremental else "crawl"
        if not name:
            pages = f"第 {params['start_page']}-{end_page} 页" if end_page else f"第 {params['start_page']} 页起"
            name = f"{'增量' if incremental else '列表'} {source} {pages} → {lib['name']}"
        job_id = await self.db.create_job(kind, name, params)
        await self.db.add_tasks(job_id, "list", [params["start_page"]], PRIORITY_LIST)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def create_videos(
        self,
        slugs: list[str],
        *,
        library_id: int | None = DEFAULT_LIBRARY_ID,
        name: str = "",
        priority: int = PRIORITY_USER,
    ) -> int:
        slugs = list(dict.fromkeys(s.lower() for s in slugs))
        if not slugs:
            raise ValueError("没有可抓取的影片")
        if library_id:
            self._library(library_id)
        name = name or f"影片 {slugs[0]}" + (f" 等 {len(slugs)} 部" if len(slugs) > 1 else "")
        job_id = await self.db.create_job("videos", name, {"count": len(slugs), "library_id": library_id})
        await self.db.add_tasks(job_id, "detail", slugs, priority)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def create_backfill(self) -> int:
        slugs = await self.db.slugs_missing_detail()
        return await self.create_videos(slugs, library_id=None, name=f"补全缺失详情（{len(slugs)} 部）",
                                        priority=PRIORITY_DETAIL)

    async def create_rewrite(self, library_id: int | None = None) -> int:
        name = f"重写输出：{self._library(library_id)['name']}" if library_id else "重写全部输出"
        job_id = await self.db.create_job("rewrite", name, {"library_id": library_id})
        await self.db.add_tasks(job_id, "rewrite", ["all"], PRIORITY_USER)
        log.info("新建任务 #%d「%s」", job_id, name)
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

    # ---- 输出库管理 ----

    def _check_library(self, name: str, dir: str, path_template: str, exclude_id: int | None = None) -> tuple:
        name, dir = name.strip(), dir.strip().rstrip("/\\")
        if not name:
            raise ValueError("库名不能为空")
        if not dir:
            raise ValueError("目录不能为空：输出根目录本身不能作为库目录，否则别的库会嵌套在里面")
        if not Path(dir).is_absolute() and ".." in Path(dir).parts:
            raise ValueError("相对目录不能包含 ..")
        template = check_path_template(path_template) if path_template.strip() else ""
        root = self.writer.library_root({"dir": dir}).resolve()
        if root == self.store.output_dir.resolve():
            raise ValueError("库目录不能是输出根目录")
        for other in self.libs.values():
            if other["id"] == exclude_id:
                continue
            o = self.writer.library_root(other).resolve()
            if root == o or o in root.parents or root in o.parents:
                raise ValueError(f"目录和输出库「{other['name']}」（{o}）重叠，库目录不能互相嵌套")
        return name, dir, template

    async def create_library(self, name: str, dir: str, path_template: str = "", rule: dict | None = None) -> dict:
        """新建输出库；带规则时自动排一个重新归库任务。"""
        name, dir, template = self._check_library(name, dir, path_template)
        rule = normalize_rule(rule)
        lib_id = await self.db.create_library(name, dir, template, rule)
        await self.reload_libraries()
        log.info("新建输出库「%s」：%s%s", name, self.writer.library_root(self.libs[lib_id]),
                 f"，规则 {describe_rule(rule)}" if rule else "")
        return {"id": lib_id, "reclassify_job_id": await self.create_reclassify(lib_id) if rule else None}

    async def update_library(self, lib_id: int, name: str, dir: str, path_template: str = "",
                             rule: dict | None = None) -> dict:
        """修改输出库：目录或模板变了排重写任务搬文件，规则变了排重新归库任务。"""
        old = self._library(lib_id)
        name, dir, template = self._check_library(name, dir, path_template, exclude_id=lib_id)
        rule = normalize_rule(rule)
        await self.db.update_library(lib_id, name=name, dir=dir, path_template=template, rule=rule)
        await self.reload_libraries()
        jobs = {"rewrite_job_id": None, "reclassify_job_id": None}
        if (dir, template) != (old["dir"], old["path_template"]):
            jobs["rewrite_job_id"] = await self.create_rewrite(lib_id)
        if rule != old["rule"]:
            jobs["reclassify_job_id"] = await self.create_reclassify(lib_id)
        return jobs

    async def delete_library(self, lib_id: int, delete_files: bool) -> int:
        lib = self._library(lib_id)
        if lib_id == DEFAULT_LIBRARY_ID:
            raise ValueError("默认库不能删除，可以改名或改目录")
        if lib["subscriptions"]:
            raise ValueError("还有订阅在使用这个库，先删除或改掉这些订阅")
        for job in await self.db.list_jobs(limit=1000):
            if job["status"] in ("running", "paused") and job["params"].get("library_id") == lib_id:
                await self.set_job_status(job["id"], "cancel")
        job_id = await self.db.create_job("purge", f"删除输出库「{lib['name']}」" + ("及文件" if delete_files else ""),
                                          {"library_id": lib_id, "delete_files": delete_files})
        await self.db.add_tasks(job_id, "purge", [lib_id], PRIORITY_USER)
        self.notify()
        return job_id

    # ---- 订阅 ----

    async def run_subscription(self, sub_id: int, mode: str = "auto") -> int:
        """mode：auto（未初始化跑首轮全量，否则增量）、full、incremental。"""
        sub = await self.db.get_subscription(sub_id)
        if sub is None:
            raise KeyError(sub_id)
        active = await self.db.subscription_active_job(sub_id)
        if active:
            raise ValueError(f"订阅「{sub['name']}」已有任务在执行：#{active['id']}")
        full = mode == "full" or (mode == "auto" and not sub["initialized"])
        job_id = await self.create_crawl(
            sub["source"], sort=sub["sort"], detail=bool(sub["detail"]), library_id=sub["library_id"],
            incremental=not full, stop_after_known=sub["stop_after_known"], max_pages=sub["max_pages"],
            subscription_id=sub_id, name=f"订阅「{sub['name']}」" + ("首轮全量" if full else "增量"),
        )
        await self.db.update_subscription(sub_id, last_run_at=int(time.time()), last_job_id=job_id)
        return job_id

    async def _schedule_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._run_due_subscriptions()
            except Exception:
                log.exception("订阅调度出错")

    async def _run_due_subscriptions(self) -> None:
        if self.paused:
            return
        t = time.time()
        for sub in await self.db.list_subscriptions():
            if not (sub["enabled"] and sub["initialized"] and sub["interval"] > 0) or sub["active_job_id"]:
                continue
            if sub["last_run_at"] and t - sub["last_run_at"] < sub["interval"] * 60:
                continue
            await self.run_subscription(sub["id"], "incremental")


def snapshot_path(boot: BootConfig, name: str) -> Path | None:
    p = (boot.data_dir / "snapshots" / name).resolve()
    return p if p.parent == (boot.data_dir / "snapshots").resolve() and p.exists() else None
