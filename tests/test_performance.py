"""用户可见的超时、并发中转、重试隔离和数据库排队回归。"""

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.cache import AsyncCache
from hls2strm.config import BootConfig, Settings
from hls2strm.errors import FetchError, NotFound
from hls2strm.http_resources import BUFFER_BYTES, ManagedSession
from hls2strm.observability import Metrics, RingHandler
from hls2strm.play import Resolver, _upstream
from hls2strm.sites import SourceDetail
from .conftest import FakeFetcher
from .test_engine import build, model_fixture, wait_job


def test_cold_discovery_and_cdn_obey_total_budget(make_store, monkeypatch, tmp_path):
    async def run():
        db, store = await make_store()
        store.current.resolve_timeout = .03
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        entered = asyncio.Event()
        async def discover(_):
            entered.set()
            await asyncio.sleep(1)
        monkeypatch.setattr(resolver, "discover", discover)
        with pytest.raises(FetchError, match="总时间预算"):
            await resolver.resolve("abc-001")
        assert entered.is_set()
    asyncio.run(run())

    app = create_app(BootConfig(data_dir=tmp_path / "http"))
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.store.current.resolve_timeout = .03
        async def slow(*args, **kw):
            await asyncio.sleep(1)
        monkeypatch.setattr(ctx.resolver, "resolve", slow)
        response = client.get("/play/abc-001.m3u8")
        assert response.status_code == 504 and "x-request-id" in response.headers


def test_slow_first_source_leaves_time_for_cached_backup(make_store):
    async def run():
        db, store = await make_store()
        store.current.resolve_attempt_timeout = .02
        store.current.resolve_timeout = .2
        store.current.quality_capture = False
        vid = await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"), "abc-001", store.current.site_rank)
        await db.upsert_detail("missav", SourceDetail(key="abc-001", code="ABC-001", title="ABC", stream_url="https://surrit.invalid/video.m3u8"), "abc-001", store.current.site_rank)
        class SlowFetcher(FakeFetcher):
            async def get_page(self, *args, **kw):
                await asyncio.sleep(1)
        resolver = Resolver(db, SlowFetcher("", {}), store, Metrics())
        resolved = await resolver.resolve("abc-001", min_remaining=60)
        assert resolved.source["site"] == "missav"
        assert (await db.get_sources(vid))[0]["fail_streak"] == 1
    asyncio.run(run())


def test_playback_sessions_pin_lines_and_reuse_url_snapshot(make_store):
    async def run():
        db, store = await make_store()
        store.current.sites["supjav"].enabled = True
        detail = SourceDetail(key="abc-001", code="ABC-001", title="ABC", lines=[("EVS", "a"), ("VOE", "b")])
        vid = await db.upsert_detail("supjav", detail, "abc-001", store.current.site_rank)
        src = (await db.get_sources(vid))[0]
        for ln in await db.get_lines(src["id"]):
            await db.set_line_stream(ln["id"], f"https://{ln['line']}.invalid/old/video.m3u8", int(time.time())+86400, "vidhide" if ln["line"] == "EVS" else "voe")
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        v = await db.get_video_by_id(vid)
        first = await resolver._ensure(v, src, 60, line="EVS")
        binding = await resolver.sessions.persist(db, first)
        await resolver._ensure(v, await db.get_source(src["id"]), 60, line="VOE")
        later = await resolver.session_source(src["id"], binding)
        assert later.line["line"] == "EVS"
        assert _upstream(later, "segment.ts") == "https://EVS.invalid/old/segment.ts"
        with pytest.raises(NotFound):
            resolver.sessions.get(binding, src["id"]+1)
        assert resolver.sessions.get(binding, src["id"]).line["line"] == "EVS"
        restarted = Resolver(db, FakeFetcher("", {}), store, Metrics())
        assert (await restarted.session_source(src["id"], binding)).line["line"] == "EVS"
    asyncio.run(run())


