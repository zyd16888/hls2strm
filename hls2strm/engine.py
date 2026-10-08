"""任务引擎：从 SQLite 领取子任务执行，失败按策略重试；支持暂停/恢复、断点续跑；管理输出库与订阅。

job 种类：
  crawl        翻某个列表来源，输出到指定输出库（订阅的首轮全量也是它）
  probe        补源：按番号到某个站点找库里影片的备用源
  verify       核对输出：数据库里的记录和磁盘上的 strm / nfo / 封面对不对得上，可选补回
  incremental  从第 1 页往后翻，连续遇到库里已有的影片就停（订阅的定时增量）
  videos       抓指定影片的详情（手动添加、补全缺失详情）
  rewrite      按当前设置重写输出（可限定某个库），路径变化时搬动文件
  purge        删除输出库及其文件
  locate       外部整理库：找回被外部工具（mdcng 等）移走、改名的 strm，更新记录的路径
  reclassify   重新归库：规则库重新求值，并按来源库、排除库归并（只在本地，不联网）
子任务种类：list（目标=页码）、detail（目标=站内 key）、probe（目标=站点:作品 id）、rewrite（目标=all）、
          purge / locate（目标=库 id）、verify（目标=all）、cover（目标=库 id:作品 id，补封面）
list、detail 子任务带站点（tasks.site）：某个站被拦截时只暂停这个站的子任务，每个站的并发也各自限制。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from .codes import work_slug
from .config import BootConfig, SettingsStore, check_path_template
from .db import DEFAULT_LIBRARY_ID, Database
from .fetcher import Blocked, FetchError, Fetcher, NotFound
from .observability import Metrics
from .parser import ParseError, VideoGone, m3u8_duration
from .rules import describe_rule, match_rule, normalize_rule
from .sites import SITES, Site, SourceDetail, SourceItem, find_by_code, get_site
from .strm_manage import StrmManager
from .writer import OutputWriter, cover_path

log = logging.getLogger(__name__)

PRIORITY_USER = 20
PRIORITY_LIST = 10
PRIORITY_DETAIL = 0
MAX_RETRY_DELAY = 7200
MAX_SNAPSHOTS = 200
DEFAULT_STOP_AFTER_KNOWN = 48
PROBE_TIMEOUT = 90  # 「查找其他源」每个站最多等多久（秒）：要过 CF 的站走解题服务，几十秒很常见
DEFAULT_MAX_PAGES = 20
SETTLE_INTERVAL = 600


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
        self.blocked: dict[str, float] = {}  # 站点 -> 被拦截到什么时候
        self.running: dict[int, dict] = {}
        self._claim_lock = asyncio.Lock()
        self.libs: dict[int, dict] = {}
        self.strm = StrmManager(self)
        self._workers: list[asyncio.Task] = []
        self._scheduler: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._settle_lock = asyncio.Lock()
        self._settle_wanted = True
        self._settled_at = 0.0
        self.missing: dict[int, int] = {}  # 库 id -> 有记录但磁盘上找不到的 strm 数（启动时、核对后统计）
        self._missing_task: asyncio.Task | None = None

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
        self.store.on_change(lambda old, new: self._on_settings())
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="scheduler")
        self._missing_task = asyncio.create_task(self.count_missing(), name="count-missing")

    async def stop(self) -> None:
        self._stopping = True
        tasks = [t for t in self._workers if not t.done()]
        if self._scheduler:
            tasks.append(self._scheduler)
        if self._missing_task and not self._missing_task.done():
            tasks.append(self._missing_task)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _worker_count(self) -> int:
        """worker 总数 = 各启用站点的并发之和；各站点实际并发在领任务时再限制。"""
        return max(1, sum(c.concurrency for c in self.store.current.sites.values() if c.enabled))

    def _resize_workers(self) -> None:
        self._workers = [t for t in self._workers if not t.done()]
        for i in range(len(self._workers), self._worker_count()):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"worker-{i}"))

    def _on_settings(self) -> None:
        self._resize_workers()
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass
        self._wake.clear()

    def blocked_sites(self) -> dict[str, int]:
        """正被拦截的站点 -> 剩余秒数。"""
        t = time.time()
        return {name: int(until - t) for name, until in self.blocked.items() if until > t}

    def status(self) -> dict:
        blocked = self.blocked_sites()
        return {
            "paused": self.paused,
            "blocked": blocked,
            "blocked_for": max(blocked.values(), default=0),
            "workers": len([t for t in self._workers if not t.done()]),
            "running": list(self.running.values()),
        }

    def pause(self) -> None:
        self.paused = True
        log.info("引擎已暂停")

    def resume(self) -> None:
        self.paused = False
        self.blocked.clear()
        self.notify()
        log.info("引擎已恢复")

    def clear_blocked(self, site: str | None = None) -> None:
        if site is None:
            self.blocked.clear()
        else:
            self.blocked.pop(site, None)
        self.notify()

    def _skip_sites(self) -> list[str]:
        """现在不能领任务的站点：未启用、被拦截中、并发已满。"""
        t = time.time()
        busy = Counter(r["site"] for r in self.running.values() if r["site"])
        cfg = self.store.current.sites
        return [name for name in SITES
                if not cfg[name].enabled or self.blocked.get(name, 0) > t or busy[name] >= cfg[name].concurrency]

    # ---- worker ----

    async def _worker(self, idx: int) -> None:
        while not self._stopping:
            if idx >= self._worker_count():
                return
            if self.paused:
                await self._idle(2)
                continue
            async with self._claim_lock:  # 算可领站点和登记 running 要原子，否则并发会超出站点上限
                task = await self.db.claim_task(self._skip_sites())
                if task is not None:
                    self.running[task["id"]] = {
                        "id": task["id"], "job_id": task["job_id"], "kind": task["kind"], "site": task["site"],
                        "target": task["target"], "attempt": task["attempts"], "started": time.time()}
            if task is None:
                await self._idle(2)
                continue
            await self._run(task)

    async def _run(self, task: dict) -> None:
        tid = task["id"]
        t0 = time.monotonic()
        finished = False
        try:
            job = await self.db.get_job(task["job_id"])
            if job is None or job["status"] != "running":
                await self.db.finish_task(tid, "pending", refund_attempt=True)
                return
            handler = {"list": self._do_list, "detail": self._do_detail, "probe": self._do_probe,
                       "rewrite": self._do_rewrite, "verify": self._do_verify, "cover": self._do_cover,
                       "purge": self._do_purge, "reclassify": self._do_reclassify,
                       "scan": self.strm.do_scan, "adopt": self.strm.do_adopt,
                       "prefix": self.strm.do_prefix, "revert": self.strm.do_revert,
                       "locate": self.strm.do_locate}[task["kind"]]
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
                await self.db.mark_source_gone(task["site"] or "jable", task["target"])
            self.metrics.inc("task_gone")
            log.info("%s %s 已下架：%s", task["kind"], task["target"], e)
            finished = True
        except Blocked as e:
            site = task["site"] or "jable"
            first = self.blocked.get(site, 0) < time.time()
            self.blocked[site] = max(self.blocked.get(site, 0), time.time() + e.retry_after)
            await self.db.finish_task(tid, "pending", f"被拦截：{e}",
                                      next_run_at=int(self.blocked[site]), refund_attempt=True)
            if first:
                log.warning("%s，暂停该站点 %d 秒后自动重试", e, int(e.retry_after))
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
        self._settle_wanted = True
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
        roots = [self.writer.library_root(lib) for lib in self.libs.values()]
        return frozenset(roots + [r for lib in self.libs.values() if (r := self.writer.external_root(lib))])

    def _library(self, library_id: int) -> dict:
        lib = self.libs.get(library_id)
        if lib is None:
            raise ValueError(f"输出库 #{library_id} 不存在")
        return lib

    async def _output_one(self, v: dict, library_id: int, *, cover: bool, old_strm: str | None = None,
                          settle: bool = False) -> None:
        lib = self._library(library_id)
        if old_strm is None:
            out = await self.db.get_output(v["id"], library_id)
            old_strm = out["strm_path"] if out else ""
        if not old_strm and lib["excludes"] and not settle:
            return  # 有排除库的库：新片等归并确认它不属于排除库后再写
        if self.store.current.play_mode == "direct":
            v = {**v, "stream_url": await self.db.cached_stream_url(v["id"])}
        strm = await asyncio.to_thread(self.writer.write, v, lib, old_strm, self.keep_dirs())
        if strm is None:
            return  # 外部整理库：文件已被外部工具移走，等「同步位置」找回
        cover_done = None
        if cover and not lib["external_dir"]:
            siblings = [Path(o["strm_path"]) for o in await self.db.get_outputs(v["id"])
                        if o["library_id"] != library_id and o["strm_path"]]
            cover_done = await self.writer.write_cover(self.fetcher, v, strm, siblings)
        await self.db.set_output(v["id"], library_id, str(strm), cover_done)

    async def _output_all(self, v: dict, *, cover: bool) -> None:
        for out in await self.db.get_outputs(v["id"]):
            await self._output_one(v, out["library_id"], cover=cover, old_strm=out["strm_path"])

    # ---- 子任务处理 ----

    def _rank(self, site: str) -> int:
        return self.store.current.site_rank(site)

    @staticmethod
    def _slug_for(site: Site, key: str, code: str, uncensored: bool) -> str:
        """新作品的 slug：Jable 沿用站内 slug（和老数据一致），其他站用番号。"""
        return key if site.name == "jable" else work_slug(code, uncensored)

    async def upsert_item(self, site: Site, it: SourceItem) -> tuple[dict, bool]:
        vid, created = await self.db.upsert_item(site.name, it, self._slug_for(site, it.key, it.code, it.uncensored),
                                                 self._rank)
        return await self.db.get_video_by_id(vid), created

    async def upsert_detail(self, site: Site, d: SourceDetail) -> dict:
        vid = await self.db.upsert_detail(site.name, d, self._slug_for(site, d.key, d.code, d.uncensored), self._rank)
        return await self.db.get_video_by_id(vid)

    async def _do_list(self, job: dict, task: dict) -> None:
        p = job["params"]
        site = get_site(p.get("site", "jable"))
        lib_id = p.get("library_id") or DEFAULT_LIBRARY_ID
        lib = self._library(lib_id)
        page = int(task["target"])
        url = site.page_url(p["source"], page, p.get("sort", ""), p.get("block_id"))
        pg = await self.fetcher.site(site.name).get_page(url)
        lp = site.parse_list(pg.html)
        if not lp.items and page == p["start_page"]:
            e = ParseError("列表为空，检查列表地址是否正确")
            e.html = pg.html
            raise e

        added_count = 0
        restored: Counter = Counter()
        detail_keys = []
        state = (await self.db.get_job(job["id"]))["state"]
        probe_sites = [n for n in self.store.current.auto_probe_sites
                       if n in SITES and n != site.name and SITES[n].can_lookup]
        for it in lp.items:
            v, created = await self.upsert_item(site, it)
            for n in probe_sites if created else ():
                await self.db.add_tasks(job["id"], "probe", [f"{n}:{v['id']}"], PRIORITY_DETAIL, n)
            if await self.db.in_libraries(v["id"], lib["excludes"]):
                # 已分到排除库：本库不收，但算作已知，增量照常停
                state["known_streak"] = state.get("known_streak", 0) + 1
                continue
            added = await self.db.ensure_output(v["id"], lib_id)
            strm_path = (await self.db.get_output(v["id"], lib_id))["strm_path"]
            if not added and strm_path and not await asyncio.to_thread(os.path.isfile, strm_path):
                # 记录在、文件不在了（换了输出目录的挂载、被删了，或者被外部刮削器挪走、改名）
                restored[await self.restore_lost(v, lib)] += 1
            elif added or not strm_path:
                # 已有详情的影片（别的库抓过）直接带上 nfo 和封面
                await self._output_one(v, lib_id, cover=bool(v["detail_at"]))
            if p.get("detail", True) and v["detail_at"] is None:
                detail_keys.append(it.key)
            added_count += added
            state["known_streak"] = 0 if added else state.get("known_streak", 0) + 1
        if restored["rewritten"] and self.missing.get(lib_id):  # 补回的不再算缺失
            self.missing[lib_id] = max(0, self.missing[lib_id] - restored["rewritten"])
        if detail_keys:
            await self.db.add_tasks(job["id"], "detail", detail_keys, PRIORITY_DETAIL, site.name)
        self.metrics.inc("videos_new", added_count)

        last = lp.last_page or page
        end = min(p.get("end_page") or last, last)
        if p.get("incremental"):
            pages_done = page - p["start_page"] + 1
            if state["known_streak"] >= p["stop_after_known"]:
                log.info("增量：连续 %d 部已在库里，停止翻页", state["known_streak"])
            elif page < end and pages_done < p["max_pages"]:
                await self.db.add_tasks(job["id"], "list", [page + 1], PRIORITY_LIST, site.name)
        elif page == p["start_page"] and not state.get("pages_enqueued"):
            n = await self.db.add_tasks(job["id"], "list", range(page + 1, end + 1), PRIORITY_LIST, site.name)
            state["pages_enqueued"] = True
            state["last_page"] = last
            if n:
                log.info("任务 #%d：共 %d 页，已排队第 %d-%d 页", job["id"], last, page + 1, end)
        await self.db.update_job(job["id"], state=state)
        log.info("%s 列表 %s 第 %d/%d 页 → 库「%s」：%d 部，新加入 %d，排队详情 %d%s", site.label,
                 p["source"], page, last, self.libs[lib_id]["name"], len(lp.items), added_count, len(detail_keys),
                 _restore_text(restored))

    async def _do_detail(self, job: dict, task: dict) -> None:
        await self.fetch_detail(task["site"] or "jable", task["target"], library_id=job["params"].get("library_id"))

    async def fetch_detail(self, site_name: str, key: str, *, library_id: int | None = None,
                           priority: bool = False) -> dict:
        """抓某个源的详情、合并作品元数据；library_id 不为空时把作品加入该库。作品所在的每个库都会重写输出。"""
        if library_id:
            self._library(library_id)
        site = get_site(site_name)
        d = await site.fetch_detail(self.fetcher.site(site.name), key, priority=priority)
        v = await self.upsert_detail(site, d)
        if not v["duration"] and d.stream_url:
            await self._fill_duration(v, site, d.stream_url)
        if library_id:
            await self.db.ensure_output(v["id"], library_id)
        await self._output_all(v, cover=True)
        await self.apply_rules(v)
        self.metrics.inc("videos_detail")
        log.info("详情 %s %s：%s，女优 %s，%d 个标签", site.label, key, v["release_date"] or "无日期",
                 "、".join(m["name"] for m in v["models"]) or "无", len(v["tags"]))
        return await self.db.get_video_by_id(v["id"])

    async def _do_probe(self, job: dict, task: dict) -> None:
        site, _, vid = task["target"].partition(":")
        await self.probe_work(int(vid), site)

    async def probe_work(self, video_id: int, site_name: str) -> int:
        """补源：按番号到这个站找这部作品，找到就加成源（同一部片的中字版也一起加）。返回新加的源数。

        站点上的页面番号对不上（被纠正到别的片），或者是不是无码流出对不上，都当作没有。
        """
        v = await self.db.get_video_by_id(video_id)
        site = get_site(site_name)
        if v is None:
            return 0
        if any(s["site"] == site.name for s in await self.db.get_sources(video_id)):
            return 0
        return await self._attach_found(v, site, await self._lookup(site, v))

    async def _attach_found(self, v: dict, site: Site, found: list[SourceItem | SourceDetail]) -> int:
        """把按番号找到的结果挂成作品的源（同一部片的中字版也一起加），重写输出。返回新加的源数。"""
        video_id = v["id"]
        sf = self.fetcher.site(site.name)
        if not found:
            await self.db.set_source_check(video_id, site.name, False)
            log.info("补源 %s：%s 上没有", v["slug"], site.label)
            return 0
        added = []
        variants: list[str] = []
        for item in found:
            if isinstance(item, SourceDetail):
                await self.db.upsert_detail(site.name, item, v["slug"], self._rank, video_id=video_id)
                variants += item.variants
            else:
                await self.db.upsert_item(site.name, item, v["slug"], self._rank, video_id=video_id)
            added.append(item.key)
        for alt in variants:  # 同一部片的中字版：多一个更好的源
            if alt in added or site.variant_of(alt) != ("zh", bool(v["uncensored"])):
                continue
            try:
                ad = await site.fetch_detail(sf, alt)
            except (NotFound, VideoGone):
                continue
            await self.db.upsert_detail(site.name, ad, v["slug"], self._rank, video_id=video_id)
            added.append(ad.key)
        await self.db.set_source_check(video_id, site.name, True)
        self.metrics.inc("probe_found")
        v = await self.db.get_video_by_id(video_id)
        await self._output_all(v, cover=True)
        await self.apply_rules(v)
        log.info("补源 %s：在 %s 找到 %s", v["slug"], site.label, "、".join(added))
        return len(added)

    async def _lookup(self, site: Site, v: dict, priority: bool = False) -> list[SourceItem | SourceDetail]:
        return await find_by_code(site, self.fetcher.site(site.name), v["code"], bool(v["uncensored"]), priority)

    async def probe_video(self, slug: str) -> tuple[dict, list[dict]]:
        """影片库里点「查找其他源」：到每个启用、还没有源的站点找一次（不管之前查过没有）。

        各站并行查、不排限速队列，每站最多 PROBE_TIMEOUT 秒（要过 CF 的站会走解题服务，慢）；查到的再逐个挂上。
        返回 (作品, 每个站的结果)。结果 status：found / none / failed / timeout。
        """
        v = await self.db.get_video(slug)
        if v is None:
            raise NotFound(slug)
        cfg = self.store.current.sites
        have = {s["site"] for s in await self.db.get_sources(v["id"])}
        sites = [SITES[n] for n in self.store.current.site_priority
                 if n not in have and cfg[n].enabled and SITES[n].can_lookup]

        async def one(site: Site):
            return await asyncio.wait_for(self._lookup(site, v, priority=True), PROBE_TIMEOUT)

        results = []
        for site, found in zip(sites, await asyncio.gather(*(one(s) for s in sites), return_exceptions=True)):
            r = {"site": site.name, "label": site.label}
            if isinstance(found, TimeoutError):
                r |= {"status": "timeout", "error": f"超过 {PROBE_TIMEOUT} 秒没查完"}
            elif isinstance(found, (NotFound, VideoGone)):
                await self.db.set_source_check(v["id"], site.name, False)
                r |= {"status": "none", "found": 0}
            elif isinstance(found, (Blocked, FetchError, ParseError)):
                r |= {"status": "failed", "error": str(found)[:200]}
            elif isinstance(found, BaseException):
                raise found
            else:
                try:
                    n = await self._attach_found(v, site, found)
                except (Blocked, FetchError, ParseError) as e:
                    r |= {"status": "failed", "error": str(e)[:200]}
                else:
                    r |= {"status": "found" if n else "none", "found": n}
            if r.get("error"):
                log.info("查找其他源 %s：%s %s", slug, site.label, r["error"])
            results.append(r)
        return await self.db.get_video_by_id(v["id"]), results

    async def create_probe(self, site: str, library_id: int | None = None) -> int:
        """补源任务：库里（或某个库里）在这个站还没有源、最近没查过的影片，逐部按番号去找。"""
        st = get_site(site)
        if not st.can_lookup:
            raise ValueError(f"{st.label} 不支持按番号查找，不能补源")
        lib_name = self._library(library_id)["name"] if library_id else "全部影片"
        cutoff = int(time.time()) - self.store.current.probe_recheck_days * 86400
        ids = await self.db.works_to_probe(st.name, cutoff, library_id)
        if not ids:
            raise ValueError(f"{lib_name}在 {st.label} 上都有源了，或者最近查过")
        name = f"补源：{lib_name} → {st.label}（{len(ids)} 部）"
        job_id = await self.db.create_job("probe", name, {"site": st.name, "library_id": library_id, "count": len(ids)})
        await self.db.add_tasks(job_id, "probe", [f"{st.name}:{i}" for i in ids], PRIORITY_DETAIL, st.name)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def refresh_video(self, slug: str) -> dict:
        """重抓作品每个可用源的详情；全部失败才报错（都下架时抛 NotFound）。"""
        v = await self.db.get_video(slug)
        if v is None:
            raise NotFound(slug)
        cfg = self.store.current.sites
        srcs = [s for s in await self.db.get_sources(v["id"])
                if s["status"] == "active" and s["site"] in cfg and cfg[s["site"]].enabled]
        if not srcs:
            raise NotFound(f"{slug} 没有可用的源")
        errors: list[Exception] = []
        for src in srcs:
            try:
                await self.fetch_detail(src["site"], src["key"], priority=True)
            except (NotFound, VideoGone) as e:
                await self.db.mark_source_gone(src["site"], src["key"])
                errors.append(e)
            except (Blocked, FetchError, ParseError) as e:
                errors.append(e)
        if len(errors) == len(srcs):
            if all(isinstance(e, (NotFound, VideoGone)) for e in errors):
                raise errors[0]
            raise next(e for e in errors if not isinstance(e, (NotFound, VideoGone)))
        return await self.db.get_video_by_id(v["id"])

    async def _fill_duration(self, v: dict, site: Site, stream_url: str) -> None:
        """没走过列表页的影片（指定影片任务）没有时长：从 m3u8 分片时长累加。"""
        try:
            duration = await playlist_duration(self.fetcher, stream_url, site.stream.headers)
        except Exception as e:
            log.info("%s 获取时长失败：%s", v["slug"], e)
            return
        if duration:
            await self.db.set_duration(v["id"], duration)
            v["duration"] = duration

    async def _do_rewrite(self, job: dict, task: dict) -> None:
        lib_id = job["params"].get("library_id")
        for lib in list(self.libs.values()):
            if lib["external_dir"] and lib_id in (None, lib["id"]):
                await self.strm.locate(lib)  # 外部整理库先找回文件的新位置，再原地改内容
        n = 0
        async for v, out in self.db.iter_outputs(lib_id):
            if v["status"] != "active" or out["library_id"] not in self.libs:
                continue
            await self._output_one(v, out["library_id"], cover=False, old_strm=out["strm_path"])
            n += 1
            if n % 1000 == 0:
                log.info("重写输出：已完成 %d 个", n)
        log.info("重写输出完成：共 %d 个", n)
        await self.count_missing()

    # ---- 核对输出 ----

    async def restore_lost(self, v: dict, lib: dict, *, force: bool = False) -> str:
        """记录的 strm 不在了，补回或找回。返回 rewritten（重新写了）/ relocated（外部工具挪走了，已更新路径）/
        missing（外部整理库找不到，按设置不补）/ unavailable（外部整理目录不在或是空的，没补）。

        普通库：直接按模板重写（含 nfo、封面）。外部整理库：先按内容在收件目录和外部整理目录里找，
        外部工具改了目录、加了后缀也认得出，找到就只更新路径；都找不到才写回收件目录让外部工具再整理一次。
        force：外部整理目录是空的也写回（「核对」里手动勾选）。
        """
        if not lib["external_dir"]:
            await self._output_one(v, lib["id"], cover=bool(v["detail_at"]))
            return "rewritten"
        found = await self.strm.index(lib)
        paths = [x for x in found.get(v["id"], []) if await asyncio.to_thread(os.path.isfile, x)]
        if not paths and v["id"] in found:  # 缓存的位置又被挪了：重新扫一遍
            found = await self.strm.index(lib, max_age=0)
            paths = found.get(v["id"], [])
        if paths:
            await self.db.set_output_paths(lib["id"], [(self.strm.pick(lib, paths), v["id"])])
            return "relocated"
        if not force and not self.strm.external_available(lib, found):
            return "unavailable"
        if not force and not self.store.current.external_restore:
            return "missing"
        await self._output_one(v, lib["id"], cover=False, old_strm="", settle=True)
        if (out := await self.db.get_output(v["id"], lib["id"])) and out["strm_path"]:
            self.strm.remember(lib, v["id"], out["strm_path"])
        return "rewritten"

    def _output_problems(self, v: dict, strm_path: str) -> list[str]:
        """一条输出在磁盘上缺什么：strm（没有或内容不是当前的播放地址）、nfo、cover。同步函数，放到线程里跑。"""
        s = self.store.current
        strm = Path(strm_path)
        problems = []
        try:
            content = strm.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            content = None
        if content is None or (s.play_mode != "direct" and content != self.writer.play_url(v)):
            problems.append("strm")
        if s.write_nfo and v.get("detail_at") and not cover_path(strm, ".nfo").is_file():
            problems.append("nfo")
        if s.download_cover and v.get("cover_url"):
            names = ["-fanart.jpg"] + (["-poster.jpg"] if s.poster_crop else [])
            if not all(cover_path(strm, n).is_file() for n in names):
                problems.append("cover")
        return problems

    async def count_missing(self) -> dict[int, int]:
        """统计每个库有记录、但磁盘上找不到 strm 的影片数。外部整理库按内容在收件目录和外部整理目录里找。"""
        paths: list[tuple[int, str]] = []
        external: Counter = Counter()
        indexes = {lid: await self.strm.index(lib) for lid, lib in list(self.libs.items()) if lib["external_dir"]}
        async for v, out in self.db.iter_outputs():
            lib = self.libs.get(out["library_id"])
            if not lib or v["status"] != "active" or not out["strm_path"]:
                continue
            if lib["external_dir"]:
                external[lib["id"]] += v["id"] not in indexes[lib["id"]]  # 按内容找，外部工具挪走、改名不算丢
            else:
                paths.append((lib["id"], out["strm_path"]))
        counts = await asyncio.to_thread(lambda: Counter(lid for lid, p in paths if not os.path.isfile(p)))
        counts.update(+external)
        self.missing = dict(counts)
        if total := sum(counts.values()):
            log.warning("磁盘上找不到 %d 个 strm（数据库里有记录）：%s。可以在「输出库与订阅」执行「核对」补回", total,
                        "、".join(f"{self.libs[k]['name']} {n}" for k, n in counts.items()))
        return self.missing

    async def _do_verify(self, job: dict, task: dict) -> None:
        p = job["params"]
        lib_id, repair, covers = p.get("library_id"), p.get("repair", True), p.get("covers", True)
        force_external = p.get("force_external", False)
        st = Counter()
        for lib in list(self.libs.values()):
            if lib["external_dir"] and lib_id in (None, lib["id"]):
                await self.strm.locate(lib)  # 外部整理库先找回被外部工具移走的文件
        cover_targets = []
        async for v, out in self.db.iter_outputs(lib_id):
            lib = self.libs.get(out["library_id"])
            if lib is None or v["status"] != "active" or not out["strm_path"]:
                continue
            st["checked"] += 1
            if lib["external_dir"]:
                # 上面已经同步过位置：记录的路径还找不到，就是收件目录和外部整理目录里都没有
                if await asyncio.to_thread(os.path.isfile, out["strm_path"]):
                    st["ok"] += 1
                elif repair:
                    st["external_" + await self.restore_lost(v, lib, force=force_external)] += 1
                else:
                    st["external_missing"] += 1
                continue
            problems = await asyncio.to_thread(self._output_problems, v, out["strm_path"])
            if not problems:
                st["ok"] += 1
                continue
            st.update(problems)
            if not repair:
                continue
            if "strm" in problems or "nfo" in problems:
                await self._output_one(v, lib["id"], cover=False, old_strm=out["strm_path"])
                st["repaired"] += 1
            if "cover" in problems:
                if covers:
                    cover_targets.append(f"{lib['id']}:{v['id']}")
                else:
                    await self.db.set_cover_done(v["id"], lib["id"], False)
            if st["checked"] % 2000 == 0:
                log.info("核对输出：已检查 %d 部", st["checked"])
        if cover_targets:
            st["covers_queued"] = await self.db.add_tasks(job["id"], "cover", cover_targets, PRIORITY_DETAIL)
        await self.db.update_job(job["id"], state=dict(st))
        await self.count_missing()
        log.info("核对输出%s：检查 %d 部，正常 %d；strm 缺失或不对 %d，nfo 缺失 %d，封面缺失 %d%s",
                 "并修复" if repair else "（只检查）", st["checked"], st["ok"], st["strm"], st["nfo"], st["cover"],
                 f"；已补写 {st['repaired']} 部，排队补封面 {st['covers_queued']} 部" if repair else "")
        ext = {k.removeprefix("external_"): n for k, n in st.items() if k.startswith("external_")}
        if ext:
            log.info("核对输出：外部整理库找不到的 %s", _restore_text(Counter(ext)).lstrip("，") or
                     f"{ext.get('missing', 0)} 部（只检查）")

    async def _do_cover(self, job: dict, task: dict) -> None:
        lib_id, vid = (int(x) for x in task["target"].split(":"))
        v = await self.db.get_video_by_id(vid)
        out = await self.db.get_output(vid, lib_id)
        if v is None or out is None or not out["strm_path"] or lib_id not in self.libs:
            return
        await self._output_one(v, lib_id, cover=True, old_strm=out["strm_path"])

    async def create_verify(self, library_id: int | None = None, *, repair: bool = True, covers: bool = True,
                            force_external: bool = False) -> int:
        lib_name = self._library(library_id)["name"] if library_id else "全部库"
        name = f"核对输出：{lib_name}" + ("（修复）" if repair else "（只检查）")
        job_id = await self.db.create_job("verify", name, {"library_id": library_id, "repair": repair, "covers": covers,
                                                           "force_external": force_external})
        await self.db.add_tasks(job_id, "verify", ["all"], PRIORITY_USER)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def apply_rules(self, v: dict, only_library: int | None = None) -> tuple[int, int]:
        """按规则库调整影片归属：命中就加入（via=rule），不再命中就移除规则加入的输出。返回 (加入, 移除)。"""
        added = removed = 0
        for lib in list(self.libs.values()):
            if not lib["rule"] or (only_library and lib["id"] != only_library):
                continue
            should = (v["status"] == "active" and match_rule(lib["rule"], v)
                      and not await self.db.in_libraries(v["id"], lib["excludes"]))
            out = await self.db.get_output(v["id"], lib["id"])
            if should and out is None:
                await self.db.ensure_output(v["id"], lib["id"], via="rule")
                await self._output_one(v, lib["id"], cover=bool(v["detail_at"]), old_strm="")
                added += 1
            elif not should and out is not None and out["via"] == "rule":
                if out["strm_path"]:
                    await asyncio.to_thread(self.writer.remove, Path(out["strm_path"]), self.keep_dirs(),
                                            bool(lib["external_dir"]))
                await self.db.delete_output(v["id"], lib["id"])
                removed += 1
        return added, removed

    async def _do_reclassify(self, job: dict, task: dict) -> None:
        lib_id = job["params"].get("library_id")
        n = added = removed = 0
        if any(lib["rule"] for lib in self.libs.values() if lib_id in (None, lib["id"])):
            async for v in self.db.iter_videos():
                a, r = await self.apply_rules(v, only_library=lib_id)
                added, removed, n = added + a, removed + r, n + 1
                if n % 2000 == 0:
                    log.info("重新归库：已检查 %d 部，加入 %d，移除 %d", n, added, removed)
            log.info("重新归库完成：检查 %d 部，加入 %d，移除 %d", n, added, removed)
        state: dict = {"checked": n, "added": added, "removed": removed}
        if settled := await self.settle():
            state["settle"] = settled
        await self.db.update_job(job["id"], state=state)

    # ---- 来源库 / 排除库 ----

    async def settle(self) -> dict:
        """归并：按来源库、排除库调整各库的影片，只在本地，不联网。返回 {移除, 归入, 写入}，没有相关库时为空。

        有排除库的库，新片先只记一条没有文件的输出；等排除库（及其依赖的库）的定时订阅都跑完一轮
        「在影片首次出现之后才开始」的任务，确认它没被分到排除库，才写 strm。这样外部工具（mdcng 等）
        不会先在这里刮一遍、等影片被分走后再刮一遍。
        """
        async with self._settle_lock:
            self._settle_wanted = False
            self._settled_at = time.time()
            linked = await self.db.libraries_with_source_outputs()
            libs = [lib for lib in self._dependency_order() if lib["sources"] or lib["excludes"] or lib["id"] in linked]
            if not libs:
                return {}
            sub_cutoffs = await self._subscription_cutoffs()
            total = {"removed": 0, "pulled": 0, "written": 0}
            for lib in libs:
                r = await self._settle_library(lib, sub_cutoffs)
                total = {k: total[k] + r[k] for k in total}
                if any(r.values()):
                    log.info("归并「%s」：移除 %d，从来源库归入 %d，写入 %d", lib["name"], r["removed"], r["pulled"],
                             r["written"])
            return total

    async def _settle_library(self, lib: dict, sub_cutoffs: dict[int, int]) -> dict:
        keep, external = self.keep_dirs(), bool(lib["external_dir"])
        drop = await self.db.outputs_to_drop(lib["id"], lib["sources"], lib["excludes"])
        for out in drop:
            if out["strm_path"]:
                await asyncio.to_thread(self.writer.remove, Path(out["strm_path"]), keep, external)
            await self.db.delete_output(out["video_id"], lib["id"])
        pulled = await self.db.pull_from_sources(lib["id"], lib["sources"], lib["excludes"])
        written = 0
        for v in await self.db.pending_outputs(lib["id"], self._seen_before(lib, sub_cutoffs)):
            # 归并只在本地跑，不下载封面；封面等下次抓详情时补
            await self._output_one(v, lib["id"], cover=False, old_strm="", settle=True)
            written += 1
            if written % 2000 == 0:
                log.info("归并「%s」：已写入 %d 部", lib["name"], written)
        return {"removed": len(drop), "pulled": pulled, "written": written}

    async def _subscription_cutoffs(self) -> dict[int, int]:
        """库 id → 该库每个定时订阅都已跑完一轮的时间点（最近一次跑完的任务的开始时间，取最早的那个订阅）。

        还没跑首轮全量的订阅记 0（库还不完整，什么都确认不了）；没跑过但不需要首轮的，从订阅创建时算起。
        """
        last_done = await self.db.subscription_last_done()
        cutoffs: dict[int, int] = {}
        for sub in await self.db.list_subscriptions():
            if not sub["enabled"] or sub["interval"] <= 0:
                continue
            t = (last_done.get(sub["id"]) or sub["created_at"]) if sub["initialized"] else 0
            cutoffs[sub["library_id"]] = min(cutoffs.get(sub["library_id"], t), t)
        return cutoffs

    def _seen_before(self, lib: dict, sub_cutoffs: dict[int, int]) -> int:
        """首次出现早于这个时间点的影片，才能确认没被分到排除库；没有排除库就不用等。"""
        t = int(time.time()) + 1
        todo, seen = list(lib["excludes"]), set()
        while todo:
            lid = todo.pop()
            if lid in seen or lid not in self.libs:
                continue
            seen.add(lid)
            t = min(t, sub_cutoffs.get(lid, t))
            todo += self.libs[lid]["sources"] + self.libs[lid]["excludes"]
        return t

    def _dependency_order(self) -> list[dict]:
        """来源库、排除库排在依赖它们的库前面（保存时已校验不成环）。"""
        order: list[dict] = []
        seen: set[int] = set()

        def visit(lid: int) -> None:
            if lid in seen or lid not in self.libs:
                return
            seen.add(lid)
            for dep in self.libs[lid]["sources"] + self.libs[lid]["excludes"]:
                visit(dep)
            order.append(self.libs[lid])

        for lid in sorted(self.libs):
            visit(lid)
        return order

    def _check_links(self, lib_id: int | None, sources: list[int], excludes: list[int]) -> tuple[list[int], list[int]]:
        sources, excludes = sorted(set(sources)), sorted(set(excludes))
        for lid in sources + excludes:
            if lid == lib_id:
                raise ValueError("来源库、排除库不能选自己")
            self._library(lid)
        if set(sources) & set(excludes):
            raise ValueError("同一个库不能既是来源库又是排除库")
        todo, seen = sources + excludes, set()
        while lib_id is not None and todo:
            lid = todo.pop()
            if lid == lib_id:
                raise ValueError("来源库、排除库不能循环引用（比如 A 排除 B，B 又排除 A）")
            if lid not in seen:
                seen.add(lid)
                todo += self.libs[lid]["sources"] + self.libs[lid]["excludes"]
        return sources, excludes

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
            external = bool(lib["external_dir"])
            if external:
                await self.strm.locate(lib)
            keep = self.keep_dirs() - {self.writer.library_root(lib)}
            n = 0
            async for _, out in self.db.iter_outputs(lib_id):
                if out["strm_path"]:
                    await asyncio.to_thread(self.writer.remove, Path(out["strm_path"]), keep, external)
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
        site: str = "jable",
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
        st = get_site(site)
        source = st.normalize_source(source)
        lib = self._library(library_id)
        params = {
            "site": st.name,
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
            name = f"{'增量' if incremental else '列表'} {st.label} {source} {pages} → {lib['name']}"
        job_id = await self.db.create_job(kind, name, params)
        await self.db.add_tasks(job_id, "list", [params["start_page"]], PRIORITY_LIST, st.name)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def create_videos(
        self,
        items: list[tuple[str, str]],
        *,
        library_id: int | None = DEFAULT_LIBRARY_ID,
        name: str = "",
        priority: int = PRIORITY_USER,
    ) -> int:
        """抓指定影片的详情。items 是 [(站点, 站内 key)]。"""
        items = list(dict.fromkeys((site, key.lower()) for site, key in items))
        if not items:
            raise ValueError("没有可抓取的影片")
        if library_id:
            self._library(library_id)
        name = name or f"影片 {items[0][1]}" + (f" 等 {len(items)} 部" if len(items) > 1 else "")
        job_id = await self.db.create_job("videos", name, {"count": len(items), "library_id": library_id})
        by_site: dict[str, list[str]] = {}
        for site, key in items:
            by_site.setdefault(get_site(site).name, []).append(key)
        for site, keys in by_site.items():
            await self.db.add_tasks(job_id, "detail", keys, priority, site)
        log.info("新建任务 #%d「%s」", job_id, name)
        self.notify()
        return job_id

    async def create_backfill(self) -> int:
        items = await self.db.sources_missing_detail(self._rank)
        return await self.create_videos(items, library_id=None, name=f"补全缺失详情（{len(items)} 部）",
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

    def _check_library(self, name: str, dir: str, path_template: str, external_dir: str = "",
                       exclude_id: int | None = None) -> tuple:
        name, dir, external_dir = name.strip(), dir.strip().rstrip("/\\"), external_dir.strip().rstrip("/\\")
        if not name:
            raise ValueError("库名不能为空")
        if not dir:
            raise ValueError("目录不能为空：输出根目录本身不能作为库目录，否则别的库会嵌套在里面")
        for d in (dir, external_dir):
            if d and not Path(d).is_absolute() and ".." in Path(d).parts:
                raise ValueError("相对目录不能包含 ..")
        template = check_path_template(path_template) if path_template.strip() else ""
        root = self.writer.library_root({"dir": dir}).resolve()
        if root == self.store.output_dir.resolve():
            raise ValueError("库目录不能是输出根目录")
        mine = [root]
        if external_dir:
            ext = self.writer.library_root({"dir": external_dir}).resolve()
            if _overlap(root, ext):
                raise ValueError("外部整理目录和库目录不能互相嵌套，否则整理好的文件会被外部工具当成新文件再处理一遍")
            mine.append(ext)
        for other in self.libs.values():
            if other["id"] == exclude_id:
                continue
            for o in filter(None, (self.writer.library_root(other), self.writer.external_root(other))):
                o = o.resolve()
                if any(_overlap(m, o) for m in mine):
                    raise ValueError(f"目录和输出库「{other['name']}」（{o}）重叠，各库的目录、外部整理目录不能互相嵌套")
        return name, dir, template, external_dir

    async def create_library(self, name: str, dir: str, path_template: str = "", rule: dict | None = None,
                             external_dir: str = "", sources: list[int] = (), excludes: list[int] = ()) -> dict:
        """新建输出库；带规则、来源库或排除库时自动排一个重新归库任务。"""
        name, dir, template, external_dir = self._check_library(name, dir, path_template, external_dir)
        sources, excludes = self._check_links(None, list(sources), list(excludes))
        rule = normalize_rule(rule)
        lib_id = await self.db.create_library(name, dir, template, rule, external_dir, sources, excludes)
        await self.reload_libraries()
        lib = self.libs[lib_id]
        log.info("新建输出库「%s」：%s%s%s%s", name, self.writer.library_root(lib),
                 f"，外部整理到 {self.writer.external_root(lib)}" if external_dir else "",
                 f"，规则 {describe_rule(rule)}" if rule else "", self.describe_links(lib))
        reclassify = rule or sources or excludes
        return {"id": lib_id, "reclassify_job_id": await self.create_reclassify(lib_id) if reclassify else None}

    def describe_links(self, lib: dict) -> str:
        names = {k: "、".join(self.libs[i]["name"] for i in lib[k] if i in self.libs) for k in ("sources", "excludes")}
        return (f"，来源 {names['sources']}" if names["sources"] else "") + (
            f"，排除 {names['excludes']}" if names["excludes"] else "")

    async def update_library(self, lib_id: int, name: str, dir: str, path_template: str = "",
                             rule: dict | None = None, external_dir: str = "", sources: list[int] = (),
                             excludes: list[int] = ()) -> dict:
        """修改输出库：目录或模板变了排重写任务搬文件，规则变了排重新归库任务。

        外部整理库的文件归外部工具管，目录或模板变了也不搬，只影响以后新写的 strm；外部整理目录变了排同步位置。
        """
        old = self._library(lib_id)
        name, dir, template, external_dir = self._check_library(name, dir, path_template, external_dir, lib_id)
        sources, excludes = self._check_links(lib_id, list(sources), list(excludes))
        rule = normalize_rule(rule)
        await self.db.update_library(lib_id, name=name, dir=dir, path_template=template, rule=rule,
                                     external_dir=external_dir, sources=sources, excludes=excludes)
        await self.reload_libraries()
        jobs = {"rewrite_job_id": None, "reclassify_job_id": None, "locate_job_id": None}
        if (dir, template) != (old["dir"], old["path_template"]) and not external_dir:
            jobs["rewrite_job_id"] = await self.create_rewrite(lib_id)
        if external_dir and external_dir != old["external_dir"]:
            jobs["locate_job_id"] = await self.strm.create_locate(lib_id)
        if rule != old["rule"] or (sources, excludes) != (old["sources"], old["excludes"]):
            jobs["reclassify_job_id"] = await self.create_reclassify(lib_id)
        return jobs

    async def delete_library(self, lib_id: int, delete_files: bool) -> int:
        lib = self._library(lib_id)
        if lib_id == DEFAULT_LIBRARY_ID:
            raise ValueError("默认库不能删除，可以改名或改目录")
        if lib["subscriptions"]:
            raise ValueError("还有订阅在使用这个库，先删除或改掉这些订阅")
        users = [o["name"] for o in self.libs.values() if lib_id in o["sources"] + o["excludes"]]
        if users:
            raise ValueError(f"输出库「{'」「'.join(users)}」把它作为来源库或排除库，先改掉这些库")
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
            sub["source"], site=sub["site"], sort=sub["sort"], detail=bool(sub["detail"]), library_id=sub["library_id"],
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
            if self.paused or not (self._settle_wanted or time.time() - self._settled_at > SETTLE_INTERVAL):
                continue
            try:
                await self.settle()
            except Exception:
                log.exception("归并出错")

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


async def playlist_duration(fetcher: Fetcher, url: str, headers: dict | None = None) -> int | None:
    """m3u8 的总时长（秒）；多码率的 master 取第一个子清单再算。"""
    text = (await fetcher.get_bytes(url, headers=headers)).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in text:
        sub = next((ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")), "")
        if not sub:
            return None
        text = (await fetcher.get_bytes(urljoin(url, sub), headers=headers)).decode("utf-8", "replace")
    return m3u8_duration(text)


def _restore_text(c: Counter) -> str:
    """补回结果的日志文字。"""
    parts = [f"补回 {c['rewritten']} 部" if c["rewritten"] else "",
             f"外部工具挪走、改名的找回位置 {c['relocated']} 部" if c["relocated"] else "",
             f"外部整理库找不到、按设置没补 {c['missing']} 部" if c["missing"] else "",
             f"外部整理目录不在或是空的、没补 {c['unavailable']} 部（检查挂载）" if c["unavailable"] else ""]
    text = "，".join(p for p in parts if p)
    return f"，磁盘上丢失的：{text}" if text else ""


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def snapshot_path(boot: BootConfig, name: str) -> Path | None:
    p = (boot.data_dir / "snapshots" / name).resolve()
    return p if p.parent == (boot.data_dir / "snapshots").resolve() and p.exists() else None
