import asyncio
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI

from hls2strm.config import Settings
from hls2strm.errors import PoolBusy
from hls2strm.fetcher import Fetcher
from hls2strm.observability import Metrics
from hls2strm.play import Resolver, router
from hls2strm.sites import SourceDetail
from .conftest import FakeFetcher, FakeMediaSession


def test_legacy_transcoding_settings_become_plain_proxy():
    settings = Settings.model_validate({"play_mode": "continuous", "resolve_mode": "continuous", "continuous_enabled": True,
                                       "file_connections": 256, "sites": {"jable": {"concurrency": 80, "play_concurrency": 50}}})
    assert settings.play_mode == settings.resolve_mode == "proxy"
    assert not any(key.startswith("continuous_") for key in settings.model_dump())
    assert settings.site("jable").concurrency == 80


def test_preflight_is_merged_and_local_overload_does_not_poison_source(make_store):
    async def run():
        db, store = await make_store()
        store.current.quality_capture = False
        vid = await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC",
            stream_url="https://cdn.invalid/a.mp4", stream_expires=int(time.time())+14400), "abc-001", store.current.site_rank)
        calls = 0
        class SlowSession(FakeMediaSession):
            async def get(self, url, **kw):
                nonlocal calls
                calls += 1
                await asyncio.sleep(.02)
                return await super().get(url, **kw)
        fake = FakeFetcher("", {})
        fake.preflight_session = SlowSession()
        resolver = Resolver(db, fake, store, Metrics())
        resolved = await resolver.resolve("abc-001")
        await asyncio.gather(*(resolver.selection.probe(resolved) for _ in range(50)))
        assert calls == 1
        resolver.selection.checked.clear()
        async def busy(*args, **kw):
            raise PoolBusy("preflight 连接额度排队超时")
        fake.preflight_session.get = busy
        with pytest.raises(PoolBusy):
            await resolver.selection.choose("abc-001")
        assert not resolver.selection.cooling
        assert (await db.get_sources(vid))[0]["fail_streak"] == 0
        await resolver.selection.close()
    asyncio.run(run())


def test_fifty_file_streams_leave_hls_preflight_and_speed_resources_available(make_store):
    async def run():
        _, store = await make_store()
        store.current.file_connections = 64
        store.current.pool_wait_timeout = .05
        release = asyncio.Event()
        handlers = set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nx")
                await writer.drain()
                await release.wait()
                writer.write(b"y")
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        fetcher = Fetcher(store, Metrics())
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/media"
        responses = []
        try:
            old = fetcher.file_session
            responses += await asyncio.gather(*(old.get(url, stream=True, timeout=5) for _ in range(50)))
            assert fetcher.metrics.gauges["http_active_file"] == 50
            for session in (fetcher.hls_session, fetcher.preflight_session, fetcher.speed_session):
                responses.append(await session.get(url, stream=True, timeout=5))
            await store.update({"file_connections": 128, "relay_buffer_kb": 64})
            assert fetcher.file_session.capacity == 128
            assert fetcher.file_session.buffer_bytes == 64*1024
            assert not old.idle.is_set()  # 已建立的连接不会被保存设置切断。
        finally:
            release.set()
            await asyncio.gather(*(response.aclose() for response in responses))
            assert all(fetcher.metrics.gauges[f"http_active_{kind}"] == 0 for kind in ("file", "hls", "preflight", "speed"))
            await fetcher.close()
            server.close()
            await server.wait_closed()
            await asyncio.gather(*handlers, return_exceptions=True)
    asyncio.run(run())


def test_pool_queue_timeout_is_distinct_and_releases_waiter(make_store):
    async def run():
        _, store = await make_store()
        store.current.preflight_connections = 1
        store.current.pool_wait_timeout = .01
        fetcher = Fetcher(store, Metrics())
        session = fetcher.preflight_session
        await session.slots.acquire()
        try:
            with pytest.raises(PoolBusy):
                await session.get("https://unused.invalid/video", timeout=1)
            assert session.active == 0 and session.idle.is_set()
            assert fetcher.metrics.gauges["http_waiting_preflight"] == 0
        finally:
            session.slots.release()
            await fetcher.close()
    asyncio.run(run())


