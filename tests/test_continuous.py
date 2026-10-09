import asyncio
from types import SimpleNamespace

import pytest

from hls2strm.continuous import ContinuousPlayback, ContinuousSession
from hls2strm.errors import FetchError
from hls2strm.observability import Metrics
from hls2strm.play import Resolved, Resolver
from hls2strm.sites import SITES, SourceDetail
from .conftest import FakeFetcher


def test_continuous_failover_atomic_cache_and_restart(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        store.current.continuous_enabled = True
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        await db.upsert_detail("missav", SourceDetail(key="abc-001", code="ABC-001", title="ABC",
            stream_url="https://first.invalid/video.mp4", duration=20), "abc-001", store.current.site_rank)
        resolved = await resolver.resolve("abc-001")
        ctx = SimpleNamespace(db=db, store=store, resolver=resolver, metrics=Metrics(), boot=boot)
        service = ContinuousPlayback(ctx)
        monkeypatch.setattr(service, "_available", lambda: None)
        async def inspect(session, source):
            return source.url, 20, True
        monkeypatch.setattr(service, "inspect", inspect)
        session = await service.create(resolved)
        playlist = service.playlist(session, "a&b")
        assert playlist.count("#EXTINF") == 4 and "#EXT-X-ENDLIST" in playlist and "t=a%26b" in playlist
        alternate = Resolved(resolved.video, {**resolved.source, "id": 999, "stream_url": "https://second.invalid/video.mp4"}, SITES["missav"])
        async def choose(slug, **kw):
            assert kw["excluded"] == frozenset({(resolved.source["id"], 0)})
            return alternate
        monkeypatch.setattr(resolver.selection, "choose", choose)
        generated = []
        async def encode(url, audio, session, index, path):
            generated.append((url, index))
            path.write_bytes(b"partial")
            if url == resolved.url:
                raise FetchError("upstream failed")
            path.write_bytes(b"complete-ts")
        monkeypatch.setattr(service, "_encode", encode)
        # 直接请求尾部，模拟外部播放器拖动；多个等待者只生成一份。
        values = await asyncio.gather(service.segment(session, 3), service.segment(session, 3))
        assert values == [b"complete-ts", b"complete-ts"] and len(generated) == 2
        assert session.resolved is alternate and ctx.metrics.counters["continuous_switch"] == 1
        assert await service.segment(session, 3) == b"complete-ts" and len(generated) == 2
        assert not list(service.root.rglob("*.part"))
        await service.close()
        restarted = ContinuousPlayback(ctx)
        monkeypatch.setattr(restarted, "_available", lambda: None)
        recovered = await restarted.get(session.id)
        assert recovered.duration == 20 and await restarted.segment(recovered, 3) == b"complete-ts"
        restarted._remove(session.id)
        assert not (service.root / session.id).exists()
        await restarted.close()
    asyncio.run(run())


def test_continuous_cancel_removes_partial_and_does_not_publish(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        ctx = SimpleNamespace(db=db, store=store, metrics=Metrics(), boot=boot)
        service = ContinuousPlayback(ctx)
        source = SimpleNamespace(url="https://cdn/video", source={"id": 1}, line=None)
        session = ContinuousSession("a" * 24, source, 60, 360)
        directory = service.root / session.id
        directory.mkdir(parents=True)
        async def inspect(*args): return source.url, 60, True
        started = asyncio.Event()
        async def encode(url, audio, session, index, path):
            path.write_bytes(b"partial")
            started.set()
            await asyncio.sleep(100)
        monkeypatch.setattr(service, "inspect", inspect)
        monkeypatch.setattr(service, "_encode", encode)
        task = asyncio.create_task(service._generate(session, 0, directory / "0.ts"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not (directory / "0.ts").exists() and not (directory / "0.part").exists()
        service._remove(session.id)
    asyncio.run(run())
