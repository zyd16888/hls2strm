"""SQLite 存储：设置、作品与源、输出库、订阅、任务（job）与子任务（task）。

作品（videos 表）是一个番号，对应一个 strm；源（sources 表）是某个站点上的一个页面，一部作品可以有多个源。
写入、快速读取、批量查询分连接；写操作串行，事务内读取使用原连接。
表结构用 PRAGMA user_version 做版本化迁移，见 MIGRATIONS。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .codes import code_key
from .queries import FACET_FIELDS, VIDEO_SORTS, VideoQuery  # API 和旧调用方保持导入兼容
from .cache import AsyncCache
from .database_indexes import migrate_v12 as _migrate_v12
from .job_history import JobHistory, migrate_v13 as _migrate_v13
from .subscription_schedule import migrate_v14 as _migrate_v14, next_run_at
from .runtime import stage
from .quality import TRUST, Quality, parse_heights
from .sites import SourceDetail, SourceItem

log = logging.getLogger(__name__)

DEFAULT_LIBRARY_ID = 1
JSON_FIELDS = ("models", "categories", "tags")
LIBRARY_LINKS = ("sources", "excludes")  # 输出库的来源库、排除库（库 id 列表）

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


async def _migrate_v3(conn: aiosqlite.Connection) -> None:
    """strm 扫描结果与改前缀记录。"""
    for sql in (
        """CREATE TABLE strm_files (
          path TEXT PRIMARY KEY,
          scan_id INTEGER NOT NULL,
          url TEXT NOT NULL DEFAULT '',
          prefix TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL,
          slug TEXT NOT NULL DEFAULT '',
          video_id INTEGER,
          expired INTEGER NOT NULL DEFAULT 0,
          managed INTEGER NOT NULL DEFAULT 0,
          library_id INTEGER,
          mtime INTEGER,
          note TEXT NOT NULL DEFAULT '',
          scanned_at INTEGER NOT NULL)""",
        "CREATE INDEX strm_files_scan ON strm_files(scan_id, kind)",
        """CREATE TABLE strm_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          change_set INTEGER NOT NULL,
          path TEXT NOT NULL,
          old TEXT NOT NULL,
          new TEXT NOT NULL,
          reverted INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL)""",
        "CREATE INDEX strm_changes_set ON strm_changes(change_set)",
    ):
        await conn.execute(sql)


async def _migrate_v4(conn: aiosqlite.Connection) -> None:
    """输出库的外部整理目录：strm 交给 mdcng 等外部工具移动、刮削。"""
    await conn.execute("ALTER TABLE libraries ADD COLUMN external_dir TEXT NOT NULL DEFAULT ''")


async def _migrate_v5(conn: aiosqlite.Connection) -> None:
    """输出库的来源库、排除库（按库归属自动归入，分库互斥）。"""
    await conn.execute("ALTER TABLE libraries ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")
    await conn.execute("ALTER TABLE libraries ADD COLUMN excludes TEXT NOT NULL DEFAULT '[]'")


async def _migrate_v6(conn: aiosqlite.Connection) -> None:
    """多站点：作品（videos）和源（sources）分开，播放地址搬到源上；每部老影片生成一个 Jable 源。"""
    for sql in (
        """CREATE TABLE sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          video_id INTEGER NOT NULL,
          site TEXT NOT NULL,
          key TEXT NOT NULL,
          site_vid TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          subtitle TEXT NOT NULL DEFAULT '',
          stream_url TEXT NOT NULL DEFAULT '',
          stream_expires INTEGER,
          height INTEGER,
          status TEXT NOT NULL DEFAULT 'active',
          fail_streak INTEGER NOT NULL DEFAULT 0,
          last_ok_at INTEGER,
          last_fail_at INTEGER,
          last_error TEXT NOT NULL DEFAULT '',
          detail_at INTEGER,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(site, key))""",
        "CREATE INDEX sources_video ON sources(video_id)",
        "CREATE INDEX sources_site_vid ON sources(site, site_vid)",
        """CREATE TABLE source_checks (
          video_id INTEGER NOT NULL,
          site TEXT NOT NULL,
          found INTEGER NOT NULL,
          checked_at INTEGER NOT NULL,
          PRIMARY KEY (video_id, site))""",
        "ALTER TABLE videos ADD COLUMN code_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE videos ADD COLUMN uncensored INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE videos ADD COLUMN maker TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE videos ADD COLUMN director TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE videos ADD COLUMN series TEXT NOT NULL DEFAULT ''",
        """INSERT INTO sources(video_id, site, key, site_vid, title, subtitle, stream_url, stream_expires, status,
                               detail_at, created_at, updated_at)
           SELECT id, 'jable', slug, CAST(id AS TEXT), title,
                  CASE WHEN quality LIKE '%中文字幕%' OR categories LIKE '%"chinese-subtitle"%' THEN 'zh' ELSE '' END,
                  hls_url, hls_expires, status, detail_at, created_at, updated_at FROM videos""",
        """UPDATE videos SET uncensored=1 WHERE categories LIKE '%"uncensored"%'""",
        "ALTER TABLE videos DROP COLUMN hls_url",
        "ALTER TABLE videos DROP COLUMN hls_expires",
        "ALTER TABLE tasks ADD COLUMN site TEXT NOT NULL DEFAULT ''",
        "UPDATE tasks SET site='jable' WHERE kind IN ('list', 'detail')",
        "ALTER TABLE subscriptions ADD COLUMN site TEXT NOT NULL DEFAULT 'jable'",
    ):
        await conn.execute(sql)
    async with conn.execute("SELECT id, code, slug FROM videos") as cur:
        rows = await cur.fetchall()
    await conn.executemany("UPDATE videos SET code_key=? WHERE id=?", [(code_key(r[1] or r[2]), r[0]) for r in rows])
    await conn.execute("CREATE INDEX videos_code_key ON videos(code_key)")


async def _migrate_v7(conn: aiosqlite.Connection) -> None:
    """多线路站点：每个源的线路，各自缓存直链、记健康度；源上记当前在用的线路。"""
    for sql in (
        """CREATE TABLE source_lines (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL,
          line TEXT NOT NULL,
          link TEXT NOT NULL DEFAULT '',
          host TEXT NOT NULL DEFAULT '',
          referer TEXT NOT NULL DEFAULT '',
          stream_url TEXT NOT NULL DEFAULT '',
          stream_expires INTEGER,
          fail_streak INTEGER NOT NULL DEFAULT 0,
          last_ok_at INTEGER,
          last_fail_at INTEGER,
          last_error TEXT NOT NULL DEFAULT '',
          updated_at INTEGER NOT NULL,
          UNIQUE(source_id, line))""",
        "ALTER TABLE sources ADD COLUMN line TEXT NOT NULL DEFAULT ''",
    ):
        await conn.execute(sql)


async def _migrate_v8(conn: aiosqlite.Connection) -> None:
    """SupJav 的备用域名 supjav.org 被停放后只回一段跳转，曾被当成「搜索结果为空」：
    清掉 SupJav 没找到的补源记录，补源时重新查。"""
    await conn.execute("DELETE FROM source_checks WHERE site='supjav' AND found=0")


async def _migrate_v9(conn: aiosqlite.Connection) -> None:
    """画质：源和线路各记各档分辨率（heights，从高到低）、来源、上次探测时间；线路也记最高分辨率。
    之前中转主播放列表时记下的 height 算作来自主播放列表。"""
    for table in ("sources", "source_lines"):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN heights TEXT NOT NULL DEFAULT ''")
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN quality_src TEXT NOT NULL DEFAULT ''")
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN quality_at INTEGER")
    await conn.execute("ALTER TABLE source_lines ADD COLUMN height INTEGER")
    await conn.execute(
        "UPDATE sources SET heights=CAST(height AS TEXT), quality_src='master' WHERE height IS NOT NULL AND height>0")


async def _migrate_v10(conn: aiosqlite.Connection) -> None:
    """各播放站的连通性（成功率、速度、最近的错误），重启后接着用。"""
    await conn.execute(
        "CREATE TABLE stream_health (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at INTEGER NOT NULL)")


async def _migrate_v11(conn: aiosqlite.Connection) -> None:
    """输出库的多画质版本：空 = 不写；emby =「目录名 - 720p.strm」；suffix =「文件名-720p.strm」（mdcng 的写法）。"""
    await conn.execute("ALTER TABLE libraries ADD COLUMN versions TEXT NOT NULL DEFAULT ''")


MIGRATIONS = [_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7,
              _migrate_v8, _migrate_v9, _migrate_v10, _migrate_v11, _migrate_v12, _migrate_v13, _migrate_v14]
WORK_LIST_FIELDS = ("title", "duration", "thumb_url", "preview_url", "views", "likes")
WORK_DETAIL_FIELDS = ("title", "duration", "cover_url", "release_date", "quality", "views", "favs", "models",
                      "categories", "tags", "maker", "director", "series")
SOURCE_FAIL_COOLDOWN = 300
SOURCE_FAIL_COOLDOWN_MAX = 6 * 3600


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


def _empty(x) -> bool:
    return x is None or x == "" or x == []


def _merge(cur: dict, new: dict, primary: bool) -> dict:
    """作品元数据合并：优先级最高的源覆盖（它没有的字段保留原值），其他源只补空字段。"""
    out = {}
    for k, v in new.items():
        if _empty(v):
            continue
        if primary or _empty(cur.get(k)):
            out[k] = v
    return out


def source_cooldown(src: dict, base: int = SOURCE_FAIL_COOLDOWN, maximum: int = SOURCE_FAIL_COOLDOWN_MAX) -> int:
    """源连续失败后的冷却截止时间（0 表示不在冷却）：5 分钟起，每次翻倍，最长 6 小时。"""
    if not base or not src.get("fail_streak") or not src.get("last_fail_at"):
        return 0
    duration = base * 2 ** (src["fail_streak"] - 1)
    return src["last_fail_at"] + (min(maximum, duration) if maximum else duration)


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
    for k in LIBRARY_LINKS:
        d[k] = json.loads(d[k])
    return d


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.reader: aiosqlite.Connection | None = None
        self.bulk_reader: aiosqlite.Connection | None = None
        self._transaction_owner = None
        self.cache = AsyncCache()
        self.metrics = None
        self.subscription_schedule_started_at = 0
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self._migrate()
        for attr in ("reader", "bulk_reader"):
            conn = await aiosqlite.connect(self.path, isolation_level=None)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA query_only=ON")
            setattr(self, attr, conn)

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
        await self.cache.close()
        for attr in ("reader", "bulk_reader"):
            conn = getattr(self, attr)
            if conn is not None:
                await conn.close()
                setattr(self, attr, None)
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # ---- 基础 ----

    async def _write(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        async with self._lock:
            result = await self.conn.execute(sql, tuple(params))
            if any(name in sql.lower() for name in ("videos", "outputs", "sources")):
                self.cache.clear()
            return result

    @asynccontextmanager
    async def _tx(self, *, invalidate=True):
        """持锁的事务：查找再插入这类需要原子性的写操作用。"""
        async with self._lock:
            self._transaction_owner = asyncio.current_task()
            try:
                await self.conn.execute("BEGIN")
                yield self.conn
                await self.conn.execute("COMMIT")
                if invalidate:
                    self.cache.clear()
            except BaseException:
                try:
                    await self.conn.execute("ROLLBACK")
                except aiosqlite.OperationalError:
                    pass  # BEGIN 尚未执行或 COMMIT 已完成时可能没有活动事务。
                raise
            finally:
                self._transaction_owner = None
                if invalidate:
                    self.cache.clear()

    async def _write_many(self, sql: str, rows: list[tuple]) -> int:
        async with self._lock:
            try:
                await self.conn.execute("BEGIN")
                before = self.conn.total_changes
                await self.conn.executemany(sql, rows)
                await self.conn.execute("COMMIT")
                return self.conn.total_changes - before
            except BaseException:
                try:
                    await self.conn.execute("ROLLBACK")
                except aiosqlite.OperationalError:
                    pass
                raise

    async def _one(self, sql: str, params: Iterable[Any] = (), *, bulk: bool = False) -> aiosqlite.Row | None:
        rows = await self._all(sql, params, bulk=bulk)
        return rows[0] if rows else None

    async def _all(self, sql: str, params: Iterable[Any] = (), *, bulk: bool = False) -> list[aiosqlite.Row]:
        # 执行和取结果放在同一次线程调用里：分两步的话，中间别的协程在同一连接上改了表（比如 worker 领子任务），
        # 读到一半的 GROUP BY 会把挪了位置的行数两次
        conn = self.conn if self._transaction_owner is asyncio.current_task() else ((self.bulk_reader if bulk else self.reader) or self.conn)
        if self.metrics is None:
            return list(await conn.execute_fetchall(sql, tuple(params)))
        with stage(self.metrics, "db.bulk" if bulk else "db.read"):
            return list(await conn.execute_fetchall(sql, tuple(params)))

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

    # ---- 作品与源 ----

    async def _attach(self, site: str, key: str, code: str, uncensored: bool, slug: str, site_vid: str,
                      title: str, subtitle: str, video_id: int | None = None) -> tuple[int, int, bool]:
        """在事务里调用：找到或新建这个源和它所属的作品，返回 (作品 id, 源 id, 是否新作品)。

        已有的源直接用；新源挂到指定的作品（video_id，补源时用），没指定就按番号匹配键 + 是否无码流出找作品，
        找不到就新建（slug 撞了加序号）。
        """
        t = now()
        async with self.conn.execute("SELECT id, video_id FROM sources WHERE site=? AND key=?", (site, key)) as cur:
            src = await cur.fetchone()
        if src is not None:
            await self.conn.execute(
                """UPDATE sources SET site_vid=CASE WHEN ?!='' THEN ? ELSE site_vid END,
                          title=CASE WHEN ?!='' THEN ? ELSE title END,
                          subtitle=CASE WHEN ?!='' THEN ? ELSE subtitle END, status='active', updated_at=?
                   WHERE id=?""",
                (site_vid, site_vid, title, title, subtitle, subtitle, t, src["id"]),
            )
            return src["video_id"], src["id"], False
        ck = code_key(code)
        work = {"id": video_id} if video_id else None
        if ck and work is None:
            async with self.conn.execute(
                "SELECT id FROM videos WHERE code_key=? AND uncensored=? ORDER BY status='active' DESC, id LIMIT 1",
                (ck, int(uncensored)),
            ) as cur:
                work = await cur.fetchone()
        created = work is None
        if created:
            base, n = slug, 1
            while True:
                async with self.conn.execute("SELECT 1 FROM videos WHERE slug=?", (slug,)) as cur:
                    if await cur.fetchone() is None:
                        break
                n += 1
                slug = f"{base}-{n}"
            cur = await self.conn.execute(
                "INSERT INTO videos(slug, code, code_key, uncensored, title, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (slug, code.upper(), ck, int(uncensored), title, t, t),
            )
            video_id = cur.lastrowid
        else:
            video_id = work["id"]
        cur = await self.conn.execute(
            "INSERT INTO sources(video_id, site, key, site_vid, title, subtitle, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (video_id, site, key, site_vid, title, subtitle, t, t),
        )
        return video_id, cur.lastrowid, created

    async def _is_primary(self, video_id: int, site: str, rank: Callable[[str], int]) -> bool:
        """这个站点是不是作品现有可用源里优先级最高的（元数据以它为准）。"""
        async with self.conn.execute("SELECT DISTINCT site FROM sources WHERE video_id=? AND status='active'",
                                     (video_id,)) as cur:
            sites = [r["site"] for r in await cur.fetchall()]
        return all(rank(other) >= rank(site) for other in sites)

    async def _update_work(self, video_id: int, fields: dict, code: str = "") -> None:
        t = now()
        sets = dict(fields)
        for k in ("models", "categories", "tags"):
            if k in sets:
                sets[k] = json.dumps(sets[k], ensure_ascii=False)
        if code:
            sets["code"] = code.upper()
            sets["code_key"] = code_key(code)
        sets["status"] = "active"
        sets["updated_at"] = t
        cols = ", ".join(f"{k}=?" for k in sets)
        await self.conn.execute(f"UPDATE videos SET {cols} WHERE id=?", (*sets.values(), video_id))

    async def upsert_item(self, site: str, it: SourceItem, slug: str, rank: Callable[[str], int],
                          video_id: int | None = None, *, crawl: tuple[int, int, int] | None = None) -> tuple[int, bool]:
        """列表页数据入库：找到或新建作品，记下这个源。返回 (作品 id, 是否新作品)。video_id 见 _attach。"""
        async with self._tx():
            video_id, _, created = await self._attach(site, it.key, it.code, it.uncensored, slug, it.site_vid,
                                                      it.title, it.subtitle, video_id)
            primary = await self._is_primary(video_id, site, rank)
            async with self.conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)) as cur:
                work = dict(await cur.fetchone())
            new = {k: getattr(it, k) for k in WORK_LIST_FIELDS}
            await self._update_work(video_id, _merge(work, new, primary), it.code if primary else "")
            if crawl is not None:
                job_id, task_id, page = crawl
                await self.conn.execute(
                    "INSERT OR IGNORE INTO crawl_items(job_id,video_id,task_id,site,source_key,page,slug,title,is_new) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (job_id, video_id, task_id, site, it.key, page, work["slug"], it.title, int(created)),
                )
        return video_id, created

    async def upsert_detail(self, site: str, d: SourceDetail, slug: str, rank: Callable[[str], int],
                            video_id: int | None = None) -> int:
        """详情页数据入库：更新这个源（播放地址等）并合并作品元数据，返回作品 id。video_id 见 _attach。"""
        t = now()
        async with self._tx():
            video_id, source_id, _ = await self._attach(site, d.key, d.code, d.uncensored, slug, d.site_vid,
                                                        d.title, d.subtitle, video_id)
            if d.lines:
                await self._set_lines(source_id, d.lines)
                await self.conn.execute(
                    "UPDATE sources SET subtitle=?, detail_at=?, status='active', updated_at=? WHERE id=?",
                    (d.subtitle, t, t, source_id),
                )
            else:
                await self.conn.execute(
                    """UPDATE sources SET stream_url=?, stream_expires=?, subtitle=?, detail_at=?, status='active',
                              fail_streak=0, last_ok_at=?, last_error='', updated_at=? WHERE id=?""",
                    (d.stream_url, d.stream_expires, d.subtitle, t, t, t, source_id),
                )
            if d.claimed_height:  # 站点标注的画质：只在还没有更可靠的结果时记
                await self.conn.execute(
                    "UPDATE sources SET height=?, heights=?, quality_src='claimed' WHERE id=? AND quality_src IN ('', 'claimed')",
                    (d.claimed_height, str(d.claimed_height), source_id),
                )
            primary = await self._is_primary(video_id, site, rank)
            async with self.conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)) as cur:
                work = _video_row(await cur.fetchone())
            new = {k: getattr(d, k) for k in WORK_DETAIL_FIELDS}
            fields = _merge(work, new, primary)
            fields["detail_at"] = t
            await self._update_work(video_id, fields, d.code if primary else "")
        return video_id

    async def _set_lines(self, source_id: int, lines: list[tuple[str, str]]) -> None:
        """在事务里调用：按详情页更新源的线路；线路数据变了就清掉缓存的直链，页面上没有了的线路删掉。"""
        t = now()
        for name, link in lines:
            await self.conn.execute(
                """INSERT INTO source_lines(source_id, line, link, updated_at) VALUES(?, ?, ?, ?)
                   ON CONFLICT(source_id, line) DO UPDATE SET
                     stream_url=CASE WHEN link!=excluded.link THEN '' ELSE stream_url END,
                     stream_expires=CASE WHEN link!=excluded.link THEN NULL ELSE stream_expires END,
                     link=excluded.link, updated_at=excluded.updated_at""",
                (source_id, name, link, t),
            )
        names = [n for n, _ in lines]
        await self.conn.execute(
            f"DELETE FROM source_lines WHERE source_id=? AND line NOT IN ({','.join('?' * len(names))})",
            (source_id, *names),
        )

    async def set_lines(self, source_id: int, lines: list[tuple[str, str]]) -> None:
        async with self._tx():
            await self._set_lines(source_id, lines)

    async def get_lines(self, source_id: int) -> list[dict]:
        rows = await self._all("SELECT * FROM source_lines WHERE source_id=? ORDER BY id", (source_id,))
        return [dict(r) for r in rows]

    async def lines_for(self, source_ids: list[int]) -> dict[int, list[dict]]:
        if not source_ids:
            return {}
        marks = ",".join("?" * len(source_ids))
        rows = await self._all(f"SELECT * FROM source_lines WHERE source_id IN ({marks}) ORDER BY id", source_ids)
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(r["source_id"], []).append(dict(r))
        return out

    async def set_line_stream(self, line_id: int, url: str, expires: int | None, host: str, referer: str = "",
                              *, use: bool = True) -> None:
        """线路取到直链：记下直链（和中转时要带的 Referer）；use 时设成这个源当前在用的线路
        （探测画质时不切：正在中转的播放只认当前线路）。"""
        t = now()
        async with self._tx():
            await self.conn.execute(
                """UPDATE source_lines SET stream_url=?, stream_expires=?, host=?, referer=?, fail_streak=0,
                          last_ok_at=?, last_error='', updated_at=? WHERE id=?""",
                (url, expires, host, referer, t, t, line_id),
            )
            if not use:
                return
            await self.conn.execute(
                """UPDATE sources SET stream_url=?, stream_expires=?, line=(SELECT line FROM source_lines WHERE id=?),
                          status='active', fail_streak=0, last_ok_at=?, last_error='', updated_at=?
                   WHERE id=(SELECT source_id FROM source_lines WHERE id=?)""",
                (url, expires, line_id, t, t, line_id),
            )

    async def set_quality(self, source_id: int, line_id: int | None, q: Quality | None) -> bool:
        """记下探测到的画质，可信度低的不覆盖高的；q 为 None 表示探测过但没认出来（mp4 等），只记时间、过一阵再试。
        线路的画质变了，源上跟着记各线路里最好的那份（挑源时用）。返回源上的各档画质变了没有（多画质版本要跟着改）。"""
        t = now()
        table, row_id = ("source_lines", line_id) if line_id else ("sources", source_id)
        async with self._tx():
            async with self.conn.execute(f"SELECT quality_src FROM {table} WHERE id=?", (row_id,)) as cur:
                row = await cur.fetchone()
            async with self.conn.execute("SELECT heights FROM sources WHERE id=?", (source_id,)) as cur:
                before = await cur.fetchone()
            if row is None or before is None:
                return False
            if q is None or not q.heights or TRUST[q.src] < TRUST.get(row["quality_src"], 0):
                await self.conn.execute(f"UPDATE {table} SET quality_at=? WHERE id=?", (t, row_id))
                return False
            heights = ",".join(map(str, q.heights))
            await self.conn.execute(
                f"UPDATE {table} SET height=?, heights=?, quality_src=?, quality_at=? WHERE id=?",
                (q.height, heights, q.src, t, row_id),
            )
            if line_id:
                async with self.conn.execute(
                    "SELECT heights, quality_src FROM source_lines WHERE source_id=? AND height>0 ORDER BY height DESC",
                    (source_id,),
                ) as cur:
                    lines = await cur.fetchall()
                merged = parse_heights([h for ln in lines for h in parse_heights(ln["heights"])])
                heights = ",".join(map(str, merged))
                await self.conn.execute(
                    "UPDATE sources SET height=?, heights=?, quality_src=?, quality_at=? WHERE id=?",
                    (merged[0], heights, lines[0]["quality_src"], t, source_id),
                )
            return heights != before["heights"]

    async def use_line(self, source_id: int, line: dict) -> None:
        """改用这条线路已缓存的直链。"""
        await self._write("UPDATE sources SET stream_url=?, stream_expires=?, line=? WHERE id=?",
                          (line["stream_url"], line["stream_expires"], line["line"], source_id))

    async def line_failed(self, line_id: int, error: str) -> None:
        await self._write(
            "UPDATE source_lines SET fail_streak=fail_streak+1, last_fail_at=?, last_error=? WHERE id=?",
            (now(), error[:500], line_id),
        )

    async def get_video_by_id(self, video_id: int) -> dict | None:
        return _video_row(await self._one("SELECT * FROM videos WHERE id=?", (video_id,)))

    async def get_sources(self, video_id: int) -> list[dict]:
        return [dict(r) for r in await self._all("SELECT * FROM sources WHERE video_id=? ORDER BY id", (video_id,))]

    async def sources_for(self, video_ids: list[int]) -> dict[int, list[dict]]:
        if not video_ids:
            return {}
        marks = ",".join("?" * len(video_ids))
        rows = await self._all(f"SELECT * FROM sources WHERE video_id IN ({marks}) ORDER BY id", video_ids)
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(r["video_id"], []).append(dict(r))
        return out

    async def get_source(self, source_id: int) -> dict | None:
        row = await self._one("SELECT * FROM sources WHERE id=?", (source_id,))
        return dict(row) if row else None

    async def find_source(self, site: str, key: str) -> dict | None:
        row = await self._one("SELECT * FROM sources WHERE site=? AND key=?", (site, key))
        return dict(row) if row else None

    async def set_stream(self, source_id: int, url: str, expires: int | None) -> None:
        t = now()
        await self._write(
            """UPDATE sources SET stream_url=?, stream_expires=?, status='active', fail_streak=0, last_ok_at=?,
                      last_error='', updated_at=? WHERE id=?""",
            (url, expires, t, t, source_id),
        )

    async def source_failed(self, source_id: int, error: str) -> None:
        await self._write(
            "UPDATE sources SET fail_streak=fail_streak+1, last_fail_at=?, last_error=? WHERE id=?",
            (now(), error[:500], source_id),
        )

    async def update_source(self, source_id: int, **fields) -> None:
        await self._update("sources", source_id, fields)

    async def mark_source_gone(self, site: str, key: str) -> int | None:
        """源已下架；作品没有其他可用源时也标为下架。返回作品 id。"""
        async with self._tx():
            async with self.conn.execute("SELECT id, video_id FROM sources WHERE site=? AND key=?", (site, key)) as cur:
                src = await cur.fetchone()
            if src is None:
                return None
            t = now()
            await self.conn.execute("UPDATE sources SET status='gone', updated_at=? WHERE id=?", (t, src["id"]))
            async with self.conn.execute(
                "SELECT 1 FROM sources WHERE video_id=? AND status='active' LIMIT 1", (src["video_id"],)
            ) as cur:
                if await cur.fetchone() is None:
                    await self.conn.execute("UPDATE videos SET status='gone', updated_at=? WHERE id=?",
                                            (t, src["video_id"]))
            return src["video_id"]

    async def cached_stream_url(self, video_id: int) -> str:
        """作品任意一个缓存了播放地址的源（直写 CDN 地址的调试模式用）。"""
        row = await self._one(
            "SELECT stream_url FROM sources WHERE video_id=? AND status='active' AND stream_url!='' "
            "ORDER BY stream_expires IS NULL, stream_expires DESC LIMIT 1",
            (video_id,),
        )
        return row["stream_url"] if row else ""

    async def sources_missing_detail(self, rank: Callable[[str], int],
                                     library_ids: list[int] | None = None) -> list[tuple[str, str]]:
        """没有详情的作品（给了 library_ids 就只要在这些库里的），各取优先级最高的可用源，返回 [(站点, key)]。"""
        sql = """SELECT s.video_id, s.site, s.key FROM sources s JOIN videos v ON v.id=s.video_id
                 WHERE v.detail_at IS NULL AND v.status='active' AND s.status='active'"""
        if library_ids:
            sql += (" AND EXISTS (SELECT 1 FROM outputs o WHERE o.video_id=v.id AND o.library_id IN (%s))"
                    % ",".join("?" * len(library_ids)))
        rows = await self._all(sql + " ORDER BY s.video_id DESC", library_ids or (), bulk=True)
        best: dict[int, tuple[str, str]] = {}
        for r in rows:
            cur = best.get(r["video_id"])
            if cur is None or rank(r["site"]) < rank(cur[0]):
                best[r["video_id"]] = (r["site"], r["key"])
        return list(best.values())

    async def load_health(self) -> list[dict]:
        return [json.loads(r["data"]) for r in await self._all("SELECT data FROM stream_health")]

    async def save_health(self, rows: list[dict]) -> None:
        t = now()
        await self._write_many(
            "INSERT INTO stream_health(key, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            [(r["key"], json.dumps(r, ensure_ascii=False), t) for r in rows],
        )

    async def health_samples(self, site: str, line: str | None, limit: int) -> list[dict]:
        """检测连通性用的样本：这个站（这条线路）最近播放成功过的可用源，没播过的按新到旧补上。
        指定了线路时每行的 line_row 是那条线路。"""
        if line is None:
            rows = await self._all(
                """SELECT s.* FROM sources s JOIN videos v ON v.id=s.video_id
                   WHERE s.site=? AND s.status='active' AND v.status='active'
                   ORDER BY s.last_ok_at IS NULL, s.last_ok_at DESC, s.id DESC LIMIT ?""", (site, limit))
            return [dict(r) for r in rows]
        rows = await self._all(
            """SELECT s.id AS sid, l.id AS lid FROM source_lines l JOIN sources s ON s.id=l.source_id
                 JOIN videos v ON v.id=s.video_id
               WHERE s.site=? AND l.line=? AND s.status='active' AND v.status='active'
               ORDER BY l.last_ok_at IS NULL, l.last_ok_at DESC, l.id DESC LIMIT ?""", (site, line, limit))
        out = []
        for r in rows:
            src = await self.get_source(r["sid"])
            ln = next(x for x in await self.get_lines(r["sid"]) if x["id"] == r["lid"])
            out.append({**src, "line_row": ln})
        return out

    async def sources_needing_quality(self, tried_before: int, library_id: int | None = None) -> list[dict]:
        """还不知道画质的可用源（源本身或它的某条线路只有站点标注、或者什么都没有），tried_before 之后试过的跳过。"""
        sql = """SELECT s.id, s.site FROM sources s JOIN videos v ON v.id=s.video_id
                 WHERE s.status='active' AND v.status='active'
                   AND ((s.quality_src IN ('', 'claimed') AND (s.quality_at IS NULL OR s.quality_at<?))
                        OR EXISTS (SELECT 1 FROM source_lines l WHERE l.source_id=s.id
                                     AND l.quality_src IN ('', 'claimed') AND (l.quality_at IS NULL OR l.quality_at<?)))"""
        params: list = [tried_before, tried_before]
        if library_id:
            sql += " AND EXISTS (SELECT 1 FROM outputs o WHERE o.video_id=v.id AND o.library_id=?)"
            params.append(library_id)
        return [dict(r) for r in await self._all(sql + " ORDER BY s.video_id DESC", params, bulk=True)]

    async def set_source_check(self, video_id: int, site: str, found: bool) -> None:
        await self._write(
            "INSERT INTO source_checks(video_id, site, found, checked_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(video_id, site) DO UPDATE SET found=excluded.found, checked_at=excluded.checked_at",
            (video_id, site, int(found), now()),
        )

    async def get_source_check(self, video_id: int, site: str) -> dict | None:
        row = await self._one("SELECT * FROM source_checks WHERE video_id=? AND site=?", (video_id, site))
        return dict(row) if row else None

    async def works_to_probe(self, site: str, checked_before: int, library_id: int | None = None) -> list[int]:
        """在这个站还没有源、且 checked_before 之后没查过的作品（下架的也算：别的站可能还有）。"""
        sql = """SELECT v.id FROM videos v
                 WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.video_id=v.id AND s.site=?)
                   AND NOT EXISTS (SELECT 1 FROM source_checks c WHERE c.video_id=v.id AND c.site=? AND c.checked_at>=?)"""
        params: list = [site, site, checked_before]
        if library_id:
            sql += " AND EXISTS (SELECT 1 FROM outputs o WHERE o.video_id=v.id AND o.library_id=?)"
            params.append(library_id)
        return [r["id"] for r in await self._all(sql + " ORDER BY v.id DESC", params, bulk=True)]

    async def cdn_video_map(self, site: str = "jable") -> dict[int, str]:
        """站内数字 id -> 作品 slug（strm 扫描时把 CDN 直链认回作品）。"""
        rows = await self._all(
            "SELECT s.site_vid, v.slug FROM sources s JOIN videos v ON v.id=s.video_id WHERE s.site=? AND s.site_vid!=''",
            (site,),
        )
        return {int(r["site_vid"]): r["slug"] for r in rows if r["site_vid"].isdigit()}

    async def get_video(self, slug: str) -> dict | None:
        return _video_row(await self._one("SELECT * FROM videos WHERE slug=?", (slug.lower(),)))

    async def set_duration(self, video_id: int, duration: int) -> None:
        await self._write("UPDATE videos SET duration=? WHERE id=?", (duration, video_id))

    async def search_videos(self, query: VideoQuery, offset: int = 0, limit: int = 50):
        cond, params = query.where()
        total = (await self._one(f"SELECT COUNT(*) AS n FROM videos WHERE {cond}", params, bulk=True))["n"]
        rows = await self._all(
            f"SELECT * FROM videos WHERE {cond} ORDER BY {query.order()} LIMIT ? OFFSET ?", params + [limit, offset], bulk=True
        )
        return [_video_row(r) for r in rows], total

    async def iter_videos(self, batch: int = 500, *, after: int = -1):
        last = after
        while True:
            rows = await self._all(
                "SELECT * FROM videos WHERE status='active' AND id>? ORDER BY id LIMIT ?", (last, batch)
            )
            if not rows:
                return
            for r in rows:
                yield _video_row(r)
            last = rows[-1]["id"]

    async def facets(self, limit: int = 300) -> dict:
        """库里已有的分类、标签、女优、发行商、画质及影片数，给规则编辑、影片库筛选做候选。"""
        return {f: await self.facet(f, limit=limit) for f in (*FACET_FIELDS, "makers", "quality")}

    async def facet(self, name: str, q: str = "", limit: int = 300) -> list[dict]:
        return await self.cache.get(("facet", name, q, limit), lambda: self._facet(name, q, limit), 30)

    async def _facet(self, name: str, q: str = "", limit: int = 300) -> list[dict]:
        """一种候选：[{item, name, n}]，按影片数排；q 按名称或 id 筛。"""
        like = f"%{q.strip()}%"
        if name in FACET_FIELDS:
            # 在覆盖索引内汇总，不再逐条随机读取影片 JSON 和状态。
            rows = await self._all(
                """SELECT j.item AS item, MAX(j.name) AS name,
                           COUNT(*) AS n
                    FROM video_facets j WHERE j.active=1 AND j.kind=?
                    GROUP BY item HAVING ?='%%' OR item LIKE ? OR name LIKE ? ORDER BY n DESC LIMIT ?""",
                (name, like, like, like, limit), bulk=True,
            )
        elif name in ("makers", "quality"):
            col = "maker" if name == "makers" else "quality"
            rows = await self._all(
                f"""SELECT {col} AS item, {col} AS name, COUNT(*) AS n FROM videos
                    WHERE status='active' AND {col}!='' AND {col} LIKE ? GROUP BY {col} ORDER BY n DESC LIMIT ?""",
                (like, limit), bulk=True,
            )
        else:
            raise ValueError(f"没有这种候选：{name}")
        return [dict(r) for r in rows]

    async def video_stats(self) -> dict:
        return await self.cache.get("video_stats", self._video_stats, 3)

    async def _video_stats(self) -> dict:
        row = await self._one(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(detail_at IS NOT NULL), 0) AS with_detail,
                      COALESCE(SUM(status='gone'), 0) AS gone,
                      (SELECT COUNT(DISTINCT video_id) FROM outputs WHERE strm_path != '') AS with_strm,
                      (SELECT COUNT(DISTINCT video_id) FROM outputs WHERE cover_done) AS with_cover
               FROM videos""", bulk=True
        )
        return dict(row)

    # ---- 输出库 ----

    async def list_libraries(self) -> list[dict]:
        rows = await self._all(
            """SELECT l.*, (SELECT COUNT(*) FROM outputs o WHERE o.library_id=l.id) AS videos,
                      (SELECT COUNT(*) FROM outputs o WHERE o.library_id=l.id AND o.strm_path='') AS pending,
                      (SELECT COUNT(*) FROM subscriptions s WHERE s.library_id=l.id) AS subscriptions
               FROM libraries l ORDER BY l.id"""
        )
        return [_library_row(r) for r in rows]

    async def get_library(self, library_id: int) -> dict | None:
        return _library_row(await self._one("SELECT * FROM libraries WHERE id=?", (library_id,)))

    async def create_library(self, name: str, dir: str, path_template: str = "", rule: dict | None = None,
                             external_dir: str = "", sources: list[int] = (), excludes: list[int] = (),
                             versions: str = "") -> int:
        cur = await self._write(
            "INSERT INTO libraries(name, dir, path_template, rule, external_dir, sources, excludes, versions, "
            "created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, dir, path_template, json.dumps(rule, ensure_ascii=False) if rule else "", external_dir,
             json.dumps(list(sources)), json.dumps(list(excludes)), versions, now()),
        )
        return cur.lastrowid

    async def update_library(self, library_id: int, **fields) -> None:
        if "rule" in fields:
            fields["rule"] = json.dumps(fields["rule"], ensure_ascii=False) if fields["rule"] else ""
        for k in LIBRARY_LINKS:
            if k in fields:
                fields[k] = json.dumps(list(fields[k]))
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

    async def ensure_output(self, video_id: int, library_id: int, via: str = "job", *, crawl_job: int | None = None) -> bool:
        """影片加入输出库，返回是否新加入。"""
        sql = "INSERT OR IGNORE INTO outputs(video_id, library_id, via) VALUES(?, ?, ?)"
        if crawl_job is not None:
            async with self._tx() as conn:
                cur = await conn.execute(sql, (video_id, library_id, via))
                added = cur.rowcount > 0
                await conn.execute(
                    "UPDATE crawl_items SET added=MAX(added,?),excluded=0 WHERE job_id=? AND video_id=?",
                    (int(added), crawl_job, video_id),
                )
            return added
        cur = await self._write(sql, (video_id, library_id, via))
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

    async def set_cover_done(self, video_id: int, library_id: int, done: bool) -> None:
        await self._write("UPDATE outputs SET cover_done=? WHERE video_id=? AND library_id=?",
                          (int(done), video_id, library_id))

    async def set_output_paths(self, library_id: int, paths: list[tuple[str, int]]) -> int:
        """批量改记录的 strm 路径（外部工具移走文件后找回的新位置），paths 是 (路径, 影片 id)。"""
        return await self._write_many(
            "UPDATE outputs SET strm_path=? WHERE video_id=? AND library_id=?",
            [(path, video_id, library_id) for path, video_id in paths],
        )

    async def delete_output(self, video_id: int, library_id: int) -> None:
        await self._write("DELETE FROM outputs WHERE video_id=? AND library_id=?", (video_id, library_id))

    async def iter_outputs(self, library_id: int | None = None, batch: int = 500, *, after: int = -1):
        """逐批产出 (影片, 输出)。"""
        last = after
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
                out["rowid"] = v.pop("output_rowid")
                yield v, out
            last = rows[-1]["output_rowid"]

    # ---- 来源库 / 排除库 ----

    async def in_libraries(self, video_id: int, library_ids: list[int]) -> bool:
        if not library_ids:
            return False
        marks = ",".join("?" * len(library_ids))
        row = await self._one(f"SELECT 1 FROM outputs WHERE video_id=? AND library_id IN ({marks}) LIMIT 1",
                              (video_id, *library_ids))
        return row is not None

    async def outputs_to_drop(self, library_id: int, sources: list[int], excludes: list[int]) -> list[dict]:
        """该库里要移除的输出：影片已在排除库里；或者是从来源库归入的，但影片已不在任何来源库。"""
        conds, params = [], [library_id]
        if excludes:
            conds.append(f"""EXISTS (SELECT 1 FROM outputs x WHERE x.video_id=o.video_id
                                       AND x.library_id IN ({",".join("?" * len(excludes))}))""")
            params += excludes
        source_cond = (f"""NOT EXISTS (SELECT 1 FROM outputs s WHERE s.video_id=o.video_id AND s.strm_path!=''
                                         AND s.library_id IN ({",".join("?" * len(sources))}))""" if sources else "1")
        conds.append(f"(o.via='source' AND {source_cond})")
        params += sources
        rows = await self._all(
            f"""SELECT o.video_id, o.strm_path, v.slug FROM outputs o JOIN videos v ON v.id=o.video_id
                WHERE o.library_id=? AND ({' OR '.join(conds)})""", params
        )
        return [dict(r) for r in rows]

    async def pull_from_sources(self, library_id: int, sources: list[int], excludes: list[int]) -> int:
        """来源库里已写出、且不在排除库里的影片加入该库（via=source，先不写文件），返回新加入数。"""
        if not sources:
            return 0
        sql = f"""INSERT OR IGNORE INTO outputs(video_id, library_id, via)
                  SELECT DISTINCT s.video_id, ?, 'source' FROM outputs s JOIN videos v ON v.id=s.video_id
                  WHERE s.library_id IN ({",".join("?" * len(sources))}) AND s.strm_path!='' AND v.status='active'"""
        params = [library_id, *sources]
        if excludes:
            sql += f""" AND NOT EXISTS (SELECT 1 FROM outputs x WHERE x.video_id=s.video_id
                                          AND x.library_id IN ({",".join("?" * len(excludes))}))"""
            params += excludes
        return (await self._write(sql, params)).rowcount

    async def pending_outputs(self, library_id: int, seen_before: int) -> list[dict]:
        """该库里还没写文件、且首次出现早于 seen_before 的影片。"""
        rows = await self._all(
            """SELECT v.* FROM outputs o JOIN videos v ON v.id=o.video_id
               WHERE o.library_id=? AND o.strm_path='' AND v.status='active' AND v.created_at<? ORDER BY v.id""",
            (library_id, seen_before),
        )
        return [_video_row(r) for r in rows]

    async def libraries_with_source_outputs(self) -> set[int]:
        rows = await self._all("SELECT DISTINCT library_id FROM outputs WHERE via='source'")
        return {r["library_id"] for r in rows}

    async def subscription_last_done(self) -> dict[int, int]:
        """每个订阅最近一次跑完的任务的开始时间。"""
        rows = await self._all(
            """SELECT json_extract(params, '$.subscription_id') AS sub_id, MAX(COALESCE(started_at,created_at)) AS t FROM jobs
               WHERE json_extract(state, '$.list_complete')=1
                 AND json_extract(params, '$.subscription_id') IS NOT NULL GROUP BY sub_id"""
        )
        return {r["sub_id"]: r["t"] for r in rows}

    async def all_output_paths(self) -> list[str]:
        rows = await self._all("SELECT strm_path FROM outputs WHERE strm_path != ''")
        return [r["strm_path"] for r in rows]

    async def video_keys(self) -> list[tuple[int, str]]:
        rows = await self._all("SELECT id, slug FROM videos")
        return [(r["id"], r["slug"]) for r in rows]

    # ---- strm 扫描 ----

    STRM_COLUMNS = ("path", "scan_id", "url", "prefix", "kind", "slug", "video_id", "expired", "managed",
                    "library_id", "mtime", "note", "scanned_at")

    async def replace_strm_files(self, dir_prefix: str, rows: list[tuple]) -> None:
        """用一次扫描的结果替换该目录下的旧记录。"""
        cols = ", ".join(self.STRM_COLUMNS)
        marks = ", ".join("?" * len(self.STRM_COLUMNS))
        async with self._lock:
            await self.conn.execute("BEGIN")
            try:
                await self.conn.execute("DELETE FROM strm_files WHERE path >= ? AND path < ?",
                                        (dir_prefix, dir_prefix + "\U0010ffff"))
                await self.conn.executemany(f"INSERT OR REPLACE INTO strm_files({cols}) VALUES({marks})", rows)
                await self.conn.execute("COMMIT")
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise

    async def strm_summary(self, scan_id: int) -> dict:
        kinds = await self._all(
            "SELECT kind, managed, COUNT(*) AS n FROM strm_files WHERE scan_id=? GROUP BY kind, managed", (scan_id,)
        )
        prefixes = await self._all(
            """SELECT prefix, COUNT(*) AS n, SUM(managed) AS managed, MIN(path) AS sample_path, MIN(url) AS sample_url
               FROM strm_files WHERE scan_id=? GROUP BY prefix ORDER BY n DESC LIMIT 100""",
            (scan_id,),
        )
        adoptable = await self._one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(video_id IS NULL), 0) AS unknown FROM strm_files
               WHERE scan_id=? AND managed=0 AND kind IN ('ours', 'cdn', 'named') AND slug != ''""",
            (scan_id,),
        )
        by_kind: dict[str, dict] = {}
        for r in kinds:
            k = by_kind.setdefault(r["kind"], {"total": 0, "managed": 0})
            k["total"] += r["n"]
            if r["managed"]:
                k["managed"] += r["n"]
        return {"kinds": by_kind, "prefixes": [dict(r) for r in prefixes],
                "adoptable": adoptable["n"], "adoptable_unknown": adoptable["unknown"]}

    async def list_strm_files(self, scan_id: int, kind: str = "", managed: str = "", prefix: str = "",
                              q: str = "", offset: int = 0, limit: int = 50):
        where, params = ["scan_id=?"], [scan_id]
        if kind:
            where.append("kind=?")
            params.append(kind)
        if managed in ("0", "1"):
            where.append("managed=?")
            params.append(int(managed))
        if prefix:
            where.append("prefix=?")
            params.append(prefix)
        if q:
            where.append("(path LIKE ? OR url LIKE ? OR slug LIKE ? OR note LIKE ?)")
            params += [f"%{q}%"] * 4
        cond = " AND ".join(where)
        total = (await self._one(f"SELECT COUNT(*) AS n FROM strm_files WHERE {cond}", params))["n"]
        rows = await self._all(f"SELECT * FROM strm_files WHERE {cond} ORDER BY path LIMIT ? OFFSET ?",
                               params + [limit, offset])
        return [dict(r) for r in rows], total

    async def adopt_candidates(self, scan_id: int, kinds: tuple[str, ...], prefix: str = "") -> list[str]:
        marks = ",".join("?" * len(kinds))
        sql = f"SELECT path FROM strm_files WHERE scan_id=? AND managed=0 AND slug != '' AND kind IN ({marks})"
        params: list = [scan_id, *kinds]
        if prefix:
            sql += " AND prefix=?"
            params.append(prefix)
        rows = await self._all(sql + " ORDER BY path", params)
        return [r["path"] for r in rows]

    async def get_strm_file(self, path: str) -> dict | None:
        row = await self._one("SELECT * FROM strm_files WHERE path=?", (path,))
        return dict(row) if row else None

    async def update_strm_file(self, path: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._write(f"UPDATE strm_files SET {cols} WHERE path=?", (*fields.values(), path))

    async def strm_prefix_matches(self, scan_id: int, prefix: str, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM strm_files WHERE scan_id=? AND substr(url, 1, ?)=? ORDER BY path"
        params: list = [scan_id, len(prefix), prefix]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in await self._all(sql, params)]

    async def count_strm_prefix(self, scan_id: int, prefix: str) -> dict:
        row = await self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(managed), 0) AS managed FROM strm_files "
            "WHERE scan_id=? AND substr(url, 1, ?)=?",
            (scan_id, len(prefix), prefix),
        )
        return dict(row)

    async def missing_outputs(self, dir_prefix: str, offset: int = 0, limit: int = 50):
        """库里有记录、但扫描时在该目录下没找到文件的输出。"""
        cond = """o.strm_path >= ? AND o.strm_path < ?
                  AND NOT EXISTS (SELECT 1 FROM strm_files f WHERE f.path=o.strm_path)"""
        params = [dir_prefix, dir_prefix + "\U0010ffff"]
        total = (await self._one(f"SELECT COUNT(*) AS n FROM outputs o WHERE {cond}", params))["n"]
        rows = await self._all(
            f"""SELECT o.video_id, o.library_id, o.strm_path, v.slug, l.name AS library_name
                FROM outputs o JOIN videos v ON v.id=o.video_id JOIN libraries l ON l.id=o.library_id
                WHERE {cond} ORDER BY o.strm_path LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        return [dict(r) for r in rows], total

    async def add_strm_change(self, change_set: int, path: str, old: str, new: str) -> None:
        await self._write(
            "INSERT INTO strm_changes(change_set, path, old, new, created_at) VALUES(?, ?, ?, ?, ?)",
            (change_set, path, old, new, now()),
        )

    async def strm_changes(self, change_set: int, only_active: bool = True) -> list[dict]:
        cond = "AND reverted=0" if only_active else ""
        rows = await self._all(f"SELECT * FROM strm_changes WHERE change_set=? {cond} ORDER BY id", (change_set,))
        return [dict(r) for r in rows]

    async def mark_change_reverted(self, change_id: int) -> None:
        await self._write("UPDATE strm_changes SET reverted=1 WHERE id=?", (change_id,))

    async def list_change_sets(self) -> list[dict]:
        rows = await self._all(
            """SELECT c.change_set, COUNT(*) AS files, SUM(c.reverted) AS reverted, MIN(c.created_at) AS created_at,
                      j.params, j.status
               FROM strm_changes c LEFT JOIN jobs j ON j.id=c.change_set
               GROUP BY c.change_set ORDER BY c.change_set DESC LIMIT 100"""
        )
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"] or "{}")
            out.append(d)
        return out

    # ---- 订阅 ----

    async def list_subscriptions(self) -> list[dict]:
        rows = await self._all(
            """SELECT s.*, l.name AS library_name,
                      (SELECT j.id FROM jobs j WHERE j.status IN ('running', 'paused')
                         AND json_extract(j.params, '$.subscription_id')=s.id ORDER BY j.id DESC LIMIT 1) AS active_job_id,
                      (SELECT j.id FROM jobs j WHERE j.status IN ('running', 'paused')
                         AND COALESCE(json_extract(j.state,'$.list_complete'),0)=0
                         AND json_extract(j.params,'$.subscription_id')=s.id ORDER BY j.id DESC LIMIT 1) AS listing_job_id
               FROM subscriptions s LEFT JOIN libraries l ON l.id=s.library_id ORDER BY s.id"""
        )
        current = now()
        return [{**dict(r), "next_run_at": next_run_at(dict(r), current,
                 self.subscription_schedule_started_at)} for r in rows]

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
        schedule_fields = {"cron", "timezone", "enabled", "initialized"} & fields.keys()
        if schedule_fields:
            old = await self.get_subscription(sub_id)
            if old and any(old[k] != fields[k] for k in schedule_fields):
                fields["schedule_updated_at"] = now()
        await self._update("subscriptions", sub_id, fields)

    async def delete_subscription(self, sub_id: int) -> None:
        await self._write("DELETE FROM subscriptions WHERE id=?", (sub_id,))

    async def subscription_active_job(self, sub_id: int, *, listing_only: bool = False) -> dict | None:
        cond = "AND COALESCE(json_extract(state,'$.list_complete'),0)=0 " if listing_only else ""
        return _job_row(await self._one(
            "SELECT * FROM jobs WHERE status IN ('running', 'paused') "
            + cond + "AND json_extract(params, '$.subscription_id')=? ORDER BY id DESC LIMIT 1",
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

    async def mark_job_started(self, job_id: int) -> bool:
        """记下任务开始执行的时间；已经记过返回 False。"""
        cur = await self._write("UPDATE jobs SET started_at=? WHERE id=? AND started_at IS NULL", (now(), job_id))
        return cur.rowcount > 0

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
        async with self._tx(invalidate=False):
            await self.conn.execute("DELETE FROM job_logs WHERE job_id=?", (job_id,))
            await self.conn.execute("DELETE FROM crawl_items WHERE job_id=?", (job_id,))
            await self.conn.execute("DELETE FROM scan_staging WHERE scan_id=?", (job_id,))
            await self.conn.execute("DELETE FROM tasks WHERE job_id=?", (job_id,))
            await self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    # ---- task ----

    @property
    def history(self) -> JobHistory:
        return JobHistory(self)

    async def add_tasks(self, job_id: int, kind: str, targets: Iterable[str], priority: int = 0,
                        site: str = "") -> int:
        t = now()
        rows = [(job_id, kind, str(x), priority, site, t) for x in targets]
        if not rows:
            return 0
        return await self._write_many(
            "INSERT OR IGNORE INTO tasks(job_id, kind, target, priority, site, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            rows,
        )

    async def claim_task(self, skip_sites: Iterable[str] = ()) -> dict | None:
        """领一个可执行的子任务；skip_sites 里的站点（被拦截、并发已满、未启用）的任务先不领。"""
        t = now()
        skip = list(skip_sites)
        cond = f"AND t.site NOT IN ({','.join('?' * len(skip))})" if skip else ""
        async with self._lock:
            async with self.conn.execute(
                f"""
                UPDATE tasks SET status='running', attempts=attempts+1, updated_at=?
                WHERE id = (
                  SELECT t.id FROM tasks t JOIN jobs j ON j.id = t.job_id
                  WHERE t.status='pending' AND t.next_run_at<=? AND j.status='running' {cond}
                  ORDER BY t.priority DESC, t.id LIMIT 1)
                RETURNING *
                """,
                (t, t, *skip),
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
