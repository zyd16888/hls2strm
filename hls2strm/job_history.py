"""任务执行日志与列表识别明细；不参与任务调度和影片匹配。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .observability import redact
from .runtime import log_area


async def migrate_v13(conn):
    await conn.execute("""CREATE TABLE job_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
        ts REAL NOT NULL, level TEXT NOT NULL, name TEXT NOT NULL, msg TEXT NOT NULL)""")
    await conn.execute("CREATE INDEX job_logs_job ON job_logs(job_id, id)")
    await conn.execute("CREATE INDEX job_logs_task ON job_logs(job_id, task_id, id)")
    await conn.execute("""CREATE TABLE crawl_items (
        job_id INTEGER NOT NULL, video_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
        site TEXT NOT NULL, source_key TEXT NOT NULL, page INTEGER NOT NULL,
        slug TEXT NOT NULL, title TEXT NOT NULL, is_new INTEGER NOT NULL,
        added INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(job_id, video_id))""")


class JobHistory:
    def __init__(self, db):
        self.db = db

    async def append_logs(self, rows):
        if not rows:
            return
        async with self.db._tx(invalidate=False) as conn:
            # 取消后删除任务时，在途子任务不能重新写出孤儿日志。
            await conn.executemany(
                "INSERT INTO job_logs(job_id,task_id,ts,level,name,msg) "
                "SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM jobs WHERE id=?)",
                [(*r, r[0]) for r in rows],
            )
            for job_id in {r[0] for r in rows}:
                await conn.execute(
                    "DELETE FROM job_logs WHERE job_id=? AND id <= "
                    "(SELECT id FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT 1 OFFSET 10000)",
                    (job_id, job_id),
                )

    async def logs(self, job_id, task_id=None, before=0, limit=200):
        where, args = "job_id=?", [job_id]
        if task_id is not None:
            where += " AND task_id=?"
            args.append(task_id)
        if before:
            where += " AND id<?"
            args.append(before)
        rows = await self.db._all(f"SELECT * FROM job_logs WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit + 1))
        items = [dict(r) for r in rows[:limit]][::-1]
        return {"items": items, "next_before": items[0]["id"] if len(rows) > limit else None}

    async def counts(self, job_ids):
        if not job_ids:
            return {}
        marks = ",".join("?" for _ in job_ids)
        rows = await self.db._all(
            f"SELECT job_id,COUNT(*) AS seen,SUM(is_new) AS new,SUM(1-is_new) AS existing,"
            f"SUM(added) AS added,SUM(excluded) AS excluded FROM crawl_items "
            f"WHERE job_id IN ({marks}) GROUP BY job_id", job_ids,
        )
        return {r["job_id"]: dict(r) for r in rows}

    async def items(self, job_id, status="", limit=50, offset=0):
        condition = {"": "", "new": " AND is_new=1", "existing": " AND is_new=0", "excluded": " AND excluded=1"}[status]
        where = "job_id=?" + condition
        total = await self.db._one(f"SELECT COUNT(*) AS n FROM crawl_items WHERE {where}", (job_id,))
        rows = await self.db._all(
            f"SELECT * FROM crawl_items WHERE {where} ORDER BY page,video_id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        )
        return {"items": [dict(r) for r in rows], "total": total["n"]}

    async def output_result(self, job_id, video_id, *, added=False, excluded=False):
        await self.db._write(
            "UPDATE crawl_items SET added=MAX(added,?),excluded=? WHERE job_id=? AND video_id=?",
            (int(added), int(excluded), job_id, video_id),
        )


_capture: ContextVar[object | None] = ContextVar("task_log_capture", default=None)


class _TaskHandler(logging.Handler):
    def __init__(self, task):
        super().__init__(logging.DEBUG)
        self.task = task
        self.rows = []

    def emit(self, record):
        if _capture.get() is not self:
            return
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        self.rows.append((self.task["job_id"], self.task["id"], record.created, record.levelname,
                          record.name.removeprefix("hls2strm."), redact(message)[:16000]))


@asynccontextmanager
async def capture_task_logs(history, task):
    handler = _TaskHandler(task)
    logger = logging.getLogger("hls2strm")
    token, area = _capture.set(handler), log_area.set("task")
    logger.addHandler(handler)
    stopping = asyncio.Event()

    async def flush():
        while True:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except TimeoutError:
                pass
            rows, handler.rows = handler.rows, []
            await history.append_logs(rows)
            if stopping.is_set():
                return

    writer = asyncio.create_task(flush())
    try:
        yield
    finally:
        logger.removeHandler(handler)
        _capture.reset(token)
        log_area.reset(area)
        stopping.set()
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            await writer
            raise
