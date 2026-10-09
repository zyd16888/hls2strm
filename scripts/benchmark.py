"""同一合成数据库在迁移前后的查询对比；不连接真实站点或业务库。"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

from hls2strm.db import Database, VideoQuery


async def measure(fn):
    values = []
    for _ in range(3):
        started = time.perf_counter()
        await fn()
        values.append((time.perf_counter()-started)*1000)
    return round(statistics.median(values), 2)


async def checks(db, query_type):
    out = {}
    for name, query in [("default", query_type()), ("release", query_type(sort="release")),
                        ("search", query_type(q="123")), ("actor", query_type(models=["42"]))]:
        out[name] = await measure(lambda query=query: db.search_videos(query))
    async def cold():
        if hasattr(db, "cache"):
            db.cache.clear()
        return await db.facets()
    out["facets_cold"] = await measure(cold)
    out["facets_warm"] = await measure(db.facets)
    if hasattr(db, "cache"):
        db.cache.clear()
    busy = asyncio.create_task(db.facets())
    await asyncio.sleep(0)
    started = time.perf_counter()
    await db.get_video("abc-000001")
    out["contended_slug_read"] = round((time.perf_counter()-started)*1000, 2)
    await busy
    return out


async def main(ref, rows):
    baseline = types.ModuleType("hls2strm._baseline_db")
    sys.modules[baseline.__name__] = baseline
    source = subprocess.check_output(["git", "show", f"{ref}:hls2strm/db.py"], encoding="utf-8")
    exec(compile(source, "baseline_db.py", "exec"), baseline.__dict__)
    with tempfile.TemporaryDirectory(prefix="hls2strm-benchmark-") as temp:
        path = Path(temp) / "synthetic.db"
        old = baseline.Database(path)
        await old.open()
        try:
            for start in range(1, rows+1, 2000):
                await old.conn.execute("BEGIN")
                await old.conn.executemany("INSERT INTO videos(id,slug,code,code_key,title,models,categories,tags,release_date,duration,detail_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                    (i, f"abc-{i:06}", f"ABC-{i:06}", f"ABC{i}", f"Title {i}",
                     json.dumps([{"id": str(i%1000), "name": f"Actor {i%1000}"}]),
                     json.dumps([{"slug": f"cat-{i%20}", "name": f"Category {i%20}"}]),
                     json.dumps([{"slug": f"tag-{i%100}", "name": f"Tag {i%100}"}]),
                     f"2026-{i%12+1:02}-{i%28+1:02}", 3600+i%7200, 1, 1, 1)
                    for i in range(start, min(start+2000, rows+1))])
                await old.conn.executemany("INSERT INTO sources(video_id,site,key,created_at,updated_at) VALUES(?,?,?,?,?)",
                                           [(i,"jable",f"abc-{i:06}",1,1) for i in range(start,min(start+2000,rows+1))])
                await old.conn.execute("COMMIT")
            before = await checks(old, baseline.VideoQuery)
        finally:
            await old.close()
        new = Database(path)
        started = time.perf_counter()
        await new.open()
        migration_ms = round((time.perf_counter()-started)*1000, 2)
        try:
            after = await checks(new, VideoQuery)
            q = VideoQuery(sort="release")
            cond, params = q.where()
            plan = [r["detail"] for r in await new._all(f"EXPLAIN QUERY PLAN SELECT * FROM videos WHERE {cond} ORDER BY {q.order()} LIMIT 50", params)]
            return {"rows": rows, "baseline_ref": ref, "before_ms": before, "after_ms": after,
                    "migration_ms": migration_ms, "release_plan": plan}
        finally:
            await new.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default="f13a344")
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(main(args.baseline_ref, args.rows))
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
