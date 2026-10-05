"""SQLite 存储：设置、影片、输出库、订阅、任务（job）与子任务（task）。

单连接 + 自动提交；所有写操作串行（同一把锁），读操作直接执行。
表结构用 PRAGMA user_version 做版本化迁移，见 MIGRATIONS。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite

from .parser import ListItem, VideoDetail

log = logging.getLogger(__name__)

DEFAULT_LIBRARY_ID = 1
JSON_FIELDS = ("models", "categories", "tags")

SCHEMA_V1 = [
    """CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS videos (
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
      updated_at INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS videos_code ON videos(code)",
    """CREATE TABLE IF NOT EXISTS jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      name TEXT NOT NULL,
      params TEXT NOT NULL DEFAULT '{}',
      state TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL,
      error TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      started_at INTEGER,
      finished_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS tasks (
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
      UNIQUE(job_id, kind, target))""",
    "CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status, priority DESC, id)",
    "CREATE INDEX IF NOT EXISTS tasks_job ON tasks(job_id, status)",
]


async def _migrate_v1(conn: aiosqlite.Connection) -> None:
    for sql in SCHEMA_V1:
        await conn.execute(sql)


async def _migrate_v2(conn: aiosqlite.Connection) -> None:
    """输出库 + 订阅：影片和输出目录变成多对多，全局增量设置变成默认订阅。"""
    t = now()
    for sql in (
        """CREATE TABLE libraries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          dir TEXT NOT NULL,
          path_template TEXT NOT NULL DEFAULT '',
          rule TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL)""",
        """CREATE TABLE outputs (
          video_id INTEGER NOT NULL,
          library_id INTEGER NOT NULL,
          strm_path TEXT NOT NULL DEFAULT '',
          cover_done INTEGER NOT NULL DEFAULT 0,
          via TEXT NOT NULL DEFAULT 'job',
          written_at INTEGER,
          PRIMARY KEY (video_id, library_id))""",
        "CREATE INDEX outputs_library ON outputs(library_id)",
        """CREATE TABLE subscriptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          source TEXT NOT NULL,
          sort TEXT NOT NULL DEFAULT '',
          library_id INTEGER NOT NULL,
          detail INTEGER NOT NULL DEFAULT 1,
          interval INTEGER NOT NULL DEFAULT 60,
          stop_after_known INTEGER NOT NULL DEFAULT 48,
          max_pages INTEGER NOT NULL DEFAULT 20,
          enabled INTEGER NOT NULL DEFAULT 1,
          initialized INTEGER NOT NULL DEFAULT 0,
          last_run_at INTEGER,
          last_job_id INTEGER,
          created_at INTEGER NOT NULL)""",
    ):
        await conn.execute(sql)

    await conn.execute(
        "INSERT INTO libraries(id, name, dir, created_at) VALUES(?, '全部', '全部', ?)", (DEFAULT_LIBRARY_ID, t)
    )
    await conn.execute(
        """INSERT INTO outputs(video_id, library_id, strm_path, cover_done, via, written_at)
           SELECT id, ?, strm_path, cover_done, 'job', output_at FROM videos WHERE strm_path != ''""",
        (DEFAULT_LIBRARY_ID,),
    )

    async with conn.execute("SELECT value FROM settings WHERE key='settings'") as cur:
        row = await cur.fetchone()
    old = json.loads(row[0]) if row else {}
    async with conn.execute(
        "SELECT 1 FROM jobs WHERE (kind='crawl' AND status='done' AND json_extract(params, '$.full')) "
        "OR kind='incremental' LIMIT 1"
    ) as cur:
        initialized = await cur.fetchone() is not None
    await conn.execute(
        """INSERT INTO subscriptions(name, source, sort, library_id, detail, interval, stop_after_known,
                                     max_pages, initialized, created_at)
           VALUES('全站：最新更新', '/latest-updates/', 'post_date', ?, ?, ?, ?, ?, ?, ?)""",
        (DEFAULT_LIBRARY_ID, int(old.get("fetch_detail", True)), old.get("incremental_interval", 60),
         old.get("incremental_stop_after_known", 48), old.get("incremental_max_pages", 20), int(initialized), t),
    )

    for col in ("strm_path", "output_at", "cover_done"):
        await conn.execute(f"ALTER TABLE videos DROP COLUMN {col}")

    # 已有输出要搬进「全部」库的目录：排一个重写任务，启动后自动执行
    async with conn.execute("SELECT COUNT(*) FROM outputs") as cur:
        moved = (await cur.fetchone())[0]
    if moved:
        cur = await conn.execute(
            "INSERT INTO jobs(kind, name, params, status, created_at) VALUES('rewrite', ?, '{}', 'running', ?)",
            ("迁移：已有输出搬进「全部」库", t),
        )
        await conn.execute(
            "INSERT INTO tasks(job_id, kind, target, priority, updated_at) VALUES(?, 'rewrite', 'all', 20, ?)",
            (cur.lastrowid, t),
        )


