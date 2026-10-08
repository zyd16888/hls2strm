import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.errors import FetchError
from hls2strm.observability import Metrics
from hls2strm.play import Resolver
from hls2strm.quality import Quality, from_labels, from_master, height_for_kbps, probe
from hls2strm.sites import SITES, SourceDetail
from hls2strm.sites.supjav import claimed_height

from .conftest import FakeFetcher, fixture
from .test_engine import build, model_fixture, wait_job

MASTER = (
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=842x480\n480p/video.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n720p/video.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360\n360p/video.m3u8\n"
)


def test_parse_quality():
    q = from_master(MASTER)
    assert (q.heights, q.src, q.height) == ([720, 480, 360], "master", 720)
    q = from_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5000000\na.m3u8\n")  # 没写分辨率：按码率估
    assert (q.heights, q.src) == ([1080], "estimate")
    assert from_master("#EXTM3U\n#EXTINF:4,\na.ts\n") is None  # 媒体播放列表不是 master
    assert from_labels(["HD", "1080p", "JAV"], "claimed").heights == [1080]
    assert from_labels(["4K"], "embed").height == 2160 and from_labels(["HD"], "embed") is None
    assert [height_for_kbps(k) for k in (2200, 4000, 900, 300)] == [720, 1080, 480, 360]  # Jable 约 2 Mbps 实测是 720p
    assert claimed_height("[4K][英文字幕]IPZZ-983 a") == 2160 and claimed_height("[中文字幕]SNOS-223 x") == 0
    assert SITES["javguru"].parse_detail(fixture("javguru_detail.html"), "1").claimed_height == 1080


class CdnFetcher:
    """CDN：播放列表按地址给，分片 HEAD 给大小，记下 HEAD 过哪些地址。"""

    def __init__(self, files: dict[str, str], sizes: dict[str, int]) -> None:
        self.files, self.sizes, self.heads = files, sizes, []

    async def get_bytes(self, url, *, referer=None, headers=None):
        return self.files[url].encode()

    async def fetch(self, url, *, method="GET", headers=None, **kw):
        self.heads.append(url)
        return SimpleNamespace(status_code=200, headers={"content-length": str(self.sizes.get(url, 0))})


def test_probe_without_downloading_video():
    """master 读一次就知道；单档的媒体播放列表只 HEAD 三个分片按码率估，不下载视频；mp4 认不出。"""
    f = CdnFetcher({"https://cdn/m/master.m3u8": MASTER}, {})
    assert asyncio.run(probe(f, "https://cdn/m/master.m3u8")).heights == [720, 480, 360] and f.heads == []
    media = "#EXTM3U\n" + "".join(f"#EXTINF:4.0,\nseg{i}.ts\n" for i in range(100))
    f = CdnFetcher({"https://cdn/j/x.m3u8": media}, {f"https://cdn/j/seg{i}.ts": 1_100_000 for i in range(100)})
    q = asyncio.run(probe(f, "https://cdn/j/x.m3u8"))
    assert (q.heights, q.src) == ([720], "estimate") and len(f.heads) == 3
    assert asyncio.run(probe(f, "https://cdn/v/video.mp4?token=1")) is None


def test_quality_trust_and_lines(make_store):
    """可信度低的不覆盖高的；线路的画质变了，源上记各线路里最好的那份。"""

    async def run():
        db, store = await make_store()
        jable = await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="t",
                                                             stream_url="https://cdn/a.m3u8"), "abc-001", store.current.site_rank)
        sid = (await db.get_sources(jable))[0]["id"]
        for q in (Quality([1080], "claimed"), Quality([720], "estimate"), Quality([2160], "claimed")):
            await db.set_quality(sid, None, q)
        src = await db.get_source(sid)
        assert (src["height"], src["quality_src"]) == (720, "estimate") and src["quality_at"]
        await db.set_quality(sid, None, Quality([720, 480], "master"))
        assert (await db.get_source(sid))["heights"] == "720,480"

        d = SourceDetail(key="1", code="ABC-001", title="[4K]ABC-001", lines=[("EVS", "x"), ("VOE", "y")],
                         claimed_height=2160)
        await db.upsert_detail("supjav", d, "abc-001", store.current.site_rank)
        sup = await db.find_source("supjav", "1")
        assert (sup["height"], sup["quality_src"]) == (2160, "claimed")
        evs, voe = await db.get_lines(sup["id"])
        await db.set_quality(sup["id"], voe["id"], Quality([720], "master"))
        await db.set_quality(sup["id"], evs["id"], Quality([1080, 720, 480], "embed"))
        sup = await db.get_source(sup["id"])
        assert (sup["height"], sup["heights"], sup["quality_src"]) == (1080, "1080,720,480", "embed")
        await db.set_quality(sup["id"], voe["id"], None)  # 探测过没认出来：只记时间
        assert (await db.get_lines(sup["id"]))[1]["height"] == 720
        await db.close()

    asyncio.run(run())


def _src(site: str, height: int | None, **kw) -> dict:
    return {"id": hash(site) % 1000, "site": site, "key": "k", "status": "active", "subtitle": "", "height": height,
            "stream_url": "", "stream_expires": None, "line": "", "fail_streak": 0, "last_fail_at": None, **kw}


