import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from hls2strm.api import router
from hls2strm.config_transfer import ImportRequest, apply_config, export_config, preview_config
from hls2strm.db import Database, MIGRATIONS
from hls2strm.subscription_schedule import ScheduleFields, next_cron_at

from .test_config_transfer import bundle, engine_for


def timestamp(value, timezone="Asia/Shanghai"):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone)).timestamp())


def test_cron_timezone_and_twice_daily():
    base = timestamp("2026-10-10T00:00:00")
    assert next_cron_at("10 2 * * *", "Asia/Shanghai", base) == timestamp("2026-10-10T02:10:00")
    assert next_cron_at("10 2 * * *", "UTC", base) == timestamp("2026-10-10T02:10:00", "UTC")
    at = timestamp("2026-10-10T02:10:00")
    assert next_cron_at("10 2,18 * * *", "Asia/Shanghai", at) == timestamp("2026-10-10T18:10:00")
    assert ScheduleFields(cron=" 10  2 * * * ", interval=120).interval == 0
    assert ScheduleFields(cron="").interval == 0
    assert ScheduleFields(interval=1080).cron is None


@pytest.mark.parametrize("fields", [
    {"cron": "@daily"}, {"cron": "0 0 0 * * *"}, {"cron": "61 2 * * *"},
    {"cron": "0 2 31 2 *"}, {"cron": "not cron"}, {"timezone": "Mars/City"},
])
def test_invalid_schedule(fields):
    with pytest.raises(ValidationError):
        ScheduleFields(**fields)


def test_v13_migration_preserves_interval_progress_and_reopen(boot):
    async def run():
        path = boot.data_dir / "v13.db"
        async with aiosqlite.connect(path) as conn:
            for step in MIGRATIONS[:13]:
                await step(conn)
            await conn.execute("UPDATE subscriptions SET interval=1080, initialized=1, last_run_at=123")
            await conn.execute("PRAGMA user_version=13")
            await conn.commit()
        for _ in range(2):
            db = Database(path)
            await db.open()
            try:
                sub = (await db.list_subscriptions())[0]
                assert (sub["interval"], sub["initialized"], sub["last_run_at"]) == (1080, 1, 123)
                assert sub["cron"] is None and sub["timezone"] == "Asia/Shanghai"
                assert sub["schedule_updated_at"] == 0
            finally:
                await db.close()
    asyncio.run(run())


def test_due_cron_no_repeat_no_startup_burst_and_edit_anchor(make_store, boot, monkeypatch):
    clock = [timestamp("2026-10-10T00:09:00")]
    monkeypatch.setattr("hls2strm.engine.time.time", lambda: clock[0])

    async def run():
        db, store = await make_store()
        await db.update_subscription(1, enabled=0)
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        sub_id = await db.create_subscription(name="cron", source="/latest-updates/", sort="post_date",
            library_id=1, cron="10 0 * * *", interval=0, timezone="Asia/Shanghai", initialized=1)
        sub = next(s for s in await db.list_subscriptions() if s["id"] == sub_id)
        assert sub["next_run_at"] == timestamp("2026-10-10T00:10:00")
        await engine._run_due_subscriptions()
        assert await db.list_jobs() == []
        clock[0] = timestamp("2026-10-10T00:10:05")
        await engine._run_due_subscriptions()
        await engine._run_due_subscriptions()
        assert len(await db.list_jobs()) == 1
        sub = await db.get_subscription(sub_id)
        assert sub["last_run_at"] == clock[0]
        await db.update_job(sub["last_job_id"], status="done")
        clock[0] = timestamp("2026-10-10T00:11:00")
        await engine._run_due_subscriptions()
        assert len(await db.list_jobs()) == 1

        # 在下一日错过时刻后重启，不能立即补跑所有站点。
        clock[0] = timestamp("2026-10-11T12:00:00")
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        await engine._run_due_subscriptions()
        assert len(await db.list_jobs()) == 1
        sub = next(s for s in await db.list_subscriptions() if s["id"] == sub_id)
        assert sub["next_run_at"] == timestamp("2026-10-12T00:10:00")
        # 修改到今天已过的时间，也从下一时刻开始；读取和运行共用同一计算。
        await db.update_subscription(sub_id, cron="30 11 * * *")
        await engine._run_due_subscriptions()
        assert len(await db.list_jobs()) == 1
        sub = next(s for s in await db.list_subscriptions() if s["id"] == sub_id)
        assert sub["next_run_at"] == timestamp("2026-10-12T11:30:00")
        clock[0] = timestamp("2026-10-12T11:30:05")
        engine.paused = True
        await engine._run_due_subscriptions()
        assert len(await db.list_jobs()) == 1
        engine.paused = False
        await engine._run_due_subscriptions()
        assert len(await db.list_jobs()) == 2
    asyncio.run(run())


