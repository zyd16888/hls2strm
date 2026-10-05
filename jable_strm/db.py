"""SQLite 存储：设置、影片、任务（job）与子任务（task）。

单连接 + 自动提交；所有写操作串行（同一把锁），读操作直接执行。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite

from .parser import ListItem, VideoDetail

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  code TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  duration INTEGER,
  thumb_url TEXT NOT NULL DEFAULT '',
  preview_url TEXT NOT NULL DEFAULT '',
  cover_url TEXT NOT NULL DEFAULT '',
  views INTEGER,
  likes INTEGER,
  favs INTEGER,
  release_date TEXT NOT NULL DEFAULT '',
  quality TEXT NOT NULL DEFAULT '',
  models TEXT NOT NULL DEFAULT '[]',
  categories TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  hls_url TEXT NOT NULL DEFAULT '',
  hls_expires INTEGER,
  detail_at INTEGER,
  strm_path TEXT NOT NULL DEFAULT '',
  output_at INTEGER,
  cover_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_code ON videos(code);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_run_at INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER,
  updated_at INTEGER NOT NULL,
  UNIQUE(job_id, kind, target)
);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status, priority DESC, id);
CREATE INDEX IF NOT EXISTS tasks_job ON tasks(job_id, status);
"""

JSON_FIELDS = ("models", "categories", "tags")
OPEN_STATUSES = ("pending", "running")


def now() -> int:
    return int(time.time())


def _video_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS:
        d[k] = json.loads(d[k] or "[]")
    return d


