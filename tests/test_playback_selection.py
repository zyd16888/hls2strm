import asyncio
import time

import pytest

from hls2strm.errors import FetchError, NotFound
from hls2strm.observability import Metrics
from hls2strm.play import Resolver
from hls2strm.sites import SourceDetail, SITES
from .conftest import FakeFetcher


def test_media_failure_falls_back_without_replacing_existing_session(make_store, monkeypatch):
    async def run():
        db, store = await make_store()
        store.current.quality_capture = False
        for site in ("jable", "missav"):
            await db.upsert_detail(site, SourceDetail(key="abc-001", code="ABC-001", title="ABC",
                stream_url=f"https://{site}.invalid/a.m3u8", stream_expires=int(time.time()) + 14400),
                "abc-001", store.current.site_rank)
        r = Resolver(db, FakeFetcher("", {}), store, Metrics())
        first = await r.resolve("abc-001")
        session = await r.sessions.persist(db, first)
        seen = []
        async def probe(resolved, want):
            seen.append(resolved.site.name)
            if resolved.site.name == "jable": raise FetchError("CDN returned HTML")
        monkeypatch.setattr(r.selection, "probe", probe)
        chosen = await r.selection.choose("abc-001")
        assert seen == ["jable", "missav"] and chosen.site.name == "missav"
        assert (await r.session_source(first.source["id"], session)).url == first.url
        assert (await db.get_source(first.source["id"]))["status"] == "active"
        seen.clear()
        assert (await r.selection.choose("abc-001")).site.name == "missav"
        assert seen == ["missav"]  # 冷却期间跳过坏源
        with pytest.raises((NotFound, FetchError)):
            await r.selection.choose("abc-001", excluded=frozenset({(chosen.source["id"], 0)}))
    asyncio.run(run())


def test_line_exclusion_preserves_other_lines_and_explicit_direct_policy(make_store, monkeypatch):
    async def run():
        db, store = await make_store()
        vid = await db.upsert_detail("javmost", SourceDetail(key="ABC-002", code="ABC-002", title="ABC",
            lines=[("DOO", "one"), ("DOOD", "two")]), "abc-002", store.current.site_rank)
        src = (await db.get_sources(vid))[0]
        lines = await db.get_lines(src["id"])
        for ln in lines:
            await db.set_line_stream(ln["id"], f"https://cdn.invalid/{ln['line']}.mp4", int(time.time())+14400,
                                     "dooplayer" if ln["line"] == "DOO" else "dood")
        r = Resolver(db, FakeFetcher("", {}), store, Metrics())
        first = await r.resolve("abc-002", site="javmost", line="DOO")
        assert first.traits.ip_uncertain and first.traits.ip_bound
        second = await r.resolve("abc-002", excluded=frozenset({(src["id"], first.line["id"])}))
        assert second.line["line"] == "DOOD"
        assert (await db.get_source(src["id"]))["fail_streak"] == 0
        with pytest.raises(FetchError):
            await r.resolve("abc-002", excluded=frozenset((src["id"], ln["id"]) for ln in lines))
        assert (await db.get_source(src["id"]))["fail_streak"] == 0  # 全部主动排除也不能惩罚健康源。
        assert not r._direct_ok(SITES["javmost"])
        await store.update({"sites": {"javmost": {"lines": {"DOO": {"direct_mode": "allow"}}}}})
        allowed = await r.resolve("abc-002", direct_only=True, remote=True)
        assert allowed.line["line"] == "DOO" and not allowed.traits.ip_bound
        sid = await r.sessions.persist(db, allowed)
        fresh = Resolver(db, FakeFetcher("", {}), store, Metrics())
        assert not (await fresh.session_source(src["id"], sid)).traits.ip_bound
        await store.update({"sites": {"javmost": {"lines": {"DOO": {"direct_mode": "proxy"}}}}})
        assert not r._direct_ok(SITES["javmost"])
    asyncio.run(run())
