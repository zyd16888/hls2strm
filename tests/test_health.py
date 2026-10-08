import asyncio
import logging
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

import hls2strm.engine as engine_mod
from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.errors import FetchError
from hls2strm.health import HealthTracker, host_key, measure
from hls2strm.observability import Metrics
from hls2strm.play import Resolver
from hls2strm.sites import SITES

from .conftest import FakeFetcher
from .test_engine import build, model_fixture, wait_job
from .test_quality import _src


def test_tiers_decay_and_persist(make_store):
    async def run():
        db, store = await make_store()
        h = HealthTracker(store)
        assert h.tier("jable") == 0  # 不知道的不压后
        h.record("jable", True, kbps=6000, ttfb_ms=120)
        h.record("voe", True, kbps=800)
        h.record("vidhide", True)
        h.record("vidhide", False, error="HTTP 403")
        assert [h.tier(k) for k in ("jable", "voe", "vidhide")] == [0, 1, 2]  # 正常、慢、不稳
        for _ in range(3):
            h.record("dood", False, error="timeout")
        assert h.tier("dood") == 3 and h.hosts["dood"].last_error == "timeout"
        h.hosts["dood"].last_fail_at = int(time.time()) - 7 * 3600
        assert h.tier("dood") == 0  # 太久没新数据：当作不知道
        await store.update({"health_slow_kbps": 0})
        assert h.tier("voe") == 0  # 不按速度分档

        await db.save_health(h.dump())
        h2 = HealthTracker(store)
        h2.load(await db.load_health())
        assert h2.hosts["vidhide"].fail == 1 and h2.hosts["jable"].kbps == 6000 and not h.dirty
        await db.close()

    asyncio.run(run())


def test_rank_by_health(make_store):
    """播放站不通、不稳的排到后面（在画质前面比）；关掉「按连通性挑源」就不看。线路一样。"""

    async def run():
        db, store = await make_store()
        await store.update({"sites": {"supjav": {"enabled": True}}})
        r = Resolver(db, None, store, Metrics())
        sources = [_src("jable", None), _src("missav", 1080)]
        assert [x["site"] for x in r.rank(sources)] == ["missav", "jable"]
        for _ in range(3):
            r.health.record("missav", False, error="502")
        assert [x["site"] for x in r.rank(sources)] == ["jable", "missav"]
        await store.update({"health_rank": False})
        assert [x["site"] for x in r.rank(sources)] == ["missav", "jable"]

        await store.update({"health_rank": True})
        site = SITES["supjav"]
        lines = [{"id": 1, "line": "EVS", "host": "vidhide", "height": 1080, "fail_streak": 0, "last_fail_at": None},
                 {"id": 2, "line": "VOE", "host": "voe", "height": 720, "fail_streak": 0, "last_fail_at": None}]
        assert [ln["line"] for ln in r.rank_lines(site, lines)] == ["EVS", "VOE"]
        r.health.record("vidhide", True, kbps=500)  # 慢
        assert [ln["line"] for ln in r.rank_lines(site, lines)] == ["VOE", "EVS"]
        assert host_key(site, None, "FST") == "vidhide" and host_key(SITES["javmost"], None, "TURBO") == "javmost:TURBO"
        await db.close()

    asyncio.run(run())


def test_scheduled_check_reuses_fresh_urls(make_store, boot, monkeypatch):
    """定时检测：有没过期的现成地址就不访问源站；测速结果记进连通性并存下来，失败也记。"""
    speeds = [(150.0, 8000.0)]

    async def fake_measure(fetcher, url, headers, nbytes):
        assert nbytes == 512 * 1024 and url.endswith(".m3u8")
        if not speeds:
            raise FetchError("分片返回 HTTP 403")
        return speeds.pop()

    monkeypatch.setattr(engine_mod, "measure", fake_measure)

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = build(db, store, boot, fetcher)
        await engine.start()
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1))  # 抓详情，有了没过期的地址
        calls = len(fetcher.calls)
        await engine.check_health()
        h = engine.resolver.health.hosts["jable"]
        assert (h.ok, h.kbps, h.ttfb_ms) == (1, 8000.0, 150.0) and h.checked_at
        assert len(fetcher.calls) == calls  # 没访问源站
        assert (await db.load_health())[0]["key"] == "jable"

        await engine.check_health()
        h = engine.resolver.health.hosts["jable"]
        assert (h.fail, h.last_error) == (1, "分片返回 HTTP 403")
        assert engine.start_health_check() and not engine.start_health_check()  # 已经在检测
        await engine.stop()
        await db.close()

    asyncio.run(run())


class FakeStream:
    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status_code, self.chunks, self.headers = status, chunks, {}

    async def aiter_content(self):
        for c in self.chunks:
            yield c

    async def aclose(self):
        pass


def test_measure_reads_only_the_start_of_one_segment():
    """多码率的先进第一档，取中间的分片，只下载开头一段（带 Range），算首字节时间和速度。"""
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720\n720p/v.m3u8\n"
    media = "#EXTM3U\n" + "".join(f"#EXTINF:4,\ns{i}.ts\n" for i in range(9))
    got = {}

    class Session:
        async def get(self, url, *, stream, headers, timeout):
            got.update(url=url, range=headers["Range"])
            return FakeStream(200, [b"x" * 65536] * 8)

    f = SimpleNamespace(session=Session(), store=SimpleNamespace(current=SimpleNamespace(request_timeout=30)))

    async def get_bytes(url, *, headers=None):
        return {"https://cdn/a/master.m3u8": master, "https://cdn/a/720p/v.m3u8": media}[url].encode()

    f.get_bytes = get_bytes
    ttfb, kbps = asyncio.run(measure(f, "https://cdn/a/master.m3u8", {}, 131072))
    assert got == {"url": "https://cdn/a/720p/s4.ts", "range": "bytes=0-131071"} and ttfb >= 0 and kbps > 0


def test_relay_errors_count_against_the_host(tmp_path):
    """中转时 CDN 出错记进这个播放站的连通性，成功也记；状态接口能看到。"""
    app = create_app(BootConfig(data_dir=tmp_path, default_public_base_url="http://hls2strm:8080"))
    try:
        with TestClient(app) as c:
            html, ids = model_fixture()
            fake = FakeFetcher(html, ids)
            app.state.ctx.fetcher = fake
            app.state.ctx.resolver.fetcher = fake
            app.state.ctx.resolver.quality.fetcher = fake
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Lavf/61"})  # 中转：读播放列表成功
            assert r.status_code == 200
            src_id = c.get("/api/videos", params={"q": "ipzz-983"}).json()["items"][0]["sources"][0]["id"]

            class Down:
                async def get(self, url, **kw):
                    return FakeStream(500, [])

            fake.session = Down()
            assert c.get(f"/hls/{src_id}/a.ts").status_code == 502
            hosts = {h["key"]: h for h in c.get("/api/health").json()["hosts"]}
            # 地址是现场找源时抓详情拿到的（缓存命中不算），读播放列表成功一次，分片失败一次
            assert (hosts["jable"]["ok"], hosts["jable"]["fail"]) == (1, 1)
            assert hosts["jable"]["last_error"] == "CDN 返回 HTTP 500" and hosts["jable"]["label"] == "Jable"
            video = c.get("/api/videos", params={"q": "ipzz-983"}).json()["items"][0]
            assert video["sources"][0]["health"] in (0, 2)
    finally:
        for hd in logging.getLogger().handlers:
            hd.close()
        logging.getLogger().handlers.clear()
