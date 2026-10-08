"""影片库筛选、排序、候选搜索、批量操作，以及前端页面的提供方式。"""

import asyncio
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from hls2strm import app as app_module
from hls2strm.config import BootConfig
from hls2strm.db import VideoQuery
from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.sites import SourceDetail, SourceItem
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job


def test_video_filters_sort_and_facets(make_store):
    async def run():
        db, store = await make_store()
        rank = {"jable": 0, "missav": 1}.get

        def detail(key, code, **kw):
            return SourceDetail(key=key, code=code, title=code, stream_url="https://cdn/" + key, **kw)

        await db.upsert_detail("jable", detail("aaa-001", "AAA-001", release_date="2024-01-01", duration=3600, maker="S1",
                                               subtitle="zh", models=[{"id": "m1", "name": "演员一"}],
                                               categories=[{"slug": "c1", "name": "分类一"}]), "aaa-001", rank)
        await db.upsert_detail("missav", detail("aaa-001-chinese-subtitle", "AAA-001", subtitle="zh"), "aaa-001", rank)
        await db.upsert_detail("missav", detail("bbb-002", "BBB-002", release_date="2025-06-01", duration=7200, maker="IP",
                                                models=[{"id": "m2", "name": "演员二"}]), "bbb-002", rank)
        await db.upsert_item("jable", SourceItem(key="ccc-003", code="CCC-003", title="没抓详情"), "ccc-003", rank)

        async def slugs(**kw):
            items, _ = await db.search_videos(VideoQuery(**kw))
            return [v["slug"] for v in items]

        assert await slugs(has_site=["missav"]) == ["bbb-002", "aaa-001"]
        assert await slugs(lacks_site=["missav"]) == ["ccc-003"]
        assert await slugs(sources="multi") == ["aaa-001"]
        assert await slugs(subtitle="zh") == ["aaa-001"]
        assert await slugs(subtitle="none") == ["ccc-003", "bbb-002"]
        assert await slugs(models=["m2"]) == ["bbb-002"]
        assert await slugs(categories=["c1"], makers=["S1"]) == ["aaa-001"]
        assert await slugs(categories=["c1"], makers=["IP"]) == []  # 不同条件之间都要满足
        assert await slugs(release_from="2025-01-01") == ["bbb-002"]
        assert await slugs(duration_min=5000) == ["bbb-002"]
        # 按上市日期排：没有日期的不管正序倒序都在最后
        assert await slugs(sort="release", desc=False) == ["aaa-001", "bbb-002", "ccc-003"]
        assert await slugs(sort="release") == ["bbb-002", "aaa-001", "ccc-003"]
        assert [f["item"] for f in await db.facet("models", q="二")] == ["m2"]
        assert {f["item"] for f in await db.facet("makers")} == {"S1", "IP"}
        await db.close()

    asyncio.run(run())


def test_batch_probe_refresh_and_library_membership(make_store, boot):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        v = await engine.fetch_detail("jable", "ipzz-983", library_id=1)

        # 补源：每个启用、还没有源的站点各一个子任务
        job = await db.get_job(await engine.create_probe_videos([v["id"]]))
        enabled = [n for n, c in store.current.sites.items() if c.enabled and n != "jable"]
        assert job["kind"] == "probe" and sum((await db.task_counts(job["id"])).values()) == len(enabled)
        await wait_job(db, job["id"])

        # 刷新：按源排详情子任务，不改所在的库
        job = await wait_job(db, await engine.create_refresh([v["id"]]))
        assert (await db.task_counts(job["id"])) == {"done": 1}

        # 加入另一个库：写出 strm；再移出：文件删掉、记录去掉
        lib_id = (await engine.create_library("另一个", "other"))["id"]
        assert await engine.add_to_library([v["id"]], lib_id) == 1
        assert await engine.add_to_library([v["id"]], lib_id) == 0  # 已在库里
        out = await db.get_output(v["id"], lib_id)
        assert out["strm_path"] and Path(out["strm_path"]).exists()
        assert await engine.remove_from_library([v["id"]], lib_id) == 1
        assert await db.get_output(v["id"], lib_id) is None and not Path(out["strm_path"]).exists()
        assert await db.get_output(v["id"], 1) is not None  # 别的库不受影响
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_frontend_not_built_and_asset_cache(tmp_path, monkeypatch):
    static = tmp_path / "static"
    monkeypatch.setattr(app_module, "STATIC_DIR", static)
    app = app_module.create_app(BootConfig(data_dir=tmp_path / "data"))
    try:
        with TestClient(app) as c:
            r = c.get("/")
            assert r.status_code == 503 and "npm run build" in r.text
            (static / "assets").mkdir(parents=True)
            (static / "index.html").write_text("<!doctype html>", encoding="utf-8")
            (static / "assets" / "index-abc123.js").write_text("1", encoding="utf-8")
            r = c.get("/")
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
            r = c.get("/static/assets/index-abc123.js")
            assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    finally:
        for h in logging.getLogger().handlers:
            h.close()
        logging.getLogger().handlers.clear()