def test_rank_by_quality_settings(make_store):
    """画质优先、画质上限、未知画质、直连优先、关掉画质优先回到站点优先顺序；线路也一样。"""

    async def run():
        db, store = await make_store()
        await store.update({"sites": {"supjav": {"enabled": True}}})
        r = Resolver(db, None, store, Metrics())
        sources = [_src("supjav", 480), _src("jable", None), _src("missav", 1080)]  # Jable 不知道画质，按 720 算

        def order(**kw):
            return [x["site"] for x in r.rank(sources, **kw)]

        assert order() == ["missav", "jable", "supjav"]
        await store.update({"quality_max": 720})
        assert order() == ["jable", "supjav", "missav"]  # 超过上限的排后面
        await store.update({"quality_max": 0, "quality_unknown": 360})
        assert order() == ["missav", "supjav", "jable"]
        await store.update({"quality_unknown": 720, "prefer_direct": True})
        assert order() == ["jable", "supjav", "missav"]  # MissAV 只能中转
        await store.update({"prefer_direct": False, "quality_first": False})
        assert order() == ["jable", "missav", "supjav"]  # 站点优先顺序

        await store.update({"quality_first": True})
        site = SITES["supjav"]
        lines = [{"id": i, "line": name, "host": host, "height": h, "fail_streak": 0, "last_fail_at": None}
                 for i, (name, host, h) in enumerate([("ST", "streamtape", None), ("VOE", "voe", 720),
                                                      ("EVS", "vidhide", 1080)])]
        assert [ln["line"] for ln in r.rank_lines(site, lines)] == ["EVS", "ST", "VOE"]
        await store.update({"prefer_direct": True})
        # 给网关（外网客户端）：绑出口 IP 的线路不算能直连
        assert [ln["line"] for ln in r.rank_lines(site, lines, remote=True)] == ["ST", "EVS", "VOE"]
        await db.close()

    asyncio.run(run())


def test_capture_on_detail_and_quality_job(make_store, boot):
    """抓详情时顺手探测（只请求 CDN）；没探测到的用「画质探测」任务补，探测过的不再排。"""

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1))  # 抓详情
        await asyncio.gather(*engine.resolver.quality._tasks)
        sources = [s for v in ids for s in await db.get_sources((await db.get_video(v))["id"])]
        # 假 CDN：两个分片 3600 秒、1800 秒，各 450 MB，平均约 1.5 Mbps → 估计 720p
        assert {(s["height"], s["quality_src"]) for s in sources} == {(720, "estimate")}

        await store.update({"quality_capture": False})
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False))
        with pytest.raises(ValueError, match="没有要探测画质的源"):
            await engine.create_quality(1)
        await db._write("UPDATE sources SET height=NULL, heights='', quality_src='', quality_at=NULL")
        job = await wait_job(db, await engine.create_quality(1))
        assert await db.task_counts(job["id"]) == {"done": 24}
        sources = [await db.get_source(s["id"]) for s in sources]
        assert {(s["height"], s["quality_src"]) for s in sources} == {(720, "estimate")}
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_resolve_modes_for_gateway(tmp_path):
    """auto 挑中能直连的给 CDN 地址；proxy 一律给中转地址；直连的源都失败时有中转地址就给它，不报 502。"""
    app = create_app(BootConfig(data_dir=tmp_path, default_public_base_url="http://hls2strm:8080"))
    try:
        with TestClient(app) as c:
            html, ids = model_fixture()
            fake = FakeFetcher(html, ids)
            app.state.ctx.fetcher = fake
            app.state.ctx.resolver.fetcher = fake
            app.state.ctx.resolver.quality.fetcher = fake
            h = {"Authorization": "Bearer tk"}
            assert c.put("/api/settings", json={"resolve_token": "tk"}).status_code == 200
            assert c.get("/api/resolve/ipzz-983", headers=h, params={"mode": "proxy"}).status_code == 409
            assert c.get("/api/resolve/ipzz-983", headers=h, params={"mode": "x"}).status_code == 400

            assert c.put("/api/settings", json={"resolve_proxy_url": "https://pub.example/"}).status_code == 200
            relay = "https://pub.example/play/ipzz-983.m3u8?proxy=1"
            assert c.get("/api/resolve/ipzz-983", headers=h).json()["url"].endswith("/62384.m3u8")  # auto：Jable 能直连
            assert c.get("/api/resolve/ipzz-983", headers=h, params={"mode": "proxy"}).json()["url"] == relay

            # 地址过期、详情页又抓不到：直连的源取地址失败，给中转地址（网关回退会把内网地址给外网客户端）
            db = app.state.ctx.db
            fake.fail["/videos/"] = FetchError("boom")

            async def expire():
                await db._write("UPDATE sources SET stream_expires=1, fail_streak=0")

            c.portal.call(expire)
            for mode in ("auto", "redirect"):
                r = c.get("/api/resolve/ipzz-983", headers=h, params={"mode": mode})
                assert r.status_code == 200 and r.json()["url"] == relay
            assert c.put("/api/settings", json={"resolve_proxy_url": ""}).status_code == 200
            assert c.get("/api/resolve/ipzz-983", headers=h).status_code == 502
    finally:
        for hd in logging.getLogger().handlers:
            hd.close()
        logging.getLogger().handlers.clear()
