import asyncio
import shutil

import pytest

from .conftest import FakeFetcher
from .test_engine import build, model_fixture, wait_job


def test_verify_and_self_heal(make_store, boot):
    """输出目录换了挂载（记录还在、文件没了）：统计缺失、只检查不改、修复补回，订阅增量也会顺手补。"""

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1))  # 24 部带详情、封面进「全部」
        lib_dir = store.output_dir / "全部"
        assert len(list(lib_dir.glob("*/*.strm"))) == 24
        assert await engine.count_missing() == {}

        shutil.rmtree(lib_dir)  # 模拟输出目录换成了一个空目录
        assert await engine.count_missing() == {1: 24}

        # 只检查：报告缺什么，不动磁盘
        job = await wait_job(db, await engine.create_verify(repair=False))
        st = job["state"]
        assert (st["checked"], st["strm"], st["nfo"], st["cover"], st.get("repaired", 0)) == (24, 24, 24, 24, 0)
        assert not lib_dir.exists()

        # 修复：strm、nfo 本地重写，封面排队补
        job = await wait_job(db, await engine.create_verify())
        assert job["state"]["repaired"] == 24 and job["state"]["covers_queued"] == 24
        assert await db.task_counts(job["id"]) == {"done": 25}
        assert len(list(lib_dir.glob("*/*.strm"))) == 24 and len(list(lib_dir.glob("*/*.nfo"))) == 24
        assert len(list(lib_dir.glob("*/*-poster.jpg"))) == 24
        assert engine.missing == {}

        # 对外地址改了却没重写：strm 内容不是当前的播放地址，也算问题
        await store.update({"public_base_url": "http://new:8080"})
        job = await wait_job(db, await engine.create_verify(repair=False))
        assert job["state"]["strm"] == 24 and job["state"].get("cover", 0) == 0
        await wait_job(db, await engine.create_verify())
        slug = next(iter(ids))
        strm = lib_dir / slug.upper() / f"{slug.upper()}.strm"
        assert strm.read_text().strip() == f"http://new:8080/play/{slug}.m3u8"

        # 订阅增量翻到文件丢了的影片：顺手补回
        shutil.rmtree(strm.parent)
        sub_id = await db.create_subscription(name="t", source="/models/abc/", sort="post_date", library_id=1,
                                              initialized=1, stop_after_known=100, max_pages=1)
        await engine.count_missing()
        assert engine.missing == {1: 1}
        await wait_job(db, await engine.run_subscription(sub_id))
        assert strm.exists() and (strm.parent / f"{slug.upper()}-poster.jpg").exists()
        assert engine.missing == {1: 0}
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_restore_with_external_scraper(make_store, boot, tmp_path):
    """外部整理库：刮削器把 strm 挪到 {番号}/ 下并加后缀（-C）。按内容找回只更新路径；真丢了才写回收件目录；
    外部整理目录是空的（多半是挂载出了问题）不补，核对时勾「空的也写回」才补。"""

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        mdc = tmp_path / "mdc"
        lib_id = (await engine.create_library("外部", "收件", external_dir=str(mdc)))["id"]
        inbox = store.output_dir / "收件"
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=lib_id))

        def scrape():  # 刮削器：移动到 {番号}/{番号}-C.strm
            for p in list(inbox.rglob("*.strm")):
                dst = mdc / p.stem / f"{p.stem}-C.strm"
                dst.parent.mkdir(parents=True, exist_ok=True)
                p.rename(dst)

        scrape()
        assert not list(inbox.rglob("*.strm")) and len(list(mdc.rglob("*-C.strm"))) == 24
        sub_id = await db.create_subscription(name="t", source="/models/abc/", sort="post_date", library_id=lib_id,
                                              detail=0, initialized=1, stop_after_known=100, max_pages=1)

        # 增量：记录的路径不在了，按内容找到改了名的文件，只更新路径，收件目录里不补
        await wait_job(db, await engine.run_subscription(sub_id))
        assert not list(inbox.rglob("*.strm"))
        v = await db.get_video("ipzz-983")
        assert (await db.get_output(v["id"], lib_id))["strm_path"] == str(mdc / "IPZZ-983" / "IPZZ-983-C.strm")
        assert await engine.count_missing() == {}

        # 真丢了一部（刮削器那边被删了）：写回收件目录，交给刮削器再整理
        (mdc / "IPZZ-983" / "IPZZ-983-C.strm").unlink()  # 不清缓存：缓存里的位置失效时会自己重扫
        await wait_job(db, await engine.run_subscription(sub_id))
        assert [p.name for p in inbox.rglob("*.strm")] == ["IPZZ-983.strm"]
        scrape()

        # 外部整理目录整个空了：不往收件目录补（不然挂载恢复后会整库重刮），只统计缺失
        for p in mdc.rglob("*.strm"):
            p.unlink()
        engine.strm._index.clear()
        await wait_job(db, await engine.run_subscription(sub_id))
        assert not list(inbox.rglob("*.strm"))
        assert await engine.count_missing() == {lib_id: 24}
        job = await wait_job(db, await engine.create_verify(lib_id))
        assert job["state"]["external_empty"] == 24 and not list(inbox.rglob("*.strm"))

        # 收件目录里有文件（挂载掉了以后订阅照样往里写）不能算外部整理目录还能用
        one = inbox / "IPZZ-983" / "IPZZ-983.strm"
        one.parent.mkdir(parents=True, exist_ok=True)
        one.write_text(engine.writer.play_url(v) + "\n")
        job = await wait_job(db, await engine.create_verify(lib_id))
        assert (job["state"]["ok"], job["state"]["external_empty"]) == (1, 23)
        assert list(inbox.rglob("*.strm")) == [one]

        # 外部整理目录整个不在了
        shutil.rmtree(mdc)
        job = await wait_job(db, await engine.create_verify(lib_id))
        assert job["state"]["external_absent"] == 23 and list(inbox.rglob("*.strm")) == [one]

        # 确认要补：核对时勾「外部整理目录不在或是空的也写回」
        job = await wait_job(db, await engine.create_verify(lib_id, force_external=True))
        assert job["state"]["external_rewritten"] == 23 and len(list(inbox.rglob("*.strm"))) == 24
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_external_symlinks_count_as_available(make_store, boot, tmp_path):
    """刮削器用软链接模式：外部整理目录里只有软链接、真文件留在收件目录，也算外部整理目录能用，真丢的照常写回。"""
    try:
        (tmp_path / "probe").symlink_to(tmp_path)
    except OSError:
        pytest.skip("这个系统建不了软链接")

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        mdc = tmp_path / "mdc"
        lib_id = (await engine.create_library("外部", "收件", external_dir=str(mdc)))["id"]
        inbox = store.output_dir / "收件"
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=lib_id))
        files = sorted(inbox.rglob("*.strm"))
        for p in files:
            (mdc / p.stem).mkdir(parents=True)
            (mdc / p.stem / f"{p.stem}-C.strm").symlink_to(p)
        files[0].unlink()
        engine.strm._index.clear()
        job = await wait_job(db, await engine.create_verify(lib_id))
        assert (job["state"]["ok"], job["state"]["external_rewritten"]) == (23, 1) and files[0].is_file()
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_verify_fetch_details(make_store, boot, tmp_path):
    """核对时勾「没详情的抓详情」：普通库里没详情的排队抓，抓完补上 nfo；外部整理库不抓；不勾不联网。"""

    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = build(db, store, boot, fetcher)
        await engine.start()
        await store.update({"download_cover": False})  # 只看详情和 nfo，封面不掺和
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False))  # 24 部没详情进「全部」
        lib_dir = store.output_dir / "全部"
        ext_id = (await engine.create_library("外部", "收件", external_dir=str(tmp_path / "mdc")))["id"]
        vids = [(await db.get_video(slug))["id"] for slug in ids]
        assert await engine.add_to_library(vids, ext_id) == 24
        calls = len(fetcher.calls)

        job = await wait_job(db, await engine.create_verify())
        assert "details_queued" not in job["state"] and job["state"]["ok"] == 48
        job = await wait_job(db, await engine.create_verify(ext_id, details=True))
        assert job["state"]["details_queued"] == 0
        assert len(fetcher.calls) == calls and not list(lib_dir.glob("*/*.nfo"))

        job = await wait_job(db, await engine.create_verify(details=True))
        assert job["state"]["details_queued"] == 24
        assert await db.task_counts(job["id"]) == {"done": 25}
        assert all([(await db.get_video_by_id(vid))["detail_at"] for vid in vids])
        assert len(list(lib_dir.glob("*/*.nfo"))) == 24
        await engine.stop()
        await db.close()

    asyncio.run(run())
