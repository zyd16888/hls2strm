import asyncio
import logging

from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.observability import Metrics
from hls2strm.play import Resolver
from hls2strm.quality import filter_master, parse_label, tier

from .conftest import FakeFetcher
from .test_engine import model_fixture
from .test_quality import MASTER, _src


def test_pick_tier_from_master():
    """只留一档：指定的档；没有就比它低里最高的；都比它高用最低的；不指定取最高。"""
    def kept(want):
        text, uri = filter_master(MASTER, want)
        return [ln for ln in text.splitlines() if ln and not ln.startswith("#")], uri

    assert kept(None) == (["720p/video.m3u8"], "720p/video.m3u8")
    assert kept(480) == (["480p/video.m3u8"], "480p/video.m3u8")
    assert kept(1080)[1] == "720p/video.m3u8" and kept(240)[1] == "360p/video.m3u8"
    assert filter_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nb.m3u8\n",
                         None) is None  # 没写分辨率：原样用
    assert filter_master("#EXTM3U\n#EXTINF:4,\na.ts\n", 720) is None
    assert [parse_label(x) for x in ("720p", "4k", "1080", "x")] == [720, 2160, 1080, None]
    assert tier(800) == 720 and tier(1076) == 1080


def test_rank_for_wanted_tier(make_store):
    async def run():
        db, store = await make_store()
        await store.update({"sites": {"supjav": {"enabled": True}}})
        r = Resolver(db, None, store, Metrics())
        sources = [_src("jable", 720, heights="720"), _src("missav", 720, heights="720,480,360"),
                   _src("supjav", 1080, heights="1080")]
        assert [x["site"] for x in r.rank(sources, want=480)][0] == "missav"
        assert [x["site"] for x in r.rank(sources, want=1080)][0] == "supjav"
        assert [x["site"] for x in r.rank(sources, want=720)][:2] == ["jable", "missav"]  # 都有 720：按站点顺序
        await db.close()

    asyncio.run(run())


def test_play_and_resolve_versions(tmp_path):
    """/play/{slug}@480p.m3u8：302 到那一档的子清单，中转时主播放列表只留那一档；不指定时只给最高档（可设成全给）。"""
    app = create_app(BootConfig(data_dir=tmp_path, default_public_base_url="http://hls2strm:8080"))
    try:
        with TestClient(app) as c:
            html, ids = model_fixture()
            fake = FakeFetcher(html, ids)
            ctx = app.state.ctx
            ctx.fetcher = ctx.resolver.fetcher = ctx.resolver.quality.fetcher = fake
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Infuse/7.8"}, follow_redirects=False)
            master_url = r.headers["location"]
            root = master_url.rsplit("/", 1)[0] + "/"
            fake.files[master_url] = MASTER.encode()  # 假装这个源是多码率的

            async def set_heights():
                await ctx.db._write("UPDATE sources SET heights='720,480,360', height=720, quality_src='master'")

            c.portal.call(set_heights)
            h = {"User-Agent": "Infuse/7.8"}
            assert c.get("/play/ipzz-983@480p.m3u8", headers=h, follow_redirects=False).headers["location"] == (
                root + "480p/video.m3u8")
            assert c.get("/play/ipzz-983.m3u8", headers=h, follow_redirects=False).headers["location"] == (
                root + "720p/video.m3u8")  # 只给最高档
            assert c.get("/play/ipzz-983@xyz.m3u8", headers=h).status_code == 404

            body = c.get("/play/ipzz-983@480p.m3u8", headers={"User-Agent": "Lavf/61"}).text  # 中转
            refs = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
            assert len(refs) == 1 and refs[0].endswith("/480p/video.m3u8")

            assert c.put("/api/settings", json={"variant_mode": "all"}).status_code == 200
            assert c.get("/play/ipzz-983.m3u8", headers=h, follow_redirects=False).headers["location"] == master_url

            assert c.put("/api/settings", json={"resolve_token": "tk", "resolve_proxy_url": "https://pub.example"}
                         ).status_code == 200
            auth = {"Authorization": "Bearer tk"}
            data = c.get("/api/resolve/play/ipzz-983@480p.m3u8", headers=auth).json()
            assert data["slug"] == "ipzz-983" and data["url"] == root + "480p/video.m3u8"
            data = c.get("/api/resolve/play/ipzz-983@480p.m3u8", headers=auth, params={"mode": "proxy"}).json()
            assert data["url"] == "https://pub.example/play/ipzz-983@480p.m3u8?proxy=1"
    finally:
        for hd in logging.getLogger().handlers:
            hd.close()
        logging.getLogger().handlers.clear()
