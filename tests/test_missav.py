import asyncio

from hls2strm.engine import Engine
from hls2strm.fetcher import NotFound
from hls2strm.observability import Metrics
from hls2strm.play import Resolver, _decode_x, _upstream, Resolved, rewrite_playlist
from hls2strm.sites import SITES
from hls2strm.sites.missav import split_variant
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher, fixture
from .test_engine import model_fixture, wait_job

PLAYLIST = "https://surrit.com/169a8267-e9a6-48c8-8142-26fd452092d2/playlist.m3u8"
MISSAV = SITES["missav"]


def test_parse_detail_and_list():
    d = MISSAV.parse_detail(fixture("missav_detail.html"), "ipzz-983")
    assert d.stream_url == PLAYLIST and d.stream_expires is None
    assert (d.code, d.subtitle, d.uncensored) == ("IPZZ-983", "", False)
    assert d.duration == 7189 and d.release_date == "2026-10-02"  # 发行日期，不是 og 里的上架日期
    assert d.maker == "IdeaPocket" and d.models[0]["name"] == "瀬緒凛"  # 括号里的原名
    assert d.cover_url == "https://fourhoi.com/ipzz-983/cover-n.jpg"

    # 中字变体页：番号字段也带后缀，要去掉才能和其他站对上
    d = MISSAV.parse_detail(fixture("missav_detail_zh.html"), "cjod-538-chinese-subtitle")
    assert (d.code, d.subtitle, d.uncensored) == ("CJOD-538", "zh", False)

    lp = MISSAV.parse_list(fixture("missav_list.html"))
    assert len(lp.items) == 12 and lp.last_page == 2000
    it = lp.items[0]
    assert (it.key, it.code, it.subtitle) == ("start-599-chinese-subtitle", "START-599", "zh") and it.duration == 8076


def test_keys_and_sources():
    assert split_variant("ssis-001-uncensored-leak") == ("ssis-001", "", True)
    assert split_variant("ssis-001-chinese-subtitle") == ("ssis-001", "zh", False)
    assert MISSAV.key_for("SSIS-001") == "ssis-001" and MISSAV.key_for("FC2PPV-491887") == "fc2-ppv-491887"
    assert MISSAV.key_for("SSIS-001", uncensored=True) == "ssis-001-uncensored-leak"
    assert MISSAV.key_from_url("https://missav.ws/dm44/ssis-001") == "ssis-001"
    assert MISSAV.key_from_url("https://missav123.com/cn/ssis-001-chinese-subtitle") == "ssis-001-chinese-subtitle"
    assert MISSAV.normalize_source("https://missav.ws/dm514/cn/genres/巨乳?page=2") == "/cn/genres/%E5%B7%A8%E4%B9%B3"
    assert MISSAV.normalize_source("new") == "/cn/new"
    assert MISSAV.page_url("/cn/new", 3, "released_at") == "/cn/new?page=3&sort=released_at"


def test_rewrite_nested_playlists():
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n640x360/video.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720\n1280x720/video.m3u8\n")
    root = PLAYLIST.rsplit("/", 1)[0] + "/"
    out = rewrite_playlist(master, PLAYLIST, root, "../hls/7/", "?t=k", True)
    assert "../hls/7/1280x720/video.m3u8?t=k" in out  # 子清单不改名
    sub_url = root + "1280x720/video.m3u8"
    sub = "#EXTM3U\n#EXTINF:4,\nvideo0.jpeg\n#EXTINF:4,\nhttps://other.cdn/x/seg1.ts?sig=1\n"
    out = rewrite_playlist(sub, sub_url, root, "../", "", True)
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert lines[0] == "../1280x720/video0.jpeg.ts"  # 伪装成图片的分片改名 .ts
    assert lines[1].startswith("../_x/")  # 不在源目录下的地址：签名后中转
    signed = lines[1][3:]
    assert signed.endswith(".ts")  # 带扩展名，新版 ffmpeg 才肯收
    assert _decode_x(signed) == "https://other.cdn/x/seg1.ts?sig=1"
    assert _decode_x(signed[:-5] + "AA.ts") is None  # 篡改后签名不对
    # 带查询串的子清单：加 .m3u8
    out = rewrite_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nindex-v1.m3u8?t=x&s=1\n", PLAYLIST, root, "../hls/7/",
                           "", False)
    ref = [ln for ln in out.splitlines() if ln and not ln.startswith("#")][0]
    assert ref.startswith("../hls/7/_x/") and ref.endswith(".m3u8") and _decode_x(ref[9:]) == root + "index-v1.m3u8?t=x&s=1"

    r = Resolved({}, {"stream_url": PLAYLIST, "stream_expires": None, "id": 7}, MISSAV)
    assert _upstream(r, "1280x720/video0.jpeg.ts") == root + "1280x720/video0.jpeg"
    assert _upstream(r, "1280x720/video.m3u8") == root + "1280x720/video.m3u8"


