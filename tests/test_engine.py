import asyncio
import logging
import time

import pytest

from hls2strm import engine as engine_mod
from hls2strm.engine import Engine
from hls2strm.fetcher import Blocked, FetchError
from hls2strm.observability import Metrics
from hls2strm.parser import parse_list
from hls2strm.play import Resolver
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher, fixture


async def wait_job(db, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = await db.get_job(job_id)
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束：{await db.task_counts(job_id)}")


def build(db, store, boot, fetcher):
    metrics = Metrics()
    engine = Engine(db, fetcher, OutputWriter(store), store, metrics, boot)
    return engine


def model_fixture():
    html = fixture("list_model_full.html")
    ids = {it.slug: it.video_id for it in parse_list(html).items}
    return html, ids


def test_list_job_writes_strm_nfo_and_covers(make_store, boot, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="hls2strm")
    monkeypatch.setattr(engine_mod, "PROGRESS_INTERVAL", 0)

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = build(db, store, boot, fetcher)
        await engine.start()
        job_id = await engine.create_crawl("https://jable.tv/models/abc/", end_page=1)
        job = await wait_job(db, job_id)
        await engine.stop()

        counts = await db.task_counts(job_id)
        assert job["status"] == "done" and counts == {"done": 25}
        assert job["state"]["last_page"] == 2
        stats = await db.video_stats()
        assert stats["total"] == 24 and stats["with_detail"] == 24 and stats["with_strm"] == 24 and stats["with_cover"] == 24
        for slug in ids:
            d = store.output_dir / "全部" / slug.upper()
            assert (d / f"{slug.upper()}.strm").read_text().strip() == f"http://hls2strm:8080/play/{slug}.m3u8"
            assert (d / f"{slug.upper()}.nfo").exists()
            assert (d / f"{slug.upper()}-poster.jpg").exists()
        # 列表请求走异步块接口
        assert fetcher.calls[0].startswith("/models/abc/?mode=async&function=get_block")
        msgs = [r.getMessage() for r in caplog.records if r.name == "hls2strm.engine"]
        assert any(m.startswith(f"任务 #{job_id}「") and m.endswith("开始执行") for m in msgs)
        assert any(f"」进度 " in m and "完成 " in m for m in msgs)
        assert any(m.startswith(f"任务 #{job_id}「") and "完成，用时 " in m and m.endswith("：完成 25") for m in msgs)
        await db.close()

    asyncio.run(run())


def test_retry_gone_and_blocked(make_store, boot):
    async def run():
        db, store = await make_store()
        await store.update({"retry_base_delay": 1, "max_attempts": 2})
        html, ids = model_fixture()
        slugs = list(ids)
        fetcher = FakeFetcher(html, ids)
        fetcher.fail = {slugs[0]: FetchError("boom")}
        engine = build(db, store, boot, fetcher)
        await engine.start()

        job_id = await engine.create_videos([("jable", x) for x in (slugs[0], slugs[1], "not-exist-1")])
        job = await wait_job(db, job_id)
        counts = await db.task_counts(job_id)
        assert job["status"] == "done"
        assert counts == {"failed": 1, "done": 1, "gone": 1}
        failed = (await db.list_tasks(job_id, "failed"))[0]
        assert failed["attempts"] == 2 and "boom" in failed["last_error"]

        # 被拦截：不计失败次数，引擎暂停到冷却结束
        fetcher.fail = {slugs[2]: Blocked("所有域名都被拦截", 30)}
        job_id = await engine.create_videos([("jable", slugs[2])])
        for _ in range(100):
            if engine.blocked.get("jable", 0) > time.time():
                break
            await asyncio.sleep(0.05)
        assert engine.blocked["jable"] > time.time() + 20 and "jable" in engine._skip_sites()
        task = (await db.list_tasks(job_id))[0]
        assert task["status"] == "pending" and task["attempts"] == 0

        # 重试失败项、解除拦截后能跑完
        fetcher.fail = {}
        engine.resume()
        await db.finish_task(task["id"], "pending", next_run_at=0)
        assert (await wait_job(db, job_id))["status"] == "done"
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_resume_after_crash(make_store, boot):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        job_id = await db.create_job("videos", "t", {})
        await db.add_tasks(job_id, "detail", list(ids)[:3], site="jable")
        claimed = await db.claim_task()  # 模拟崩溃前正在执行
        assert claimed["status"] == "running"

        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()  # 启动时把 running 放回队列
        job = await wait_job(db, job_id)
        await engine.stop()
        assert job["status"] == "done" and await db.task_counts(job_id) == {"done": 3}
        # 指定影片没有列表页时长：从 m3u8 补齐
        v = await db.get_video(list(ids)[0])
        assert v["duration"] == 5400
        await db.close()

    asyncio.run(run())


def test_resolver_cache_and_single_flight(make_store):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        slug = "ipzz-983"
        fetcher.ids[slug] = 62384
        r = Resolver(db, fetcher, store, Metrics())

        # 库里还没有：按番号到站点上找到并入库
        first = await r.resolve(slug, min_remaining=60)
        assert fetcher.calls == ["/videos/ipzz-983/"]
        src_id = first.source["id"]

        # 已有作品、地址过期：同一个源的并发请求只抓一次
        await db.update_source(src_id, stream_expires=0)
        vs = await asyncio.gather(*(r.resolve(slug, min_remaining=60) for _ in range(5)))
        assert len(fetcher.calls) == 2
        assert all(x.url == vs[0].url for x in vs)

        # 缓存仍新鲜：不再请求
        await r.resolve(slug, min_remaining=60)
        assert len(fetcher.calls) == 2
        # 要求的剩余时间超过缓存：重新请求
        await r.resolve(slug, min_remaining=10**10)
        assert len(fetcher.calls) == 3
        # 指定的失效地址与缓存一致：强制刷新；不一致：直接返回
        cur = await r.ensure_source(src_id)
        await r.ensure_source(src_id, stale=cur.url)
        assert len(fetcher.calls) == 4
        await r.ensure_source(src_id, stale="https://old")
        assert len(fetcher.calls) == 4
        await db.close()

    asyncio.run(run())


def test_libraries_and_subscription(make_store, boot):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        cover_calls = []
        orig = fetcher.get_bytes

        async def counting(url, **kw):
            if url.endswith(".jpg"):
                cover_calls.append(url)
            return await orig(url, **kw)

        fetcher.get_bytes = counting
        engine = build(db, store, boot, fetcher)
        await engine.start()

        # 目录校验：不能为空、不能嵌套、不能是输出根目录
        for bad in ("", "全部/子目录", str(store.output_dir)):
            with pytest.raises(ValueError):
                await engine.create_library("坏库", bad)
        lib_id = (await engine.create_library("中文字幕", "中文字幕"))["id"]

        # 订阅：首轮全量 → initialized
        sub_id = await db.create_subscription(name="女优", source="/models/abc/", sort="post_date",
                                              library_id=lib_id, max_pages=5)
        job = await wait_job(db, await engine.run_subscription(sub_id))
        assert job["kind"] == "crawl" and job["status"] == "done"
        assert (await db.get_subscription(sub_id))["initialized"] == 1
        assert len(cover_calls) == 24
        lib_dir = store.output_dir / "中文字幕"
        assert len(list(lib_dir.glob("*/*.strm"))) == 24

        # 同一批影片再进默认库：已有详情，直接带 nfo，封面硬链接，不再下载
        job = await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1))
        assert await db.task_counts(job["id"]) == {"done": 1}
        assert len(cover_calls) == 24
        slug = next(iter(ids))
        v = await db.get_video(slug)
        assert {o["library_name"] for o in await db.get_outputs(v["id"])} == {"全部", "中文字幕"}
        assert (store.output_dir / "全部" / slug.upper() / f"{slug.upper()}.nfo").exists()

        # 增量：都已在库里，连续 48 部后停止
        job = await wait_job(db, await engine.run_subscription(sub_id))
        assert job["kind"] == "incremental" and job["state"]["known_streak"] >= 48
        assert await db.task_counts(job["id"]) == {"done": 2}
        with pytest.raises(ValueError):  # 同一订阅不能并发
            await db.update_job(job["id"], status="running")
            await engine.run_subscription(sub_id)
        await db.update_job(job["id"], status="done")

        # 改库目录：自动重写，文件搬到新目录
        jobs = await engine.update_library(lib_id, "中文字幕", "zh/中文字幕")
        assert jobs["reclassify_job_id"] is None
        await wait_job(db, jobs["rewrite_job_id"])
        assert not lib_dir.exists()
        assert len(list((store.output_dir / "zh" / "中文字幕").glob("*/*-poster.jpg"))) == 24

        # 删除库：有订阅时不让删；删掉订阅后连文件一起删
        with pytest.raises(ValueError):
            await engine.delete_library(lib_id, True)
        await db.delete_subscription(sub_id)
        await engine.reload_libraries()
        await wait_job(db, await engine.delete_library(lib_id, True))
        assert lib_id not in engine.libs and not (store.output_dir / "zh").exists()
        assert {o["library_name"] for o in await db.get_outputs(v["id"])} == {"全部"}
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_migrate_v1_database(boot):
    """v1 的库（影片上直接记 strm_path，全局增量设置）升级到 v2。"""
    import json
    import sqlite3

    from hls2strm.db import MIGRATIONS, SCHEMA_V1, Database

    path = boot.data_dir / "old.db"
    conn = sqlite3.connect(path)
    for sql in SCHEMA_V1:
        conn.execute(sql)
    conn.execute("INSERT INTO videos(id, slug, code, strm_path, cover_done, output_at, created_at, updated_at) "
                 "VALUES(1, 'abc-1', 'ABC-1', '/old/ABC-1/ABC-1.strm', 1, 5, 1, 1)")
    conn.execute("INSERT INTO settings VALUES('settings', ?)",
                 (json.dumps({"incremental_interval": 30, "fetch_detail": False}),))
    conn.execute("INSERT INTO jobs(kind, name, params, status, created_at) "
                 "VALUES('crawl', '全站', '{\"full\": true}', 'done', 1)")
    conn.commit()
    conn.close()

    async def run():
        db = Database(path)
        await db.open()
        assert (await db._one("PRAGMA user_version"))[0] == len(MIGRATIONS)
        assert [l["name"] for l in await db.list_libraries()] == ["全部"]
        out = await db.get_output(1, 1)
        assert out["strm_path"] == "/old/ABC-1/ABC-1.strm" and out["cover_done"] == 1
        sub = (await db.list_subscriptions())[0]
        assert (sub["interval"], sub["detail"], sub["initialized"]) == (30, 0, 1)
        assert "strm_path" not in await db.get_video("abc-1")
        jobs = await db.list_jobs()
        assert jobs[0]["kind"] == "rewrite" and jobs[0]["tasks"] == {"pending": 1}
        await db.close()
        # 再次打开不重复迁移
        db = Database(path)
        await db.open()
        assert len(await db.list_subscriptions()) == 1
        await db.close()

    asyncio.run(run())
