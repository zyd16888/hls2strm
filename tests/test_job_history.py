import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.db import Database
from hls2strm.job_history import capture_task_logs
from hls2strm.sites import SourceItem

from .conftest import FakeFetcher
from .test_engine import build, model_fixture, wait_job


def test_crawl_counts_survive_retry_and_separate_library_membership(make_store, boot):
    async def run():
        db, store = await make_store()
        jid = await db.create_job("crawl", "first", {})
        rank = lambda _: 0
        existing = SourceItem(key="abc-001", code="ABC-001", title="已有影片")
        fresh = SourceItem(key="abc-002", code="ABC-002", title="新增影片")
        await db.upsert_item("jable", existing, "abc-001", rank)
        old_id, _ = await db.upsert_item("jable", existing, "abc-001", rank, crawl=(jid, 1, 1))
        new_id, _ = await db.upsert_item("jable", fresh, "abc-002", rank, crawl=(jid, 1, 1))
        await db.ensure_output(old_id, 1, crawl_job=jid)
        await db.history.output_result(jid, new_id, excluded=True)
        # 暂停、崩溃后重跑本页，或下一页重复遇见，保留第一次判定。
        await db.upsert_item("jable", fresh, "abc-002", rank, crawl=(jid, 2, 2))
        await db.ensure_output(old_id, 1, crawl_job=jid)
        counts = (await db.history.counts([jid]))[jid]
        assert (counts["seen"], counts["new"], counts["existing"], counts["added"], counts["excluded"]) == (2, 1, 1, 1, 1)
        assert (await db.history.items(jid, "new"))["items"][0]["slug"] == "abc-002"
        assert (await db.history.items(jid, "existing"))["items"][0]["slug"] == "abc-001"
        assert (await db.history.items(jid, "excluded"))["total"] == 1
        await db.close()
        reopened = Database(boot.data_dir / "test.db")
        await reopened.open()
        try:
            assert (await reopened.history.counts([jid]))[jid] == counts
            await reopened.delete_job(jid)
            assert not await reopened.history.counts([jid])
            assert await reopened.get_video("abc-002") is not None
        finally:
            await reopened.close()
    asyncio.run(run())


def test_parallel_logs_are_isolated_persisted_paginated_and_redacted(make_store, boot, caplog):
    caplog.set_level(logging.INFO, logger="hls2strm")
    async def run():
        db, _ = await make_store()
        jobs = [await db.create_job("videos", name, {}) for name in ("one", "two")]
        log = logging.getLogger("hls2strm.test")
        async def task(jid):
            async with capture_task_logs(db.history, {"job_id": jid, "id": jid * 10}):
                log.info("job %s start https://cdn.example/video?token=secret", jid)
                await asyncio.sleep(.01)
                log.warning("job %s end", jid)
        await asyncio.gather(*(task(jid) for jid in jobs))
        log.info("outside task")
        await db.close()
        db = Database(boot.data_dir / "test.db")
        await db.open()
        try:
            for jid in jobs:
                items = (await db.history.logs(jid))["items"]
                assert len(items) == 2
                assert all(f"job {jid}" in x["msg"] and "secret" not in x["msg"] for x in items)
                assert not (await db.history.logs(jid, task_id=999))["items"]
                page = await db.history.logs(jid, limit=1)
                earlier = await db.history.logs(jid, before=page["next_before"], limit=1)
                assert "start" in earlier["items"][0]["msg"] and earlier["next_before"] is None
            await db.delete_job(jobs[0])
            await db.history.append_logs([(jobs[0], 1, 1, "INFO", "test", "late")])
            assert not (await db.history.logs(jobs[0]))["items"]
        finally:
            await db.close()
    asyncio.run(run())


def test_engine_records_new_then_duplicate_list(make_store, boot, caplog):
    caplog.set_level(logging.INFO, logger="hls2strm")
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        try:
            first = await engine.create_crawl("https://jable.tv/models/abc/", end_page=1, detail=False)
            await wait_job(db, first)
            second = await engine.create_crawl("https://jable.tv/models/abc/", end_page=1, detail=False)
            await wait_job(db, second)
        finally:
            await engine.stop()
        counts = await db.history.counts([first, second])
        assert counts[first]["new"] == 24 and counts[first]["existing"] == 0
        assert counts[second]["new"] == 0 and counts[second]["existing"] == 24
        assert counts[first]["added"] == 24 and counts[second]["added"] == 0
        assert any("第 1/2 页" in r["msg"] for r in (await db.history.logs(first))["items"])
        assert any("完成，用时" in r["msg"] for r in (await db.history.logs(second))["items"])
    asyncio.run(run())


def test_cancel_during_final_log_flush_preserves_logs(make_store, caplog):
    caplog.set_level(logging.INFO, logger="hls2strm")
    async def run():
        db, _ = await make_store()
        jid = await db.create_job("videos", "stop", {})
        history = db.history
        append = history.append_logs
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_append(rows):
            entered.set()
            await release.wait()
            await append(rows)
        history.append_logs = slow_append
        async def worker():
            async with capture_task_logs(history, {"job_id": jid, "id": 1}):
                logging.getLogger("hls2strm.test").info("final message")
        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await db.history.logs(jid))["items"][0]["msg"] == "final message"
    asyncio.run(run())


def test_log_retention_and_live_flush(make_store, caplog):
    caplog.set_level(logging.INFO, logger="hls2strm")
    async def run():
        db, _ = await make_store()
        jid = await db.create_job("videos", "retention", {})
        await db.history.append_logs([(jid, 1, i, "INFO", "test", str(i)) for i in range(10005)])
        count = await db._one("SELECT COUNT(*) AS n,MIN(ts) AS oldest FROM job_logs WHERE job_id=?", (jid,))
        assert count["n"] == 10000 and count["oldest"] == 5
        async with capture_task_logs(db.history, {"job_id": jid, "id": 2}):
            logging.getLogger("hls2strm.test").info("still running")
            async with asyncio.timeout(3):
                while not (await db.history.logs(jid, task_id=2))["items"]:
                    await asyncio.sleep(.05)
        assert (await db.history.logs(jid, task_id=2))["items"][0]["msg"] == "still running"
    asyncio.run(run())


def test_history_api_auth_validation_and_missing_job(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path, ui_password="test"))
    with TestClient(app) as client:
        for path in ("logs", "items"):
            assert client.get(f"/api/jobs/999/{path}").status_code == 401
            assert client.get(f"/api/jobs/999/{path}", auth=("admin", "test")).status_code == 404
            assert client.get(f"/api/jobs/999/{path}?limit=-1", auth=("admin", "test")).status_code == 422
