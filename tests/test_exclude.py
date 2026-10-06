import asyncio

import pytest

from jable_strm.engine import Engine
from jable_strm.observability import Metrics
from jable_strm.writer import OutputWriter

from .conftest import FakeFetcher, fixture
from .test_engine import model_fixture, wait_job


async def lib_info(db, lib_id):
    return next(lib for lib in await db.list_libraries() if lib["id"] == lib_id)


def strm_names(d):
    return {p.parent.name for p in d.rglob("*.strm")}


def test_source_and_exclude_libraries(make_store, boot):
    """「其他」= 全部 − 中文字幕 − 无码（无码也排除中文字幕，中字优先），每部影片只在一个库里。"""
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        out = store.output_dir
        cn = (await engine.create_library("中文字幕", "中字"))["id"]
        unc = (await engine.create_library("无码", "无码", excludes=[cn]))["id"]
        r = await engine.create_library("其他", "其他", sources=[1], excludes=[cn, unc])
        other = r["id"]
        await wait_job(db, r["reclassify_job_id"])

        with pytest.raises(ValueError, match="既是来源库又是排除库"):
            await engine.create_library("x", "x", sources=[cn], excludes=[cn])
        with pytest.raises(ValueError, match="循环引用"):
            await engine.update_library(cn, "中文字幕", "中字", excludes=[other])
        with pytest.raises(ValueError, match="不能选自己"):
            await engine.update_library(other, "其他", "其他", sources=[1], excludes=[other])
        with pytest.raises(ValueError, match="作为来源库或排除库"):
            await engine.delete_library(cn, delete_files=False)

        # 中文字幕的定时订阅：库里已有数据，跳过首轮
        sub = await db.create_subscription(name="中字", source="/categories/chinese-subtitle/", sort="post_date",
                                           library_id=cn, detail=0, interval=60, stop_after_known=48, max_pages=20,
                                           initialized=1)
        await asyncio.sleep(1.1)  # 影片首次出现晚于订阅创建

        # 全站抓到 24 部进「全部」：中文字幕订阅还没在它们出现之后跑过，「其他」先不写
        await wait_job(db, await engine.create_crawl("/latest-updates/", end_page=1, detail=False))
        assert len(strm_names(out / "全部")) == 24
        assert (await engine.settle())["pulled"] == 24
        assert (await lib_info(db, other))["pending"] == 24 and not strm_names(out / "其他")

        # 中文字幕订阅跑完一轮（列表里有 IPZZ-983）：剩下 23 部写进「其他」
        await asyncio.sleep(1.1)
        fetcher.list_html = fixture("list_category_full.html")
        await wait_job(db, await engine.run_subscription(sub, "incremental"))
        assert (await engine.settle())["written"] == 23
        assert "IPZZ-983" in strm_names(out / "中字")
        assert len(strm_names(out / "其他")) == 23 and "IPZZ-983" not in strm_names(out / "其他")

        # 无码抓到同一批：IPZZ-983 已是中字不收；其余 23 部归无码，并从「其他」移除
        fetcher.list_html = html
        await wait_job(db, await engine.create_crawl("/categories/uncensored/", end_page=1, detail=False,
                                                     library_id=unc))
        assert not strm_names(out / "无码")  # 有排除库：等归并
        r = await engine.settle()
        assert (r["removed"], r["written"]) == (23, 23)
        assert len(strm_names(out / "无码")) == 23 and "IPZZ-983" not in strm_names(out / "无码")
        assert not strm_names(out / "其他")
        assert len(strm_names(out / "全部")) == 24  # 「全部」不受影响

        # 任务直接输出到「其他」：同样要等中文字幕订阅在它们出现之后跑完一轮
        fetcher.list_html = fixture("list_search_full.html")
        await wait_job(db, await engine.create_crawl("/search/ipzz/", end_page=1, detail=False, library_id=other))
        await engine.settle()
        assert not strm_names(out / "其他") and (await lib_info(db, other))["pending"] == 24
        await asyncio.sleep(1.1)
        fetcher.list_html = fixture("list_category_full.html")
        await wait_job(db, await engine.run_subscription(sub, "incremental"))
        assert (await engine.settle())["written"] == 24
        assert len(strm_names(out / "其他")) == 24

        # 排除库的订阅还没跑完首轮全量：什么都确认不了
        await db.update_subscription(sub, initialized=0)
        assert (await engine._subscription_cutoffs())[cn] == 0
        await engine.stop()

    asyncio.run(run())
