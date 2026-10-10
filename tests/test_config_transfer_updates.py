"""Existing-library imports must retain identity, progress and organized outputs."""

import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from hls2strm.config_transfer import ImportRequest, apply_config, export_config, preview_config
from hls2strm.sites import SITES, SourceItem

from .test_config_transfer import engine_for


async def existing_instance(db, store, boot):
    root, matched = store.output_dir, boot.data_dir / "matched"
    await db.update_library(1, dir=str(root / "all"))
    unused = await db.create_library("removed", "removed")
    await db.delete_library(unused)
    chinese = await db.create_library("中文字幕", str(root / "chinese-subtitle"),
                                      external_dir=str(matched / "chinese_subtitle"))
    native = await db.create_library("無碼解放", str(root / "uncensored"),
                                     external_dir=str(matched / "uncensored"))
    other = await db.create_library("asia", str(root / "asia"), external_dir=str(matched / "asia"),
                                    sources=[1], excludes=[chinese, native])
    await db.update_subscription(1, enabled=0, initialized=1, interval=1080)
    await db.create_subscription(name="無碼解放", source="/categories/uncensored/", sort="post_date",
                                 library_id=native, interval=720, enabled=0, initialized=1)
    history = await db.create_job("crawl", "历史抓取", {}, status="done")
    sub_id = await db.create_subscription(name="中文字幕", source="/categories/chinese-subtitle/",
        sort="post_date", library_id=chinese, detail=0, interval=180, enabled=1, initialized=1,
        last_run_at=123, last_job_id=history)
    # Force AUTOINCREMENT IDs to differ from preview's max(id)+1 placeholders.
    unused = await db.create_library("removed-again", "removed-again")
    await db.delete_library(unused)
    engine = engine_for(db, store, boot)
    await engine.reload_libraries()
    data = json.loads(Path("docs/mdcng-recommended.json").read_text(encoding="utf-8"))
    for lib in data["libraries"]:
        lib["external_dir"] = str(matched / Path(lib["external_dir"]).name)
    return engine, data, {"chinese": chinese, "native_uncensored": native, "other": other}, sub_id


def test_update_preview_commit_preserves_live_ids_history_and_source_outputs(make_store, boot):
    async def run():
        db, store = await make_store()
        engine, data, ids, sub_id = await existing_instance(db, store, boot)
        video, _ = await engine.upsert_item(SITES["jable"], SourceItem(key="miab-576", code="MIAB-576", title="MIAB-576"))
        files = []
        for lid in (1, ids["other"]):
            lib = engine.libs[lid]
            root = engine.writer.external_root(lib) or engine.writer.library_root(lib)
            path = root / "MIAB" / "MIAB-576" / "MIAB-576.strm"
            path.parent.mkdir(parents=True)
            path.write_text("http://example.test/play/miab-576.m3u8\n", encoding="utf-8")
            files.append(path)
            await db.ensure_output(video["id"], lid, via="source" if lid == ids["other"] else "job")
            await db.set_output(video["id"], lid, str(path), cover_done=True)
        before = await export_config(db)
        old_sub = await db.get_subscription(sub_id)
        old_output = await db.get_output(video["id"], ids["other"])
        old_jobs = await db.list_jobs()
        request = ImportRequest(config=data, update_existing=True, activate_subscriptions=True)
        plan = await preview_config(engine, request)
        assert await export_config(db) == before
        assert all(path.is_file() for path in files)
        plans = {lib["key"]: lib for lib in plan["libraries"]}
        assert plans["chinese"]["action"] == "reuse"
        assert plans["other"]["id"] == ids["other"]
        assert plans["other"]["action"] == "update"
        assert plans["other"]["sources"] == [1]
        assert plans["other"]["preserved_source_names"] == ["全部"]
        assert {c["field"] for c in plans["other"]["changes"]} == {"name", "excludes"}
        subscription = next(s for s in plan["subscriptions"] if s["name"] == "中字 · Jable")
        assert subscription["id"] == sub_id and subscription["action"] == "update"
        assert subscription["enabled"]  # file's disabled state cannot switch off an existing subscription.
        assert {c["field"] for c in subscription["changes"]} >= {"name", "interval", "cron"}
        request.preview_token = plan["preview_token"]
        result = await apply_config(engine, request)
        assert result == {"libraries_created": 2, "libraries_updated": 2,
                          "subscriptions_created": 35, "subscriptions_updated": 1}
        libs = {lib["name"]: lib for lib in await db.list_libraries()}
        assert libs["其他"]["id"] == ids["other"] and libs["其他"]["sources"] == [1]
        assert libs["原生无码"]["id"] == ids["native_uncensored"]
        assert libs["其他"]["excludes"] == sorted(libs[n]["id"] for n in
                                              ("中文字幕", "FC2", "无码破解与流出", "原生无码"))
        assert libs["FC2"]["id"] > max(ids.values()) + 1
        updated = await db.get_subscription(sub_id)
        for key in ("id", "created_at", "enabled", "initialized", "last_run_at", "last_job_id"):
            assert updated[key] == old_sub[key]
        assert updated["cron"] == "10 0 * * *" and updated["interval"] == 0
        assert await db.list_jobs() == old_jobs
        assert await db.get_output(video["id"], ids["other"]) == old_output
        await engine.settle()
        assert all(path.is_file() for path in files)
        assert await db.get_output(video["id"], ids["other"]) == old_output
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        assert await apply_config(engine, request) == {
            "libraries_created": 0, "libraries_updated": 0,
            "subscriptions_created": 0, "subscriptions_updated": 0}
    asyncio.run(run())


