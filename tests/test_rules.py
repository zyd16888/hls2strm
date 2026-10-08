import asyncio

import pytest

from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.rules import describe_rule, match_rule, normalize_rule
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job

VIDEO = {
    "code": "SONE-001", "title": "SONE-001 タイトル", "quality": "中文字幕",
    "categories": [{"slug": "chinese-subtitle", "name": "中文字幕"}],
    "tags": [{"slug": "big-tits", "name": "巨乳"}],
    "models": [{"id": "abc123", "name": "三上悠亜"}],
}


def test_normalize_and_match():
    assert normalize_rule(None) is None
    assert normalize_rule({"categories": " , ", "match": "any"}) is None
    with pytest.raises(ValueError):
        normalize_rule({"tags": "x", "match": "both"})

    rule = normalize_rule({"categories": "中文字幕，chinese-subtitle", "tags": ["巨乳", "巨乳"], "match": "all"})
    assert rule["categories"] == ["中文字幕", "chinese-subtitle"] and rule["tags"] == ["巨乳"]
    assert match_rule(rule, VIDEO)
    assert not match_rule({**rule, "tags": ["人妻"]}, VIDEO)            # all：每组都要满足
    assert match_rule({**rule, "tags": ["人妻"], "match": "any"}, VIDEO)  # any：满足一组即可
    assert match_rule(normalize_rule({"models": "abc123"}), VIDEO)
    assert match_rule(normalize_rule({"keywords": "sone-"}), VIDEO)
    assert match_rule(normalize_rule({"quality": "中文"}), VIDEO)
    assert not match_rule(normalize_rule({"categories": "中文字幕"}), {**VIDEO, "categories": []})
    assert describe_rule(rule) == "分类：中文字幕、chinese-subtitle 且 标签：巨乳"


def test_rule_library(make_store, boot):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = Engine(db, FakeFetcher(html, ids), OutputWriter(store), store, Metrics(), boot)
        await engine.start()

        # 样本详情的分类里有「凌辱快感」(insult)
        r = await engine.create_library("凌辱", "凌辱", rule={"categories": "insult"})
        await wait_job(db, r["reclassify_job_id"])
        lib_id = r["id"]
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1))
        lib_dir = store.output_dir / "凌辱"
        assert len(list(lib_dir.glob("*/*.strm"))) == 24 and len(list(lib_dir.glob("*/*.nfo"))) == 24

        # 其中一部是任务加入的：改规则后只移除规则加入的
        keep_slug = sorted(ids)[0]
        keep = await db.get_video(keep_slug)
        await db._write("UPDATE outputs SET via='job' WHERE video_id=? AND library_id=?", (keep["id"], lib_id))
        jobs = await engine.update_library(lib_id, "凌辱", "凌辱", rule={"tags": "不存在的标签"})
        assert jobs["rewrite_job_id"] is None
        job = await wait_job(db, jobs["reclassify_job_id"])
        assert job["state"] == {"checked": 24, "added": 0, "removed": 23}
        assert [p.parent.name for p in lib_dir.glob("*/*.strm")] == [keep_slug.upper()]

        # 去掉规则：不再自动归库，已有输出不动
        jobs = await engine.update_library(lib_id, "凌辱", "凌辱", rule=None)
        assert (await wait_job(db, jobs["reclassify_job_id"]))["state"]["removed"] == 0
        await engine.stop()

    asyncio.run(run())