def test_manual_uninitialized_and_legacy_scheduling(make_store, boot, monkeypatch):
    clock = [timestamp("2026-10-10T01:00:00")]
    monkeypatch.setattr("hls2strm.engine.time.time", lambda: clock[0])

    async def run():
        db, store = await make_store()
        await db.update_subscription(1, enabled=0)
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        legacy = await db.create_subscription(name="legacy", source="/latest-updates/", sort="post_date",
            library_id=1, interval=1080, initialized=1, last_run_at=clock[0] - 1080 * 60)
        await db.create_subscription(name="manual", source="/latest-updates/", library_id=1,
                                     cron="", interval=0, initialized=1)
        await db.create_subscription(name="full", source="/latest-updates/", library_id=1,
                                     cron="* * * * *", interval=0, initialized=0)
        clock[0] += 120
        await engine._run_due_subscriptions()
        jobs = await db.list_jobs()
        assert len(jobs) == 1 and jobs[0]["params"]["subscription_id"] == legacy
        subs = {s["name"]: s for s in await db.list_subscriptions()}
        assert subs["manual"]["next_run_at"] is None and subs["full"]["next_run_at"] is None
    asyncio.run(run())


def test_schedule_api_and_import_roundtrip_are_read_only_until_confirm(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        app = FastAPI()
        app.state.ctx = SimpleNamespace(db=db, engine=engine, auth=SimpleNamespace(required=False))
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            before = await export_config(db)
            response = await client.post("/api/subscriptions/schedule-preview", json={"cron": "10 2 * * *", "timezone": "UTC"})
            assert response.status_code == 200 and response.json()["next_run_at"]
            assert before == await export_config(db) and await db.list_jobs() == []
            assert (await client.post("/api/subscriptions/schedule-preview", json={"cron": "bad"})).status_code == 422
            body = {"name": "API", "source": "/latest-updates/", "cron": "15 3,15 * * *", "initial_full": False}
            response = await client.post("/api/subscriptions", json=body)
            assert response.status_code == 200
            sub_id = response.json()["id"]
            sub = await db.get_subscription(sub_id)
            assert sub["cron"] == body["cron"] and sub["interval"] == 0
            body["cron"] = ""
            assert (await client.put(f"/api/subscriptions/{sub_id}", json=body)).status_code == 200
            assert (await db.get_subscription(sub_id))["interval"] == 0

        data = bundle()
        data["subscriptions"][0].update(cron="10 2 * * *", timezone="Asia/Shanghai")
        request = ImportRequest(config=data)
        plan = await preview_config(engine, request)
        assert plan["subscriptions"][0]["scheduled_at"]
        request.preview_token = plan["preview_token"]
        await apply_config(engine, request)
        exported = await export_config(db)
        request = ImportRequest(config=exported)
        assert all(s["action"] == "reuse" for s in (await preview_config(engine, request))["subscriptions"])
        assert next(s for s in exported["subscriptions"] if s["name"] == "Jable")["cron"] == "10 2 * * *"
    asyncio.run(run())


def test_recommended_paths_daily_frequency_and_staggering():
    data = json.loads(Path("docs/mdcng-recommended.json").read_text(encoding="utf-8"))
    libs = {lib["key"]: lib for lib in data["libraries"]}
    assert libs["chinese"]["dir"] == "chinese-subtitle"
    assert libs["chinese"]["external_dir"] == "/mnt/hls2strm_matched/chinese_subtitle"
    assert libs["native_uncensored"]["dir"] == "uncensored"
    assert libs["decensored"]["dir"] == "decensored"
    assert libs["other"]["dir"] == "asia"
    after = timestamp("2026-10-10T00:00:00")
    times = {}
    for sub in data["subscriptions"]:
        assert sub["timezone"] == "Asia/Shanghai" and "interval" not in sub
        at = next_cron_at(sub["cron"], sub["timezone"], after)
        assert next_cron_at(sub["cron"], sub["timezone"], at) - at == 86400
        times.setdefault(sub["site"], []).append(at)
    all_times = [at for values in times.values() for at in values]
    assert len(all_times) == len(set(all_times)) == 36
    for values in times.values():
        assert all(b - a == 1200 for a, b in zip(values, values[1:]))
    windows = sorted((min(values), max(values)) for values in times.values())
    assert all(a[1] < b[0] for a, b in zip(windows, windows[1:]))
