import asyncio
import json
import time
from types import SimpleNamespace

from hls2strm.line_speed import speed_events
from hls2strm.sites import SourceDetail
from .conftest import FakeFetcher


def test_all_lines_run_in_parallel_and_results_do_not_change_playback(make_store):
    async def run():
        db, store = await make_store()
        store.current.speed_test_bytes = 1
        vid = await db.upsert_detail("javmost", SourceDetail(key="ABC-001", code="ABC-001", title="ABC",
            lines=[("DOO", "one"), ("DOOD", "two"), ("MOST", "unsupported")]), "abc-001", store.current.site_rank)
        source = (await db.get_sources(vid))[0]
        for line in await db.get_lines(source["id"]):
            await db.set_line_stream(line["id"], f"https://cdn.invalid/{line['line']}.mp4", int(time.time())+14400,
                                     "dooplayer" if line["line"] == "DOO" else "dood")
        before = await db.get_source(source["id"])
        all_started = asyncio.Event()
        calls, closed = [], []
        class Response:
            status_code = 206
            headers = {"content-type": "video/mp4"}
            def __init__(self, url): self.url = url
            async def aiter_content(self):
                yield b"x"*8192  # 上游忽略 Range 时仍按采样上限停止。
                raise AssertionError("must stop after sample limit")
            async def aclose(self): closed.append(self.url)
        class Session:
            async def get(self, url, **kw):
                calls.append((url, kw))
                if len(calls) == 2: all_started.set()
                await asyncio.wait_for(all_started.wait(), 1)  # 串行执行会失败。
                return Response(url)
        ctx = SimpleNamespace(db=db, store=store, fetcher=FakeFetcher("", {}))
        ctx.fetcher.speed_session = Session()
        events = [json.loads(value) async for value in speed_events(ctx, await db.get_video_by_id(vid))]
        assert events[0]["event"] == "start" and events[-1]["event"] == "done"
        results = {event["item"]["line"]: event["item"] for event in events if event["event"] == "result"}
        assert results["DOO"]["status"] == results["DOOD"]["status"] == "ok"
        assert results["DOO"]["sample_bytes"] == 1024
        assert results["DOO"]["mode"] == "原样中转"
        assert results["MOST"]["status"] == "skipped"
        assert len(closed) == 2 and all(kw["headers"]["Range"] == "bytes=0-1023" for _, kw in calls)
        assert await db.get_source(source["id"]) == before
    asyncio.run(run())


def test_hls_http_failure_timeout_disabled_and_partial_results(make_store):
    async def run():
        db, store = await make_store()
        store.current.speed_test_timeout = .1
        store.current.site("supjav").enabled = True
        vid = await db.upsert_detail("supjav", SourceDetail(key="abc-002", code="ABC-002", title="ABC",
            lines=[("ST", "ok"), ("EVS", "bad"), ("FST", "slow"), ("VOE", "disabled")]), "abc-002", store.current.site_rank)
        source = (await db.get_sources(vid))[0]
        for line in await db.get_lines(source["id"]):
            name = line["line"]
            await db.set_line_stream(line["id"], f"https://cdn.invalid/{name}.m3u8", int(time.time())+14400, "vidhide")
        store.current.site("supjav").line("VOE").enabled = False
        closed = []
        class Response:
            headers = {"content-type": "video/mp2t"}
            content = b"#EXTM3U\n#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
            def __init__(self, status): self.status_code = status
            async def aiter_content(self): yield b"x"*4096
            async def aclose(self): closed.append(True)
        class Session:
            async def get(self, url, **kw):
                if "FST" in url: await asyncio.sleep(1)
                return Response(403 if "EVS" in url else 206 if kw.get("stream") else 200)
        fake = FakeFetcher("", {})
        fake.speed_session = Session()
        ctx = SimpleNamespace(db=db, store=store, fetcher=fake)
        results = [json.loads(value)["item"] async for value in speed_events(ctx, await db.get_video_by_id(vid))
                   if json.loads(value)["event"] == "result"]
        by_line = {row["line"]: row for row in results}
        assert by_line["ST"]["status"] == "ok" and by_line["ST"]["manifest_ms"] >= 0
        assert by_line["EVS"]["status"] == "failed" and by_line["EVS"]["http_status"] == 403
        assert by_line["FST"]["status"] == "timeout"
        assert by_line["VOE"]["status"] == "skipped"
        assert closed
        assert all(line["fail_streak"] == 0 for line in await db.get_lines(source["id"]))
    asyncio.run(run())


def test_speed_cancellation_closes_active_media(make_store):
    async def run():
        db, store = await make_store()
        vid = await db.upsert_detail("jable", SourceDetail(key="abc-003", code="ABC-003", title="ABC",
            stream_url="https://cdn.invalid/a.mp4", stream_expires=int(time.time())+14400), "abc-003", store.current.site_rank)
        active, closed = asyncio.Event(), asyncio.Event()
        class Response:
            status_code = 200
            headers = {"content-type": "video/mp4"}
            async def aiter_content(self):
                active.set()
                await asyncio.Event().wait()
                yield b"unused"
            async def aclose(self): closed.set()
        class Session:
            async def get(self, *args, **kw): return Response()
        fake = FakeFetcher("", {})
        fake.speed_session = Session()
        ctx = SimpleNamespace(db=db, store=store, fetcher=fake)
        async def consume():
            async for _ in speed_events(ctx, await db.get_video_by_id(vid)): pass
        task = asyncio.create_task(consume())
        await asyncio.wait_for(active.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert closed.is_set()
    asyncio.run(run())


def test_speed_endpoint_requires_login_and_reports_missing_video(tmp_path):
    from fastapi.testclient import TestClient
    from hls2strm.app import create_app
    from hls2strm.config import BootConfig
    with TestClient(create_app(BootConfig(data_dir=tmp_path, ui_password="secret"))) as client:
        assert client.post("/api/videos/abc-001/speed-test", json={}).status_code == 401
        assert client.post("/api/videos/abc-001/speed-test", json={}, auth=("admin", "secret")).status_code == 404