def test_failed_list_is_not_complete_but_cover_failure_is_independent(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        sub = await db.create_subscription(name="test", source="/", library_id=1, initialized=0)
        job = await db.create_job("crawl", "test", {"subscription_id": sub})
        await db.add_tasks(job, "list", [1, 2])
        tasks = await db.list_tasks(job)
        await db.finish_task(tasks[0]["id"], "done")
        await db.finish_task(tasks[1]["id"], "failed", "network")
        await engine._maybe_finish_job(job)
        assert (await db.get_subscription(sub))["initialized"] == 0
        assert not (await db.get_job(job))["state"]["list_complete"]
        assert sub not in await db.subscription_last_done()
        await db.update_job(job, status="running")
        await db.finish_task(tasks[1]["id"], "done")
        await db.add_tasks(job, "cover", ["1:1"])
        cover = next(t for t in await db.list_tasks(job) if t["kind"] == "cover")
        await db.finish_task(cover["id"], "failed", "cdn")
        await engine._maybe_finish_job(job)
        assert (await db.get_subscription(sub))["initialized"] == 1
        assert (await db.get_job(job))["state"]["partial_failure"]
        assert sub in await db.subscription_last_done()
    asyncio.run(run())


def test_cover_retry_does_not_repeat_source_fetch(make_store, boot):
    async def run():
        db, store = await make_store()
        await store.update({"retry_base_delay": 1, "max_attempts": 2, "quality_capture": False})
        html, ids = model_fixture()
        class CoverDown(FakeFetcher):
            async def get_bytes(self, url, **kw):
                if not url.endswith(".m3u8"):
                    raise FetchError("cover unavailable")
                return await super().get_bytes(url, **kw)
        fetcher = CoverDown(html, ids)
        engine = build(db, store, boot, fetcher)
        await engine.start()
        try:
            job = await wait_job(db, await engine.create_videos([("jable", "ipzz-983")]))
            assert job["state"]["partial_failure"]
            assert fetcher.calls.count("/videos/ipzz-983/") == 1
            assert await db.task_counts(job["id"]) == {"done": 1, "failed": 1}
        finally:
            await engine.stop()
    asyncio.run(run())


def test_bulk_query_does_not_block_playback_read(make_store):
    async def run():
        db, store = await make_store()
        await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"), "abc-001", store.current.site_rank)
        entered = threading.Event()
        def busy():
            entered.set()
            time.sleep(.3)
            return 1
        await db.bulk_reader.create_function("audit_busy", 0, busy)
        task = asyncio.create_task(db._one("SELECT audit_busy()", bulk=True))
        await asyncio.to_thread(entered.wait, 1)
        assert (await db.get_video("abc-001"))["title"] == "ABC"
        assert not task.done()
        await task
    asyncio.run(run())


def test_cache_cancellation_isolated_between_waiters():
    async def run():
        cache = AsyncCache()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def load():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"ok": True}
        first = asyncio.create_task(cache.get("x", load, 30))
        await started.wait()
        second = asyncio.create_task(cache.get("x", load, 30))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        assert await second == {"ok": True} and calls == 1
    asyncio.run(run())


def test_media_backpressure_and_disconnect_release_connection():
    async def run():
        payload = b"x" * (BUFFER_BYTES * 6)
        async def serve(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()+payload)
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        metrics = Metrics()
        session = ManagedSession(Settings(), metrics, "media", 1)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/video"
        try:
            resp = await session.get(url, stream=True, timeout=5)
            await asyncio.sleep(.05)
            assert resp.status_code == 200 and resp.buffered <= BUFFER_BYTES
            assert metrics.counters["relay_buffer_pauses"] > 0
            total = 0
            async for chunk in resp.aiter_content():
                total += len(chunk)
            assert total == len(payload)
            await resp.aclose()
            assert session.active == 0 and metrics.gauges["relay_buffer_bytes"] == 0
            resp = await session.get(url, stream=True, timeout=5)
            await resp.aclose()  # 消费前断开也释放上游与连接额度
            assert session.active == 0
        finally:
            await session.close()
            server.close()
            await server.wait_closed()
    asyncio.run(run())


def test_log_restart_has_distinct_instance_and_gap_is_counted():
    first, second = RingHandler(), RingHandler()
    assert first.instance != second.instance
    subscriber = first.subscribe()
    for i in range(1001):
        first._offer(subscriber, {"id": i})
    assert first.dropped == 1
    assert subscriber.get_nowait()["id"] == 1


