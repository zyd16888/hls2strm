import asyncio

from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job


def test_list_errors_name_url_and_item(make_store, boot):
    """列表为空的报错带上请求的地址；某一部入库失败不连累同一页的其他影片，报错写明是哪一部。"""
    async def run():
        db, store = await make_store()
        await store.update({"max_attempts": 1})
        html, ids = model_fixture()
        fetcher = FakeFetcher("<html><body>没有影片</body></html>", ids)
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()

        job = await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False))
        task = (await db.list_tasks(job["id"], "failed"))[0]
        assert "列表为空" in task["last_error"] and "https://fs1.app/models/abc/" in task["last_error"]

        fetcher.list_html = html
        upsert = engine.upsert_item

        async def flaky(site, it, **kw):
            if it.key == "abf-156":
                raise RuntimeError("坏数据")
            return await upsert(site, it, **kw)

        engine.upsert_item = flaky
        job = await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False))
        task = (await db.list_tasks(job["id"], "failed"))[0]
        assert "abf-156（番号 ABF-156）" in task["last_error"] and "坏数据" in task["last_error"]
        assert len(list((store.output_dir / "全部").rglob("*.strm"))) == 23
        await engine.stop()

    asyncio.run(run())
