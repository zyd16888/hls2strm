import asyncio
import shutil

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
        assert job["state"]["external_unavailable"] == 24 and not list(inbox.rglob("*.strm"))

        # 确认要补：核对时勾「外部整理目录是空的也写回」
        job = await wait_job(db, await engine.create_verify(lib_id, force_external=True))
        assert job["state"]["external_rewritten"] == 24 and len(list(inbox.rglob("*.strm"))) == 24
        await engine.stop()
        await db.close()

    asyncio.run(run())