def test_updates_require_opt_in_fresh_preview_and_preserve_on_rollback(make_store, boot):
    async def run():
        db, store = await make_store()
        engine, data, ids, sub_id = await existing_instance(db, store, boot)
        before = await export_config(db)
        with pytest.raises(ValueError):
            await preview_config(engine, ImportRequest(config=data))
        request = ImportRequest(config=data, update_existing=True)
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        # A changed progress state must invalidate confirmation even when the JSON is unchanged.
        await db.update_subscription(sub_id, initialized=0)
        with pytest.raises(ValueError, match="重新预览"):
            await apply_config(engine, request)
        await db.update_subscription(sub_id, initialized=1)
        assert await export_config(db) == before
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        await db._write("CREATE TRIGGER reject_update BEFORE UPDATE ON subscriptions "
                        "BEGIN SELECT RAISE(ABORT, 'rollback'); END")
        with pytest.raises(sqlite3.IntegrityError):
            await apply_config(engine, request)
        assert await export_config(db) == before
        assert engine.libs[ids["other"]]["name"] == "asia"
    asyncio.run(run())


@pytest.mark.parametrize("case", ["move_root", "move_external", "ambiguous_library", "duplicate_library", "cycle", "changed_source", "ambiguous_subscription", "duplicate_subscription"])
def test_unsafe_or_ambiguous_updates_do_not_write(make_store, boot, case):
    async def run():
        db, store = await make_store()
        engine, data, ids, sub_id = await existing_instance(db, store, boot)
        if case == "move_root":
            data["libraries"][0]["dir"] = "different-inbox"
        if case == "move_external":
            data["libraries"][0]["external_dir"] = "different-target"
        if case == "ambiguous_library":
            data["libraries"][0]["name"] = "無碼解放"
        if case == "duplicate_library":
            duplicate = copy.deepcopy(data["libraries"][0])
            duplicate.update(key="duplicate", name="another-name")
            data["libraries"].append(duplicate)
        if case == "cycle":
            data["libraries"][0]["sources"] = ["other"]
        if case == "changed_source":
            data["subscriptions"][0].update(name="中文字幕", source="/categories/uncensored/")
        if case == "ambiguous_subscription":
            await db.create_subscription(name="duplicate", source="/categories/chinese-subtitle/",
                                         sort="post_date", library_id=ids["chinese"])
        if case == "duplicate_subscription":
            duplicate = copy.deepcopy(data["subscriptions"][0])
            duplicate.update(name="different-sort", sort="")
            data["subscriptions"].append(duplicate)
        before = await export_config(db)
        with pytest.raises(ValueError):
            await preview_config(engine, ImportRequest(config=data, update_existing=True))
        assert await export_config(db) == before
    asyncio.run(run())


def test_update_option_is_bound_to_confirmation_token(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        request = ImportRequest(config=await export_config(db))
        before = await export_config(db)
        request.preview_token = (await preview_config(engine, request))["preview_token"]
        request.update_existing = True
        with pytest.raises(ValueError, match="重新预览"):
            await apply_config(engine, request)
        assert await export_config(db) == before
    asyncio.run(run())