def test_candidate_budget_exhausted_in_local_page_queue_does_not_poison_source(make_store):
    async def run():
        db, store = await make_store()
        store.current.resolve_attempt_timeout = .02
        store.current.quality_capture = False
        vid = await db.upsert_detail("jable", SourceDetail(key="abc-004", code="ABC-004", title="ABC"), "abc-004", store.current.site_rank)
        await db.upsert_detail("missav", SourceDetail(key="abc-004", code="ABC-004", title="ABC",
            stream_url="https://cached.invalid/a.mp4"), "abc-004", store.current.site_rank)
        fetcher = Fetcher(store, Metrics())
        slots = fetcher.site("jable")._priority_slots
        for _ in range(store.current.site("jable").play_concurrency):
            await slots.acquire()
        try:
            resolver = Resolver(db, fetcher, store, fetcher.metrics)
            assert (await resolver.resolve("abc-004", min_remaining=60)).site.name == "missav"
            assert (await db.get_sources(vid))[0]["fail_streak"] == 0
        finally:
            for _ in range(store.current.site("jable").play_concurrency):
                slots.release()
            await fetcher.close()
    asyncio.run(run())


def test_gateway_bound_relay_strict_policy_and_external_ip_handling(make_store):
    async def run():
        db, store = await make_store()
        store.current.quality_capture = False
        store.current.resolve_token = "resolve"
        store.current.play_token = "play"
        store.current.resolve_proxy_url = "http://test"
        vid = await db.upsert_detail("javmost", SourceDetail(key="ABC-001", code="ABC-001", title="ABC",
            lines=[("DOO", "embed")]), "abc-001", store.current.site_rank)
        source = (await db.get_sources(vid))[0]
        line = (await db.get_lines(source["id"]))[0]
        await db.set_line_stream(line["id"], "https://cdn.invalid/a.mp4", int(time.time())+14400, "dooplayer")
        fake = FakeFetcher("", {})
        resolver = Resolver(db, fake, store, Metrics())
        app = FastAPI()
        app.include_router(router)
        app.state.ctx = SimpleNamespace(db=db, store=store, resolver=resolver, fetcher=fake, metrics=resolver.metrics)
        auth = {"Authorization": "Bearer resolve"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            strict = await client.get("/api/resolve/abc-001", headers=auth, params={"mode": "strict_redirect"})
            assert strict.status_code == 409
            data = (await client.get("/api/resolve/abc-001", headers=auth)).json()
            assert data["relay"] and urlsplit(data["url"]).path == f"/media/{source['id']}"
            assert data["ttl"] <= 3600  # 未知片长仍不能让网关缓存超过会话最低有效期。
            # 中转入口已经绑定解析时的线路，不再按影片重新选源。
            async def no_selection(*args, **kw):
                raise AssertionError("bound gateway relay must not select again")
            resolver.selection.choose = no_selection
            response = await client.get(data["url"])
            assert response.status_code == 206 and response.content
            assert (await client.get(data["url"].replace("t=play", "t=wrong"))).status_code == 403
        await resolver.selection.close()
    asyncio.run(run())


def test_plain_play_auto_relays_ip_bound_and_legacy_continuous_is_not_transcoded(make_store):
    async def run():
        db, store = await make_store()
        store.current.quality_capture = False
        vid = await db.upsert_detail("javmost", SourceDetail(key="ABC-002", code="ABC-002", title="ABC",
            lines=[("DOO", "embed")]), "abc-002", store.current.site_rank)
        source = (await db.get_sources(vid))[0]
        line = (await db.get_lines(source["id"]))[0]
        await db.set_line_stream(line["id"], "https://cdn.invalid/b.mp4", int(time.time())+14400, "dooplayer")
        fake = FakeFetcher("", {})
        resolver = Resolver(db, fake, store, Metrics())
        app = FastAPI()
        app.include_router(router)
        app.state.ctx = SimpleNamespace(db=db, store=store, resolver=resolver, fetcher=fake, metrics=resolver.metrics)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/play/abc-002.m3u8")).status_code == 206
            await store.update({"play_remote": False})
            assert (await client.get("/play/abc-002.m3u8")).status_code == 302
            assert (await client.get("/play/abc-002.m3u8?continuous=1")).status_code == 206
        await resolver.selection.close()
    asyncio.run(run())
