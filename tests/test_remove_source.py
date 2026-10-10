import asyncio

from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.queries import VideoQuery
from hls2strm.sites import SourceItem
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job


def test_removed_source_stays_removed(make_store, boot):
    """删掉播不了的源：影片没有别的源就移出所有输出库，订阅、详情再抓到也不回来；别的站有了新源会回来；
    恢复放回原来的库；还有别的源的影片留在库里。"""
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = Engine(db, FakeFetcher(html, ids), OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        out = store.output_dir
        fc2 = (await engine.create_library("FC2", "fc2"))["id"]
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False))
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=fc2))
        v = await db.get_video("ipzz-983")
        strm = [out / "全部" / "IPZZ-983" / "IPZZ-983.strm", out / "fc2" / "IPZZ-983" / "IPZZ-983.strm"]
        assert all(p.is_file() for p in strm)

        job = await wait_job(db, await engine.create_source_removal([v["id"]], ["jable"]))
        assert job["status"] == "done" and job["kind"] == "source_remove"
        v = await db.get_video_by_id(v["id"])
        assert v["status"] == "removed" and not any(p.exists() for p in strm)
        assert await db.get_outputs(v["id"]) == [] and [s["status"] for s in await db.get_sources(v["id"])] == ["removed"]
        items, total = await db.search_videos(VideoQuery(status="removed"))
        assert total == 1 and items[0]["id"] == v["id"]

        # 订阅再翻到、重抓详情：不复活、不写文件
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=fc2))
        await engine.fetch_detail("jable", "ipzz-983")
        assert (await db.get_video_by_id(v["id"]))["status"] == "removed" and not any(p.exists() for p in strm)
        assert await db.get_outputs(v["id"]) == []

        # 恢复：源可用，放回原来的两个库
        await wait_job(db, await engine.create_source_removal([v["id"]]))
        assert (await db.get_video_by_id(v["id"]))["status"] == "active" and all(p.is_file() for p in strm)
        assert {o["library_id"] for o in await db.get_outputs(v["id"])} == {1, fc2}

        # 还有别的源：只删这个站的，影片留在库里
        w = await db.get_video("abf-156")
        await db.upsert_item("missav", SourceItem(key="abf-156", code="ABF-156", title="ABF-156"), "abf-156",
                             engine._rank)
        await wait_job(db, await engine.create_source_removal([w["id"]], ["jable"]))
        assert (await db.get_video_by_id(w["id"]))["status"] == "active" and len(await db.get_outputs(w["id"])) == 2
        assert {s["site"]: s["status"] for s in await db.get_sources(w["id"])} == {"jable": "removed", "missav": "active"}

        # 删完没有源的，别的站以后抓到新源：回到可用，之后按订阅入库
        await wait_job(db, await engine.create_source_removal([v["id"]], ["jable"]))
        await db.upsert_item("missav", SourceItem(key="ipzz-983", code="IPZZ-983", title="IPZZ-983"), "ipzz-983",
                             engine._rank)
        v = await db.get_video_by_id(v["id"])
        assert v["status"] == "active" and not v["removed_from"]
        await engine.stop()

    asyncio.run(run())