def _setup(fetcher: FakeFetcher, missav_keys: dict[str, str]) -> None:
    fetcher.pages["missav"] = {f"/cn/{k}": fixture(name) for k, name in missav_keys.items()}


def test_probe_failover_and_discover(make_store, boot):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        ids["ipzz-983"] = 62384
        fetcher = FakeFetcher(html, ids)
        _setup(fetcher, {"ipzz-983": "missav_detail.html"})
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        resolver = Resolver(db, fetcher, store, Metrics())

        # Jable 详情入库（中字），然后到 MissAV 补源
        v = await engine.fetch_detail("jable", "ipzz-983", library_id=1)
        job = await wait_job(db, await engine.create_probe("missav"))
        assert (await db.task_counts(job["id"]))["done"] >= 1
        srcs = {s["site"]: s for s in await db.get_sources(v["id"])}
        assert srcs["missav"]["stream_url"] == PLAYLIST and srcs["missav"]["subtitle"] == ""
        v = await db.get_video_by_id(v["id"])
        assert v["maker"] == "IdeaPocket"  # MissAV 补上了 Jable 没有的字段
        # 其他作品在 MissAV 上没有：记下查过，下次补源跳过
        assert not await db.works_to_probe("missav", 0)

        # 挑源：字幕相同（样本是高清原片），按站点优先级 Jable 在前
        ranked = resolver.rank(await db.get_sources(v["id"]))
        assert [s["site"] for s in ranked] == ["jable", "missav"]

        # Jable 源下架：自动换 MissAV，并把 Jable 源标为下架
        await db.update_source(srcs["jable"]["id"], stream_expires=0)
        fetcher.fail = {"/videos/ipzz-983/": NotFound("x")}
        r = await resolver.resolve("ipzz-983")
        assert r.site.name == "missav" and r.url == PLAYLIST
        assert (await db.find_source("jable", "ipzz-983"))["status"] == "gone"
        assert (await db.get_video("ipzz-983"))["status"] == "active"
        # 网关只要能直连的源：只剩 MissAV（要中转）时报 NoDirectSource
        from hls2strm.play import NoDirectSource
        try:
            await resolver.resolve("ipzz-983", direct_only=True)
            raise AssertionError("应当没有能直连的源")
        except NoDirectSource:
            pass

        # 字幕不回退：中字源（Jable）还在冷却中时，不用无字幕的 MissAV
        await db._write("UPDATE sources SET status='active', subtitle='zh' WHERE site='jable'")
        await db.source_failed(srcs["jable"]["id"], "boom")
        await store.update({"subtitle_fallback": False})
        assert [s["site"] for s in resolver.rank(await db.get_sources(v["id"]))] == ["jable"]
        await store.update({"subtitle_fallback": True})

        # 现场找源：只有 Jable 源的作品，Jable 下架时到 MissAV 按番号找
        other = next(s for s in ids if s != "ipzz-983")
        await engine.fetch_detail("jable", other)
        fetcher.pages["missav"][f"/cn/{other}"] = fixture("missav_detail.html").replace("IPZZ-983", other.upper())
        fetcher.fail = {f"/videos/{other}/": NotFound("x")}
        w = await db.get_video(other)
        await db.update_source((await db.get_sources(w["id"]))[0]["id"], stream_expires=0)
        await db._write("DELETE FROM source_checks WHERE video_id=?", (w["id"],))
        r = await resolver.resolve(other)
        assert r.site.name == "missav" and (await db.get_source_check(w["id"], "missav"))["found"] == 1
        await engine.stop()
        await db.close()

    asyncio.run(run())
