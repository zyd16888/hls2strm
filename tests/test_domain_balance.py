import asyncio
import time

import pytest

from hls2strm.errors import FetchError, NotFound
from hls2strm.config import Settings
from .test_fetcher import CHALLENGE, Resp, make_fetcher


def test_priority_remains_default_and_domain_duplicates_are_removed():
    settings = Settings(sites={"jable": {"domains": ["fs1.app/", "https://fs1.app", "jable.tv"]}})
    assert settings.site("jable").domain_mode == "priority"
    assert settings.site("jable").domains == ["https://fs1.app", "https://jable.tv"]


def test_round_robin_distributes_requests_and_skips_cooling_mirrors(make_store):
    async def run():
        _, store = await make_store()
        await store.update({"sites": {"jable": {"domain_mode": "round_robin"}}})
        fetcher = make_fetcher(store, {"fs1.app": Resp(), "jable.tv": Resp()})
        pages = [await fetcher.get_page(f"/{i}", priority=True) for i in range(6)]
        assert [page.domain for page in pages] == ["https://fs1.app", "https://jable.tv"]*3
        fetcher.domains[0].cooldown_until = time.time()+60
        assert (await fetcher.get_page("/next", priority=True)).domain == "https://jable.tv"
        assert all(domain.in_flight == 0 for domain in fetcher.domains)
    asyncio.run(run())


def test_balanced_concurrent_requests_do_not_pile_onto_the_first_mirror(make_store):
    async def run():
        _, store = await make_store()
        await store.update({"sites": {"jable": {"domain_mode": "balanced"}}})
        fetcher = make_fetcher(store, {})
        started = []
        release = asyncio.Event()
        class Session:
            async def get(self, url, **kw):
                started.append(url)
                if len(started) == 2: release.set()
                await asyncio.wait_for(release.wait(), 1)
                return Resp(url=url)
        fetcher.parent._session = Session()
        a, b = await asyncio.gather(fetcher.get_page("/a", priority=True), fetcher.get_page("/b", priority=True))
        assert {a.domain, b.domain} == {"https://fs1.app", "https://jable.tv"}
        assert all(domain.in_flight == 0 and domain.response_ms is not None for domain in fetcher.domains)
        # 没有正在处理的请求时优先响应更快的镜像。
        fetcher.domains[0].response_ms = 400
        fetcher.domains[1].response_ms = 40
        assert (await fetcher.get_page("/fast", priority=True)).domain == "https://jable.tv"
    asyncio.run(run())


def test_short_error_cooldown_recovers_without_resetting_the_whole_site(make_store):
    async def run():
        _, store = await make_store()
        await store.update({"sites": {"jable": {"domain_mode": "round_robin", "domain_error_cooldown": 15}}})
        fetcher = make_fetcher(store, {"fs1.app": OSError("timeout"), "jable.tv": Resp()})
        assert (await fetcher.get_page("/a", priority=True)).domain == "https://jable.tv"
        failed = fetcher.domains[0]
        assert failed.error_until > time.time() and failed.blocked == 0
        assert (await fetcher.get_page("/b", priority=True)).domain == "https://jable.tv"
        assert len(fetcher.parent._session.calls) == 3  # 短冷却内没有再次请求故障镜像。
        failed.error_until = time.time()-1
        fetcher.parent._session.by_host["fs1.app"] = Resp()
        assert (await fetcher.get_page("/recovered", priority=True)).domain == "https://fs1.app"
        assert failed.error_until == 0 and failed.in_flight == 0
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["round_robin", "balanced"])
def test_single_mirror_404_does_not_mean_the_video_is_gone(make_store, mode):
    async def run():
        _, store = await make_store()
        await store.update({"sites": {"jable": {"domain_mode": mode}}})
        fetcher = make_fetcher(store, {"fs1.app": Resp(404), "jable.tv": Resp()})
        assert (await fetcher.get_page("/video", priority=True)).domain == "https://jable.tv"
        fetcher = make_fetcher(store, {"fs1.app": Resp(404), "jable.tv": Resp(404)})
        with pytest.raises(NotFound, match="所有镜像"):
            await fetcher.get_page("/gone", priority=True)
        # 其他镜像不可访问时无法确认下架，不能把部分镜像 404 变成 NotFound。
        fetcher = make_fetcher(store, {"fs1.app": Resp(404), "jable.tv": CHALLENGE})
        with pytest.raises(FetchError):
            await fetcher.get_page("/unknown", priority=True)
        fetcher = make_fetcher(store, {"fs1.app": Resp(404), "jable.tv": Resp()})
        fetcher.domains[1].cooldown_until = time.time()+60
        with pytest.raises(FetchError):
            await fetcher.get_page("/unknown", priority=True)
    asyncio.run(run())


def test_cancellation_releases_mirror_load_and_mode_updates_preserve_cookies(make_store):
    async def run():
        _, store = await make_store()
        await store.update({"sites": {"jable": {"domain_mode": "balanced"}}})
        fetcher = make_fetcher(store, {})
        active = asyncio.Event()
        class Session:
            async def get(self, url, **kw):
                active.set()
                await asyncio.Event().wait()
        fetcher.parent._session = Session()
        task = asyncio.create_task(fetcher.get_page("/slow", priority=True))
        await asyncio.wait_for(active.wait(), 1)
        assert sum(domain.in_flight for domain in fetcher.domains) == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert all(domain.in_flight == 0 for domain in fetcher.domains)
        first = fetcher.domains[0]
        first.cookies = {"test": "only-first-mirror"}
        await store.update({"sites": {"jable": {"domain_mode": "round_robin", "domains": ["jable.tv", "fs1.app"]}}})
        assert fetcher.domains[1] is first and fetcher.domains[1].cookies == {"test": "only-first-mirror"}
        assert not fetcher.domains[0].cookies
    asyncio.run(run())