def test_listing_completion_does_not_wait_for_metadata(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        sub = await db.create_subscription(name="test", source="/", library_id=1, initialized=0)
        job = await db.create_job("crawl", "test", {"subscription_id": sub})
        await db.update_job(job, started_at=123)
        await db.add_tasks(job, "list", [1])
        await db.add_tasks(job, "detail", ["abc-001"])
        listing = next(t for t in await db.list_tasks(job) if t["kind"] == "list")
        await db.finish_task(listing["id"], "done")
        await engine._update_list_completion(job)
        assert (await db.get_job(job))["status"] == "running"
        assert (await db.get_subscription(sub))["initialized"] == 1
        assert (await db.subscription_last_done())[sub] == 123
        assert await db.subscription_active_job(sub, listing_only=True) is None
        assert (await db.subscription_active_job(sub))["id"] == job  # 删除订阅仍保护后台任务
    asyncio.run(run())


def test_membership_write_retry_finishes_output(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        await store.update({"retry_base_delay": 1, "download_cover": False})
        vid = await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"), "abc-001", store.current.site_rank)
        engine = build(db, store, boot, FakeFetcher("", {}))
        original, calls = engine.writer.write, []
        def write(*args):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("temporarily unavailable")
            return original(*args)
        monkeypatch.setattr(engine.writer, "write", write)
        await engine.start()
        try:
            job = await wait_job(db, await engine.create_membership([vid], 1))
            assert job["status"] == "done" and len(calls) == 2
            out = await db.get_output(vid, 1)
            from pathlib import Path
            assert Path(out["strm_path"]).exists()
            await wait_job(db, await engine.create_membership([vid], 1, remove=True))
            assert await db.get_output(vid, 1) is None
            assert await db.get_video_by_id(vid) is not None
        finally:
            await engine.stop()
    asyncio.run(run())


def test_scan_resume_keeps_published_index_until_complete(make_store, boot):
    from hls2strm.engine import TaskStopped
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.reload_libraries()
        root = boot.data_dir / "scan"
        root.mkdir()
        for i in range(260):
            (root / f"{i:04}.strm").write_text(f"http://test/play/abc-{i}.m3u8", encoding="utf-8")
        await db._write("INSERT INTO strm_files(path,scan_id,kind,scanned_at) VALUES(?,99,'other',0)", (str(root / "old.strm"),))
        job_id = await engine.strm.create_scan(str(root))
        job = await db.get_job(job_id)
        original, calls = engine.checkpoint, []
        async def pause(jid):
            calls.append(1)
            if len(calls) == 3:
                await db.update_job(jid, status="paused")
            await original(jid)
        engine.checkpoint = pause
        with pytest.raises(TaskStopped):
            await engine.strm.do_scan(job, {})
        assert await db.get_strm_file(str(root / "old.strm")) is not None
        assert (await db._one("SELECT COUNT(*) AS n FROM scan_staging WHERE scan_id=?", (job_id,)))["n"] == 256
        engine.checkpoint = original
        await db.update_job(job_id, status="running")
        await engine.strm.do_scan(await db.get_job(job_id), {})
        assert (await db.get_job(job_id))["state"]["files"] == 260
        assert await db.get_strm_file(str(root / "old.strm")) is None
    asyncio.run(run())


def test_cancel_during_begin_does_not_poison_next_transaction(make_store, monkeypatch):
    async def run():
        db, _ = await make_store()
        original = db.conn.execute
        async def delayed(sql, *args):
            result = await original(sql, *args)
            if sql == "BEGIN":
                await asyncio.sleep(1)
            return result
        monkeypatch.setattr(db.conn, "execute", delayed)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.03):
                async with db._tx():
                    pass
        monkeypatch.setattr(db.conn, "execute", original)
        async with db._tx():
            await db.conn.execute("INSERT INTO settings(key,value) VALUES('after_cancel','ok')")
        assert await db.get_setting("after_cancel") == "ok"
    asyncio.run(run())


def test_single_actions_return_jobs_and_validation_is_clear(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.engine.pause()
        async def seed():
            await ctx.store.update({"sites": {site: {"enabled": site == "jable"} for site in ctx.store.current.sites}})
            return await ctx.db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"), "abc-001", ctx.store.current.site_rank)
        vid = client.portal.call(seed)
        response = client.post("/api/videos/abc-001/refresh")
        assert response.status_code == 202
        async def drain_claims():
            async with ctx.engine._claim_lock:
                pass
        client.portal.call(drain_claims)
        job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
        assert job["tasks"] == {"pending": 1}
        assert client.post("/api/videos/abc-001/probe").status_code == 400
        assert client.post("/api/videos/batch", json={"action": "add", "ids": [vid], "library_id": 1}).status_code == 202
        assert client.get("/readyz").status_code == 200  # 暂停不是服务故障