MIGRATIONS = [_migrate_v1, _migrate_v2]


def now() -> int:
    return int(time.time())


def _video_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS:
        if k in d:
            d[k] = json.loads(d[k] or "[]")
    return d


def _job_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"] or "{}")
    d["state"] = json.loads(d["state"] or "{}")
    return d


def _library_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["rule"] = json.loads(d["rule"]) if d.get("rule") else None
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
        await self._migrate()

    async def _migrate(self) -> None:
        version = (await self._one("PRAGMA user_version"))[0]
        for target, step in enumerate(MIGRATIONS, start=1):
            if version >= target:
                continue
            async with self._lock:
                await self.conn.execute("BEGIN")
                try:
                    await step(self.conn)
                    await self.conn.execute(f"PRAGMA user_version={target}")
                    await self.conn.execute("COMMIT")
                except BaseException:
                    await self.conn.execute("ROLLBACK")
                    raise
            log.info("数据库已迁移到 v%d", target)

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

    async def _update(self, table: str, row_id: int, fields: dict) -> None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            await self._write(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))

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

    async def set_duration(self, video_id: int, duration: int) -> None:
        await self._write("UPDATE videos SET duration=? WHERE id=?", (duration, video_id))

    async def mark_gone(self, slug: str) -> None:
        await self._write("UPDATE videos SET status='gone', updated_at=? WHERE slug=?", (now(), slug))

    async def search_videos(self, q: str = "", flt: str = "", offset: int = 0, limit: int = 50,
                            library_id: int | None = None):
        where, params = ["1=1"], []
        if q:
            like = f"%{q.strip()}%"
            where.append("(slug LIKE ? OR code LIKE ? OR title LIKE ? OR models LIKE ? OR tags LIKE ?)")
            params += [like] * 5
        if library_id:
            where.append("EXISTS (SELECT 1 FROM outputs o WHERE o.video_id=videos.id AND o.library_id=?)")
            params.append(library_id)
        if flt == "no_detail":
            where.append("detail_at IS NULL AND status='active'")
        elif flt == "no_output":
            where.append("status='active' AND NOT EXISTS "
                         "(SELECT 1 FROM outputs o WHERE o.video_id=videos.id AND o.strm_path!='')")
        elif flt == "gone":
            where.append("status='gone'")
        cond = " AND ".join(where)
        total = (await self._one(f"SELECT COUNT(*) AS n FROM videos WHERE {cond}", params))["n"]
        rows = await self._all(
            f"SELECT * FROM videos WHERE {cond} ORDER BY id DESC LIMIT ? OFFSET ?", params + [limit, offset]
        )
        return [_video_row(r) for r in rows], total

    async def slugs_missing_detail(self) -> list[str]:
        rows = await self._all("SELECT slug FROM videos WHERE detail_at IS NULL AND status='active' ORDER BY id DESC")
        return [r["slug"] for r in rows]

    async def video_stats(self) -> dict:
        row = await self._one(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(detail_at IS NOT NULL), 0) AS with_detail,
                      COALESCE(SUM(status='gone'), 0) AS gone,
                      (SELECT COUNT(DISTINCT video_id) FROM outputs WHERE strm_path != '') AS with_strm,
                      (SELECT COUNT(DISTINCT video_id) FROM outputs WHERE cover_done) AS with_cover
               FROM videos"""
        )
        return dict(row)

    # ---- 输出库 ----

    async def list_libraries(self) -> list[dict]:
        rows = await self._all(
            """SELECT l.*, (SELECT COUNT(*) FROM outputs o WHERE o.library_id=l.id) AS videos,
                      (SELECT COUNT(*) FROM subscriptions s WHERE s.library_id=l.id) AS subscriptions
               FROM libraries l ORDER BY l.id"""
        )
        return [_library_row(r) for r in rows]

    async def get_library(self, library_id: int) -> dict | None:
        return _library_row(await self._one("SELECT * FROM libraries WHERE id=?", (library_id,)))

    async def create_library(self, name: str, dir: str, path_template: str = "", rule: dict | None = None) -> int:
        cur = await self._write(
            "INSERT INTO libraries(name, dir, path_template, rule, created_at) VALUES(?, ?, ?, ?, ?)",
            (name, dir, path_template, json.dumps(rule, ensure_ascii=False) if rule else "", now()),
        )
        return cur.lastrowid

    async def update_library(self, library_id: int, **fields) -> None:
        if "rule" in fields:
            fields["rule"] = json.dumps(fields["rule"], ensure_ascii=False) if fields["rule"] else ""
        await self._update("libraries", library_id, fields)

    async def delete_library(self, library_id: int) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM outputs WHERE library_id=?", (library_id,))
            await self.conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))

    # ---- 输出（影片 × 库） ----

    async def get_outputs(self, video_id: int) -> list[dict]:
        rows = await self._all(
            """SELECT o.*, l.name AS library_name FROM outputs o JOIN libraries l ON l.id=o.library_id
               WHERE o.video_id=? ORDER BY o.library_id""",
            (video_id,),
        )
        return [dict(r) for r in rows]

    async def outputs_for(self, video_ids: list[int]) -> dict[int, list[dict]]:
        if not video_ids:
            return {}
        marks = ",".join("?" * len(video_ids))
        rows = await self._all(
            f"""SELECT o.video_id, o.library_id, o.strm_path, o.via, l.name AS library_name
                FROM outputs o JOIN libraries l ON l.id=o.library_id
                WHERE o.video_id IN ({marks}) ORDER BY o.library_id""",
            video_ids,
        )
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(r["video_id"], []).append(dict(r))
        return out

    async def get_output(self, video_id: int, library_id: int) -> dict | None:
        row = await self._one("SELECT * FROM outputs WHERE video_id=? AND library_id=?", (video_id, library_id))
        return dict(row) if row else None

    async def ensure_output(self, video_id: int, library_id: int, via: str = "job") -> bool:
        """影片加入输出库，返回是否新加入。"""
        cur = await self._write(
            "INSERT OR IGNORE INTO outputs(video_id, library_id, via) VALUES(?, ?, ?)", (video_id, library_id, via)
        )
        return cur.rowcount > 0

    async def set_output(self, video_id: int, library_id: int, strm_path: str, cover_done: bool | None = None) -> None:
        if cover_done is None:
            await self._write(
                "UPDATE outputs SET strm_path=?, written_at=? WHERE video_id=? AND library_id=?",
                (strm_path, now(), video_id, library_id),
            )
        else:
            await self._write(
                "UPDATE outputs SET strm_path=?, written_at=?, cover_done=? WHERE video_id=? AND library_id=?",
                (strm_path, now(), int(cover_done), video_id, library_id),
            )

    async def delete_output(self, video_id: int, library_id: int) -> None:
        await self._write("DELETE FROM outputs WHERE video_id=? AND library_id=?", (video_id, library_id))

    async def iter_outputs(self, library_id: int | None = None, batch: int = 500):
        """逐批产出 (影片, 输出)。"""
        last = -1
        cond = "AND o.library_id=?" if library_id else ""
        while True:
            params: list = [last] + ([library_id] if library_id else []) + [batch]
            rows = await self._all(
                f"""SELECT o.rowid AS output_rowid, o.library_id AS out_library_id, o.strm_path AS out_strm_path,
                           o.cover_done AS out_cover_done, v.*
                    FROM outputs o JOIN videos v ON v.id=o.video_id
                    WHERE o.rowid>? {cond} ORDER BY o.rowid LIMIT ?""",
                params,
            )
            if not rows:
                return
            for r in rows:
                v = _video_row(r)
                out = {"library_id": v.pop("out_library_id"), "strm_path": v.pop("out_strm_path"),
                       "cover_done": v.pop("out_cover_done")}
                v.pop("output_rowid")
                yield v, out
            last = rows[-1]["output_rowid"]

    # ---- 订阅 ----

    async def list_subscriptions(self) -> list[dict]:
        rows = await self._all(
            """SELECT s.*, l.name AS library_name,
                      (SELECT j.id FROM jobs j WHERE j.status IN ('running', 'paused')
                         AND json_extract(j.params, '$.subscription_id')=s.id ORDER BY j.id DESC LIMIT 1) AS active_job_id
               FROM subscriptions s LEFT JOIN libraries l ON l.id=s.library_id ORDER BY s.id"""
        )
        return [dict(r) for r in rows]

    async def get_subscription(self, sub_id: int) -> dict | None:
        row = await self._one("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
        return dict(row) if row else None

    async def create_subscription(self, **fields) -> int:
        fields["created_at"] = now()
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = await self._write(f"INSERT INTO subscriptions({cols}) VALUES({marks})", tuple(fields.values()))
        return cur.lastrowid

    async def update_subscription(self, sub_id: int, **fields) -> None:
        await self._update("subscriptions", sub_id, fields)

    async def delete_subscription(self, sub_id: int) -> None:
        await self._write("DELETE FROM subscriptions WHERE id=?", (sub_id,))

    async def subscription_active_job(self, sub_id: int) -> dict | None:
        return _job_row(await self._one(
            "SELECT * FROM jobs WHERE status IN ('running', 'paused') "
            "AND json_extract(params, '$.subscription_id')=? ORDER BY id DESC LIMIT 1",
            (sub_id,),
        ))

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
        await self._update("jobs", job_id, fields)

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
