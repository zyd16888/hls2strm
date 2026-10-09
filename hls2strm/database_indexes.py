"""查询索引与扫描暂存迁移；JSON 元数据仍是兼容的写入入口。"""

import aiosqlite

async def migrate_v12(conn: aiosqlite.Connection) -> None:
    """候选和关联筛选使用索引；原 JSON 元数据保持兼容。"""
    await conn.execute("CREATE TABLE video_facets (video_id INTEGER NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL, PRIMARY KEY(video_id, kind, item))")
    await conn.execute("CREATE INDEX video_facets_lookup ON video_facets(kind, item, video_id)")
    await conn.execute("CREATE INDEX video_facets_summary ON video_facets(kind,item,name) WHERE active=1")
    for col, key in (("models", "id"), ("categories", "slug"), ("tags", "slug")):
        await conn.execute(f"INSERT OR IGNORE INTO video_facets SELECT v.id, '{col}', CAST(json_extract(j.value, '$.{key}') AS TEXT), COALESCE(json_extract(j.value, '$.name'), ''), v.status='active' FROM videos v, json_each(v.{col}) j WHERE json_extract(j.value, '$.{key}') IS NOT NULL")
        insert = f"INSERT OR IGNORE INTO video_facets SELECT NEW.id, '{col}', CAST(json_extract(j.value, '$.{key}') AS TEXT), COALESCE(json_extract(j.value, '$.name'), ''), NEW.status='active' FROM json_each(NEW.{col}) j WHERE json_extract(j.value, '$.{key}') IS NOT NULL;"
        await conn.execute(f"CREATE TRIGGER videos_{col}_insert AFTER INSERT ON videos BEGIN {insert} END")
        await conn.execute(f"CREATE TRIGGER videos_{col}_update AFTER UPDATE OF {col} ON videos WHEN OLD.{col}!=NEW.{col} BEGIN DELETE FROM video_facets WHERE video_id=NEW.id AND kind='{col}'; {insert} END")
    await conn.execute("CREATE TRIGGER videos_facets_delete AFTER DELETE ON videos BEGIN DELETE FROM video_facets WHERE video_id=OLD.id; END")
    await conn.execute("CREATE TRIGGER videos_facets_status AFTER UPDATE OF status ON videos WHEN OLD.status!=NEW.status BEGIN UPDATE video_facets SET active=NEW.status='active' WHERE video_id=NEW.id; END")
    for direction in ("ASC", "DESC"):
        await conn.execute(f"CREATE INDEX videos_release_{direction.lower()} ON videos((release_date IS NULL OR release_date='') ASC, release_date {direction}, id {direction})")
    await conn.execute("CREATE INDEX videos_created ON videos(created_at, id)")
    await conn.execute("CREATE INDEX tasks_recent_failures ON tasks(status, updated_at DESC)")
    await conn.execute("CREATE INDEX tasks_job_kind ON tasks(job_id,kind,status)")
    await conn.execute("CREATE TABLE scan_staging AS SELECT * FROM strm_files WHERE 0")
    await conn.execute("CREATE UNIQUE INDEX scan_staging_key ON scan_staging(scan_id,path)")
    await conn.execute("CREATE TABLE play_sessions (id TEXT PRIMARY KEY, source_id INTEGER NOT NULL, data TEXT NOT NULL, expires_at INTEGER NOT NULL)")
    await conn.execute("CREATE INDEX play_sessions_expiry ON play_sessions(expires_at)")
    await conn.execute("""UPDATE jobs SET state=json_set(state, '$.list_complete',
        CASE WHEN EXISTS(SELECT 1 FROM tasks t WHERE t.job_id=jobs.id AND t.kind='list')
              AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.job_id=jobs.id AND t.kind='list' AND t.status!='done')
             THEN json('true') ELSE json('false') END)
        WHERE status IN ('done','running','paused') AND kind IN ('crawl','incremental')""")
    await conn.execute("""UPDATE subscriptions SET initialized=0 WHERE EXISTS(
        SELECT 1 FROM jobs j WHERE j.kind='crawl' AND j.status='done'
        AND json_extract(j.params,'$.subscription_id')=subscriptions.id
        AND json_extract(j.state,'$.list_complete')=0
        AND j.id=(SELECT MAX(k.id) FROM jobs k WHERE k.kind='crawl' AND k.status='done'
                  AND json_extract(k.params,'$.subscription_id')=subscriptions.id))""")
