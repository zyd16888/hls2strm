import asyncio
import json

import aiosqlite

from hls2strm.codes import code_key, work_slug
from hls2strm.config import Settings, migrate_settings
from hls2strm.db import MIGRATIONS, Database


def test_code_key():
    assert code_key("SSIS-001") == code_key("ssis00001") == code_key("ssis-1") == "SSIS-1"
    assert code_key("FC2-PPV-1066192") == code_key("fc2ppv-1066192") == code_key("FC2-1066192") == "FC2PPV-1066192"
    assert code_key("T28-001") == "T28-1"
    assert code_key("300MIUM-001") == "300MIUM-1"
    assert code_key("KIRA-020-2") == "KIRA-20-2"
    assert code_key("010120-001") == "010120-001"  # 日期型番号原样
    assert code_key("") == ""
    assert work_slug("SSIS-001") == "ssis-001" and work_slug("SSIS-001", True) == "ssis-001-u"


def test_settings_migrate_single_site():
    data = migrate_settings({"domains": ["fs1.app"], "rate_per_sec": 2, "concurrency": 3, "proxy": "http://p:1"})
    s = Settings.model_validate(data)
    jable = s.site("jable")
    assert jable.domains == ["https://fs1.app"] and jable.rate_per_sec == 2 and jable.concurrency == 3
    assert s.proxy == "http://p:1"
    # 新加的站点自动补上默认设置；优先顺序补齐
    assert set(s.sites) == set(s.site_priority)


def test_migrate_v5_to_v6(boot):
    """v5 的库（播放地址记在影片上）升级到 v6：每部影片生成一个 Jable 源，地址搬过去。"""
    path = boot.data_dir / "v5.db"

    async def run():
        conn = await aiosqlite.connect(path, isolation_level=None)
        for step in MIGRATIONS[:5]:
            await step(conn)
        await conn.execute("PRAGMA user_version=5")
        await conn.execute(
            """INSERT INTO videos(id, slug, code, title, quality, categories, hls_url, hls_expires, detail_at, status,
                                  created_at, updated_at)
               VALUES(62384, 'ipzz-983', 'IPZZ-983', 't', '中文字幕', '[]', 'https://cdn/62384.m3u8', 99, 5, 'active', 1, 1),
                     (7, 'fc2ppv-1066192', 'FC2PPV-1066192', 't', '', ?, '', NULL, NULL, 'gone', 1, 1)""",
            (json.dumps([{"slug": "uncensored", "name": "無碼解放"}]),),
        )
        await conn.execute("INSERT INTO jobs(kind, name, params, status, created_at) VALUES('videos', 'x', '{}', 'running', 1)")
        await conn.execute("INSERT INTO tasks(job_id, kind, target, updated_at) VALUES(1, 'detail', 'ipzz-983', 1)")
        await conn.close()

        db = Database(path)
        await db.open()
        assert (await db._one("PRAGMA user_version"))[0] == len(MIGRATIONS)
        v = await db.get_video("ipzz-983")
        assert "hls_url" not in v and v["code_key"] == "IPZZ-983"
        src = (await db.get_sources(62384))[0]
        assert (src["site"], src["key"], src["site_vid"], src["subtitle"]) == ("jable", "ipzz-983", "62384", "zh")
        assert (src["stream_url"], src["stream_expires"], src["detail_at"]) == ("https://cdn/62384.m3u8", 99, 5)
        fc2 = await db.get_video("fc2ppv-1066192")
        assert fc2["code_key"] == "FC2PPV-1066192" and fc2["uncensored"] == 1
        assert (await db.get_sources(7))[0]["status"] == "gone"
        assert (await db.list_tasks(1))[0]["site"] == "jable"
        assert (await db.list_subscriptions())[0]["site"] == "jable"
        assert await db.cdn_video_map() == {62384: "ipzz-983", 7: "fc2ppv-1066192"}
        await db.close()

    asyncio.run(run())


def test_sources_merge_by_code(make_store):
    """不同站点的同一番号并成一部作品；元数据以优先级高的站点为准，其他站只补空字段。"""
    from hls2strm.sites import SourceDetail, SourceItem

    async def run():
        db, store = await make_store()
        rank = {"jable": 0, "missav": 1}.get

        # 先从低优先级站点进来：新建作品
        vid, created = await db.upsert_item("missav", SourceItem(key="ssis-001", code="SSIS-001", title="M 标题",
                                                                 duration=100), "ssis-001", rank)
        assert created
        # 高优先级站点的同一番号（写法不同）：挂到同一部作品上，标题以它为准
        vid2, created = await db.upsert_item("jable", SourceItem(key="ssis-001", code="SSIS-001", title="J 标题",
                                                                 site_vid="5"), "ssis-001", rank)
        assert vid2 == vid and not created
        v = await db.get_video_by_id(vid)
        assert v["title"] == "J 标题" and v["duration"] == 100
        # 低优先级站点的详情只补空字段
        await db.upsert_detail("missav", SourceDetail(key="ssis-001", code="SSIS-001", title="M 新标题",
                                                      stream_url="https://surrit/x/playlist.m3u8", maker="S1",
                                                      release_date="2021-02-21"), "ssis-001", rank)
        v = await db.get_video_by_id(vid)
        assert v["title"] == "J 标题" and v["maker"] == "S1" and v["release_date"] == "2021-02-21"
        assert len(await db.get_sources(vid)) == 2
        # 无码流出版是另一部作品
        vid3, created = await db.upsert_item("missav", SourceItem(key="ssis-001-uncensored-leak", code="SSIS-001",
                                                                  title="U", uncensored=True), "ssis-001-u", rank)
        assert created and vid3 != vid
        # 所有源都下架，作品才下架
        await db.mark_source_gone("jable", "ssis-001")
        assert (await db.get_video_by_id(vid))["status"] == "active"
        await db.mark_source_gone("missav", "ssis-001")
        assert (await db.get_video_by_id(vid))["status"] == "gone"
        await db.close()

    asyncio.run(run())


def test_close_stream_aborts_transfer():
    """curl_cffi 的 aclose() 只等传输结束；关之前要先设 quit_now，不然会把整个文件下完。"""
    from hls2strm.play import close_stream

    class Resp:
        def __init__(self):
            self.quit_now = asyncio.Event()
            self.closed_after_quit = None

        async def aclose(self):
            self.closed_after_quit = self.quit_now.is_set()

    r = Resp()
    asyncio.run(close_stream(r))
    assert r.closed_after_quit is True


def test_env_names_and_database_file(tmp_path, monkeypatch):
    """改名 hls2strm：环境变量用 HLS2STRM_*，旧的 JABLE_* 也认且优先（镜像里的默认值不能盖掉老部署显式写的）；
    数据目录里已有 jable.db 就接着用。"""
    from hls2strm.app import database_path
    from hls2strm.config import BootConfig

    monkeypatch.setenv("HLS2STRM_PORT", "9000")
    monkeypatch.setenv("HLS2STRM_UI_PASSWORD", "new")
    monkeypatch.setenv("JABLE_UI_PASSWORD", "old")
    boot = BootConfig.from_env()
    assert boot.port == 9000 and boot.ui_password == "old"

    assert database_path(tmp_path).name == "hls2strm.db"
    (tmp_path / "jable.db").write_bytes(b"")
    assert database_path(tmp_path).name == "jable.db"
    (tmp_path / "hls2strm.db").write_bytes(b"")
    assert database_path(tmp_path).name == "hls2strm.db"