def _job_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"] or "{}")
    d["state"] = json.loads(d["state"] or "{}")
    return d


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # ---- 基础 ----

    async def _write(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        async with self._lock:
            return await self.conn.execute(sql, tuple(params))

    async def _write_many(self, sql: str, rows: list[tuple]) -> int:
        async with self._lock:
            await self.conn.execute("BEGIN")
            try:
                before = self.conn.total_changes
                await self.conn.executemany(sql, rows)
                await self.conn.execute("COMMIT")
                return self.conn.total_changes - before
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise

    async def _one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    # ---- 设置 ----

    async def get_setting(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._write(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- 影片 ----

    async def upsert_list_item(self, it: ListItem) -> bool:
        """列表页数据入库，返回是否为新影片。"""
        t = now()
        exists = await self._one("SELECT 1 FROM videos WHERE id=?", (it.video_id,))
        await self._write(
            """
            INSERT INTO videos(id, slug, code, title, duration, thumb_url, preview_url, views, likes,
                               created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              slug=excluded.slug, code=excluded.code, title=excluded.title,
              duration=COALESCE(excluded.duration, duration),
              thumb_url=excluded.thumb_url, preview_url=excluded.preview_url,
              views=COALESCE(excluded.views, views), likes=COALESCE(excluded.likes, likes),
              status='active', updated_at=excluded.updated_at
            """,
            (it.video_id, it.slug, it.code, it.title, it.duration, it.thumb_url, it.preview_url,
             it.views, it.likes, t, t),
        )
        return exists is None

    async def upsert_detail(self, d: VideoDetail) -> None:
        t = now()
        await self._write(
            """
            INSERT INTO videos(id, slug, code, title, cover_url, release_date, quality, views, favs,
                               models, categories, tags, hls_url, hls_expires, detail_at,
                               created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              slug=excluded.slug, code=excluded.code, title=excluded.title,
              cover_url=excluded.cover_url, release_date=excluded.release_date, quality=excluded.quality,
              views=COALESCE(excluded.views, views), favs=COALESCE(excluded.favs, favs),
              models=excluded.models, categories=excluded.categories, tags=excluded.tags,
              hls_url=excluded.hls_url, hls_expires=excluded.hls_expires, detail_at=excluded.detail_at,
              status='active', updated_at=excluded.updated_at
            """,
            (d.video_id, d.slug, d.code, d.title, d.cover_url, d.release_date, d.quality, d.views, d.favs,
             json.dumps(d.models, ensure_ascii=False), json.dumps(d.categories, ensure_ascii=False),
             json.dumps(d.tags, ensure_ascii=False), d.hls_url, d.hls_expires, t, t, t),
        )

    async def get_video(self, slug: str) -> dict | None:
        return _video_row(await self._one("SELECT * FROM videos WHERE slug=?", (slug.lower(),)))

    async def set_output(self, video_id: int, strm_path: str, cover_done: bool | None = None) -> None:
        if cover_done is None:
            await self._write(
                "UPDATE videos SET strm_path=?, output_at=? WHERE id=?", (strm_path, now(), video_id)
            )
        else:
            await self._write(
                "UPDATE videos SET strm_path=?, output_at=?, cover_done=? WHERE id=?",
                (strm_path, now(), int(cover_done), video_id),
            )

    async def set_duration(self, video_id: int, duration: int) -> None:
        await self._write("UPDATE videos SET duration=? WHERE id=?", (duration, video_id))

    async def mark_gone(self, slug: str) -> None:
        await self._write("UPDATE videos SET status='gone', updated_at=? WHERE slug=?", (now(), slug))

    async def search_videos(self, q: str = "", flt: str = "", offset: int = 0, limit: int = 50):
        where, params = ["1=1"], []
        if q:
            like = f"%{q.strip()}%"
            where.append("(slug LIKE ? OR code LIKE ? OR title LIKE ? OR models LIKE ? OR tags LIKE ?)")
            params += [like] * 5
        if flt == "no_detail":
            where.append("detail_at IS NULL AND status='active'")
        elif flt == "no_output":
            where.append("strm_path='' AND status='active'")
        elif flt == "gone":
            where.append("status='gone'")
        cond = " AND ".join(where)
        total = (await self._one(f"SELECT COUNT(*) AS n FROM videos WHERE {cond}", params))["n"]
        rows = await self._all(
            f"SELECT * FROM videos WHERE {cond} ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset]
        )
        return [_video_row(r) for r in rows], total

    async def iter_videos(self, batch: int = 500):
        last = -1
        while True:
            rows = await self._all(
                "SELECT * FROM videos WHERE status='active' AND id>? ORDER BY id LIMIT ?", (last, batch)
            )
            if not rows:
                return
            for r in rows:
                yield _video_row(r)
            last = rows[-1]["id"]

    async def slugs_missing_detail(self) -> list[str]:
        rows = await self._all("SELECT slug FROM videos WHERE detail_at IS NULL AND status='active' ORDER BY id DESC")
        return [r["slug"] for r in rows]

    async def video_stats(self) -> dict:
        row = await self._one(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(detail_at IS NOT NULL), 0) AS with_detail,
                      COALESCE(SUM(strm_path != ''), 0) AS with_strm,
                      COALESCE(SUM(cover_done), 0) AS with_cover,
                      COALESCE(SUM(status='gone'), 0) AS gone
               FROM videos"""
        )
        return dict(row)

    # ---- job ----

    async def create_job(self, kind: str, name: str, params: dict, status: str = "running") -> int:
        cur = await self._write(
            "INSERT INTO jobs(kind, name, params, status, created_at) VALUES(?, ?, ?, ?, ?)",
            (kind, name, json.dumps(params, ensure_ascii=False), status, now()),
        )
        return cur.lastrowid

    async def get_job(self, job_id: int) -> dict | None:
        return _job_row(await self._one("SELECT * FROM jobs WHERE id=?", (job_id,)))

    async def update_job(self, job_id: int, **fields) -> None:
        if "state" in fields:
            fields["state"] = json.dumps(fields["state"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._write(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    async def list_jobs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = await self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        jobs = [_job_row(r) for r in rows]
        if jobs:
            ids = [j["id"] for j in jobs]
            marks = ",".join("?" * len(ids))
            counts = await self._all(
                f"SELECT job_id, status, COUNT(*) AS n FROM tasks WHERE job_id IN ({marks}) GROUP BY job_id, status",
                ids,
            )
            by_job: dict[int, dict] = {i: {} for i in ids}
            for c in counts:
                by_job[c["job_id"]][c["status"]] = c["n"]
            for j in jobs:
                j["tasks"] = by_job[j["id"]]
        return jobs

    async def active_job(self, kind: str) -> dict | None:
        return _job_row(
            await self._one(
                "SELECT * FROM jobs WHERE kind=? AND status IN ('running', 'paused') ORDER BY id DESC LIMIT 1",
                (kind,),
            )
        )

    async def incremental_armed(self) -> bool:
        """跑完过全站，或者手动跑过增量，才自动定时增量。"""
        row = await self._one(
            "SELECT 1 FROM jobs WHERE (kind='crawl' AND status='done' AND json_extract(params, '$.full')) "
            "OR kind='incremental' LIMIT 1"
        )
        return row is not None

    async def last_job_time(self, kind: str) -> int | None:
        row = await self._one("SELECT MAX(created_at) AS t FROM jobs WHERE kind=?", (kind,))
        return row["t"] if row else None

    async def delete_job(self, job_id: int) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM tasks WHERE job_id=?", (job_id,))
            await self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    # ---- task ----

    async def add_tasks(self, job_id: int, kind: str, targets: Iterable[str], priority: int = 0) -> int:
        t = now()
        rows = [(job_id, kind, str(x), priority, t) for x in targets]
        if not rows:
            return 0
        return await self._write_many(
            "INSERT OR IGNORE INTO tasks(job_id, kind, target, priority, updated_at) VALUES(?, ?, ?, ?, ?)", rows
        )

    async def claim_task(self) -> dict | None:
        t = now()
        async with self._lock:
            async with self.conn.execute(
                """
                UPDATE tasks SET status='running', attempts=attempts+1, updated_at=?
                WHERE id = (
                  SELECT t.id FROM tasks t JOIN jobs j ON j.id = t.job_id
                  WHERE t.status='pending' AND t.next_run_at<=? AND j.status='running'
                  ORDER BY t.priority DESC, t.id LIMIT 1)
                RETURNING *
                """,
                (t, t),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def finish_task(
        self,
        task_id: int,
        status: str,
        error: str = "",
        duration_ms: int | None = None,
        next_run_at: int = 0,
        refund_attempt: bool = False,
    ) -> None:
        await self._write(
            """UPDATE tasks SET status=?, last_error=?, duration_ms=?, next_run_at=?,
                      attempts=attempts-?, updated_at=? WHERE id=?""",
            (status, error[:2000], duration_ms, next_run_at, int(refund_attempt), now(), task_id),
        )

    async def reset_running_tasks(self) -> int:
        cur = await self._write("UPDATE tasks SET status='pending' WHERE status='running'")
        return cur.rowcount

    async def open_task_count(self, job_id: int) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM tasks WHERE job_id=? AND status IN ('pending', 'running')", (job_id,)
        )
        return row["n"]

    async def task_counts(self, job_id: int) -> dict:
        rows = await self._all("SELECT status, COUNT(*) AS n FROM tasks WHERE job_id=? GROUP BY status", (job_id,))
        return {r["status"]: r["n"] for r in rows}

    async def list_tasks(self, job_id: int, status: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        if status:
            rows = await self._all(
                "SELECT * FROM tasks WHERE job_id=? AND status=? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (job_id, status, limit, offset),
            )
        else:
            rows = await self._all(
                "SELECT * FROM tasks WHERE job_id=? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            )
        return [dict(r) for r in rows]

    async def retry_failed_tasks(self, job_id: int) -> int:
        cur = await self._write(
            "UPDATE tasks SET status='pending', attempts=0, next_run_at=0, updated_at=? WHERE job_id=? AND status='failed'",
            (now(), job_id),
        )
        return cur.rowcount

    async def cancel_open_tasks(self, job_id: int) -> int:
        cur = await self._write(
            "UPDATE tasks SET status='cancelled', updated_at=? WHERE job_id=? AND status='pending'", (now(), job_id)
        )
        return cur.rowcount

    async def queue_stats(self) -> dict:
        rows = await self._all(
            """SELECT t.kind, t.status, COUNT(*) AS n FROM tasks t JOIN jobs j ON j.id=t.job_id
               WHERE j.status IN ('running', 'paused') GROUP BY t.kind, t.status"""
        )
        out: dict[str, dict] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        return out

    async def recent_failures(self, limit: int = 10) -> list[dict]:
        rows = await self._all(
            "SELECT id, job_id, kind, target, attempts, last_error, updated_at FROM tasks "
            "WHERE status='failed' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
