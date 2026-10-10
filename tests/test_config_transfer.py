import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from types import SimpleNamespace

from hls2strm.api import require_auth, router
from hls2strm.config_transfer import ImportRequest, apply_config, export_config, preview_config
from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher, fixture


def bundle():
    return {"format": "hls2strm-library-config", "version": 1,
            "libraries": [{"key": "main", "name": "MDCNG", "dir": "inbox", "external_dir": "organized"}],
            "subscriptions": [{"name": "Jable", "site": "jable", "source": "/latest-updates/",
                               "sort": "post_date", "library": "main", "enabled": True}]}


def engine_for(db, store, boot):
    return Engine(db, FakeFetcher(fixture("list_category_full.html"), {}), OutputWriter(store), store, Metrics(), boot)


def test_preview_confirm_roundtrip_and_stale(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        before = await export_config(db)
        request = ImportRequest(config=bundle())
        plan = await preview_config(engine, request)
        assert await export_config(db) == before
        assert engine.libs == {} and not store.output_dir.exists()
        assert not plan["subscriptions"][0]["enabled"]
        with pytest.raises(ValueError, match="重新预览"):
            await apply_config(engine, request)
        request.preview_token = plan["preview_token"]
        result = await apply_config(engine, request)
        assert result == {"libraries_created": 1, "subscriptions_created": 1,
                          "libraries_updated": 0, "subscriptions_updated": 0}
        assert not store.output_dir.exists()
        subs = await db.list_subscriptions()
        added = next(s for s in subs if s["name"] == "Jable")
        assert added["enabled"] == 0 and added["initialized"] == 0
        assert added["last_job_id"] is None
        assert (await db.list_jobs()) == []
        with pytest.raises(ValueError, match="重新预览"):
            await apply_config(engine, request)
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        assert await apply_config(engine, request) == {"libraries_created": 0, "subscriptions_created": 0,
                                                       "libraries_updated": 0, "subscriptions_updated": 0}
        exported = await export_config(db)
        assert "play_token" not in json.dumps(exported)
        restored = ImportRequest(config=exported)
        p = await preview_config(engine, restored)
        assert all(x["action"] == "reuse" for x in p["libraries"] + p["subscriptions"])
        restored.preview_token = p["preview_token"]
        assert (await apply_config(engine, restored))["subscriptions_created"] == 0
        assert await export_config(db) == exported
        # 确认前修改文件或环境，原来的预览凭证无效。
        request.config.subscriptions[0].interval += 1
        with pytest.raises(ValueError):
            await apply_config(engine, request)
        request = ImportRequest(config=bundle())
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        await store.update({"path_template": "{slug}/new-{slug}"})
        with pytest.raises(ValueError, match="重新预览"):
            await apply_config(engine, request)
    asyncio.run(run())


def test_forward_references_real_id_mapping_and_opt_in(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        # AUTOINCREMENT 序号不等于 max(id)+1，导入需要按实际插入 id 重映射。
        deleted = await db.create_library("deleted", "deleted")
        await db.delete_library(deleted)
        data = bundle()
        data["libraries"].insert(0, {"key": "other", "name": "Other", "dir": "other", "sources": ["main"]})
        request = ImportRequest(config=data, activate_subscriptions=True)
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        await apply_config(engine, request)
        libs = {x["name"]: x for x in await db.list_libraries()}
        assert libs["Other"]["sources"] == [libs["MDCNG"]["id"]]
        sub = next(x for x in await db.list_subscriptions() if x["name"] == "Jable")
        assert sub["enabled"] == 1 and sub["library_id"] == libs["MDCNG"]["id"]
        # 导出配置能在 ID 完全不同的新实例中恢复。
        from hls2strm.db import Database
        from hls2strm.config import SettingsStore
        other_db = Database(boot.data_dir / "other.db")
        await other_db.open()
        try:
            other_store = SettingsStore(boot, other_db)
            other_engine = engine_for(other_db, other_store, boot)
            request = ImportRequest(config=await export_config(db))
            request.preview_token = (await preview_config(other_engine, request))["preview_token"]
            await apply_config(other_engine, request)
            restored = {x["name"]: x for x in await other_db.list_libraries()}
            assert restored["Other"]["sources"] == [restored["MDCNG"]["id"]]
        finally:
            await other_db.close()
    asyncio.run(run())


def test_exported_subscription_requires_full_scan_only_on_new_instance(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        original = await db.create_subscription(
            name="Initialized", site="jable", source="/categories/chinese-subtitle/", sort="post_date",
            library_id=1, detail=0, enabled=1, initialized=1, last_run_at=123,
        )
        exported = await export_config(db)
        assert next(s for s in exported["subscriptions"] if s["name"] == "Initialized")["initial_full"] is True
        request = ImportRequest(config=exported, activate_subscriptions=True)
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        await apply_config(engine, request)
        reused = await db.get_subscription(original)
        assert reused["initialized"] == 1 and reused["last_run_at"] == 123

        from hls2strm.db import Database
        from hls2strm.config import SettingsStore
        fresh_db = Database(boot.data_dir / "fresh.db")
        await fresh_db.open()
        try:
            fresh_store = SettingsStore(boot, fresh_db)
            await fresh_store.load()
            fresh_engine = engine_for(fresh_db, fresh_store, boot)
            request.preview_token = (await preview_config(fresh_engine, request))["preview_token"]
            await apply_config(fresh_engine, request)
            imported = next(s for s in await fresh_db.list_subscriptions() if s["name"] == "Initialized")
            assert imported["enabled"] == 1 and imported["initialized"] == 0
            assert imported["last_run_at"] is None and imported["last_job_id"] is None
            # 启用也不能自动跳过首轮全量；手动 auto 首次运行应创建全量任务。
            await fresh_engine._run_due_subscriptions()
            assert await fresh_db.list_jobs() == []
            job_id = await fresh_engine.run_subscription(imported["id"])
            job = await fresh_db.get_job(job_id)
            assert job["kind"] == "crawl" and not job["params"].get("incremental")
        finally:
            await fresh_db.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["overlap", "cycle", "reference", "duplicate", "site", "sort", "placeholder", "conflict"])
def test_invalid_import_is_read_only(make_store, boot, case):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        data = bundle()
        lib = data["libraries"][0]
        if case == "overlap": lib["external_dir"] = "inbox/final"
        if case == "cycle": lib["excludes"] = ["main"]
        if case == "reference": lib["sources"] = ["missing"]
        if case == "duplicate": data["subscriptions"].append(copy.deepcopy(data["subscriptions"][0]))
        if case == "site": data["subscriptions"][0]["site"] = "missing"
        if case == "sort": data["subscriptions"][0]["sort"] = "invalid"
        if case == "placeholder": data["subscriptions"][0]["source"] = "/categories/<slug>/"
        if case == "conflict": lib["name"] = "全部"
        before = await export_config(db)
        with pytest.raises(ValueError):
            await preview_config(engine, ImportRequest(config=data))
        assert await export_config(db) == before and not store.output_dir.exists()
    asyncio.run(run())


def test_failure_rolls_back_whole_import(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        before = await export_config(db)
        request = ImportRequest(config=bundle())
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        await db._write("CREATE TRIGGER reject_import BEFORE INSERT ON subscriptions BEGIN SELECT RAISE(ABORT, 'test'); END")
        with pytest.raises(sqlite3.IntegrityError):
            await apply_config(engine, request)
        assert await export_config(db) == before
        assert engine.libs == {} and not store.output_dir.exists()
    asyncio.run(run())


def test_api_auth_validation_and_preview(make_store, boot):
    async def run():
        db, store = await make_store()
        app = FastAPI()
        engine = engine_for(db, store, boot)
        app.state.ctx = SimpleNamespace(db=db, engine=engine,
                                        auth=SimpleNamespace(required=True, verify=lambda cookie: False))
        app.include_router(router)
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/api/configuration/export")).status_code == 401
            assert (await client.post("/api/configuration/preview", json={"config": bundle()})).status_code == 401
            assert (await client.post("/api/configuration/import", json={"config": bundle()})).status_code == 401
            app.dependency_overrides[require_auth] = lambda: None
            assert (await client.get("/api/configuration/export")).status_code == 200
            request = {"config": bundle()}
            preview = await client.post("/api/configuration/preview", json=request)
            assert preview.status_code == 200
            assert (await client.post("/api/configuration/import", json=request)).status_code == 400
            request["preview_token"] = preview.json()["preview_token"]
            assert (await client.post("/api/configuration/import", json=request)).status_code == 200
            request["config"]["settings"] = {"proxy": "bad"}
            assert (await client.post("/api/configuration/preview", json=request)).status_code == 422
    asyncio.run(run())


def test_recommended_config_valid_and_five_exclusive_destinations(make_store, boot):
    async def run():
        db, store = await make_store()
        data = json.loads(Path("docs/mdcng-recommended.json").read_text(encoding="utf-8"))
        request = ImportRequest(config=data)
        engine = engine_for(db, store, boot)
        plan = await preview_config(engine, request)
        assert [lib["key"] for lib in plan["libraries"]] == ["chinese", "fc2", "decensored", "native_uncensored", "other"]
        for i, lib in enumerate(plan["libraries"]):
            assert lib["excludes"] == [previous["id"] for previous in plan["libraries"][:i]]
            assert not lib["sources"] and not lib["versions"] and lib["external_root"]
        assert len({s["library_key"] for s in plan["subscriptions"]}) == 5
        destinations = {(s["site"], s["source"]): s["library_key"] for s in plan["subscriptions"]}
        for site, native, changed in (
            ("missav", "/cn/heyzo", "/cn/uncensored-leak"),
            ("supjav", "/zh/category/uncensored-jav", "/zh/category/reducing-mosaic"),
            ("javmost", "/category/uncensor", "/tag/REDUCING"),
            ("123av", "/cn/uncensored", "/cn/uncensored-leaked"),
        ):
            assert destinations[site, native] == "native_uncensored"
            assert destinations[site, changed] == "decensored"
        assert destinations["javguru", "/category/decensored"] == "decensored"
        assert destinations["jable", "/categories/uncensored/"] == "other"
        assert not any(s["enabled"] for s in plan["subscriptions"])
        assert {s["site"] for s in plan["subscriptions"]} == {"jable", "missav", "supjav", "javguru", "javmost", "123av"}
        request.preview_token = plan["preview_token"]
        await apply_config(engine, request)
    asyncio.run(run())


def test_five_libraries_wait_for_classification_and_emit_one_strm(make_store, boot):
    """先抓最新、再发现交叉分类：分类完成前不向低优先级库送刮，完成后每个作品只有一份输出。"""
    from hls2strm.sites import SITES, SourceItem

    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        data = json.loads(Path("docs/mdcng-recommended.json").read_text(encoding="utf-8"))
        request = ImportRequest(config=data)
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        await apply_config(engine, request)
        libs = {lib["name"]: lib["id"] for lib in engine.libs.values()}
        classifiers = [s for s in await db.list_subscriptions()
                       if s["library_id"] in [libs[n] for n in ("中文字幕", "FC2", "无码破解与流出", "原生无码")]]
        for sub in classifiers:
            await db.update_subscription(sub["id"], enabled=1, initialized=0)

        cases = [
            ("fc2-ppv-100001", "FC2-PPV-100001", False, ["其他", "原生无码", "FC2", "中文字幕"], "中文字幕"),
            ("fc2-ppv-100002", "FC2-PPV-100002", False, ["其他", "原生无码", "FC2"], "FC2"),
            ("ssis-001-uncensored-leak", "SSIS-001", True, ["其他", "原生无码", "无码破解与流出"], "无码破解与流出"),
            ("ssis-002", "SSIS-002", False, ["其他"], "其他"),
            ("ssis-001", "SSIS-001", False, ["其他", "中文字幕"], "中文字幕"),
            ("heyzo-3991", "HEYZO-3991", False, ["其他", "原生无码"], "原生无码"),
            ("ssis-003-uncensored-leak", "SSIS-003", True, ["其他", "无码破解与流出", "中文字幕"], "中文字幕"),
        ]
        works = []
        for key, code, uncensored, memberships, expected in cases:
            video, _ = await engine.upsert_item(SITES["missav"], SourceItem(
                key=key, code=code, title=code, uncensored=uncensored))
            works.append((video, expected))
            await db._write("UPDATE videos SET created_at=100 WHERE id=?", (video["id"],))
            for name in memberships:
                await db.ensure_output(video["id"], libs[name])
                await engine._output_one(video, libs[name], cover=False)
        await engine.settle()
        assert len(list(store.output_dir.rglob("*.strm"))) == 3  # 仅无上级排除库的中字库可先写。
        for video, expected in works:
            if expected != "中文字幕":
                assert (await db.get_output(video["id"], libs[expected]))["strm_path"] == ""

        # 各分类完整扫描后释放其余四个库，不靠实际等待或网络请求。
        for sub in classifiers:
            job = await db.create_job("crawl", "分类首轮", {"subscription_id": sub["id"]}, status="done")
            await db.update_job(job, started_at=500, state={"list_complete": True})
            await db.update_subscription(sub["id"], initialized=1)
        await engine.settle()
        for video, expected in works:
            outputs = await db.get_outputs(video["id"])
            assert len(outputs) == 1 and outputs[0]["library_id"] == libs[expected]
            assert Path(outputs[0]["strm_path"]).is_file()
        files = {p: p.read_bytes() for p in store.output_dir.rglob("*.strm")}
        assert len(files) == len(works)

        # 别站不同 FC2 拼法仍是同一作品，重写不会多送一份。
        same, created = await engine.upsert_item(SITES["javmost"], SourceItem(
            key="FC2PPV-100002", code="FC2PPV-100002", title="FC2PPV-100002"))
        assert not created and same["id"] == works[1][0]["id"]
        await engine._output_one(same, libs["FC2"], cover=False)
        await engine.settle()
        assert {p: p.read_bytes() for p in store.output_dir.rglob("*.strm")} == files
    asyncio.run(run())
