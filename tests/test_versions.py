import asyncio
import time
from pathlib import Path

from hls2strm.quality import Quality
from hls2strm.strm_manage import classify

from .conftest import FakeFetcher
from .test_engine import build, model_fixture, wait_job


def _names(d: Path) -> set[str]:
    return {p.name for p in d.iterdir()} if d.exists() else set()


def test_version_files_in_library(make_store, boot):
    """输出库开了多画质版本：至少两档才写，两种命名都行，换命名、调最低档、画质变了、移出库、关掉都跟着改。"""

    async def run():
        db, store = await make_store()
        await store.update({"quality_capture": False})
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False))
        lib_dir = store.output_dir / "全部"
        folder = lib_dir / "IPZZ-983"
        assert _names(folder) == {"IPZZ-983.strm"}
        await db._write("UPDATE sources SET heights='720,480', height=720, quality_src='master'")

        jobs = await engine.update_library(1, "全部", "全部", versions="emby")
        await wait_job(db, jobs["rewrite_job_id"])
        assert _names(folder) == {"IPZZ-983.strm", "IPZZ-983 - 720p.strm", "IPZZ-983 - 480p.strm"}
        assert (folder / "IPZZ-983 - 480p.strm").read_text().strip() == "http://hls2strm:8080/play/ipzz-983@480p.m3u8"

        jobs = await engine.update_library(1, "全部", "全部", versions="suffix")  # 换命名：旧的清掉
        await wait_job(db, jobs["rewrite_job_id"])
        assert _names(folder) == {"IPZZ-983.strm", "IPZZ-983-720p.strm", "IPZZ-983-480p.strm"}

        await store.update({"version_min_height": 720})  # 只剩一档：不写
        await wait_job(db, await engine.create_rewrite(1))
        assert _names(folder) == {"IPZZ-983.strm"}
        await store.update({"version_min_height": 480})
        await wait_job(db, await engine.create_rewrite(1))

        # 画质变了：开了多画质版本的库里，这部片的版本跟着改
        v = await db.get_video("ipzz-983")
        src = (await db.get_sources(v["id"]))[0]
        await engine.resolver.quality.save(src["id"], None, Quality([1080, 720, 480], "master"))
        assert _names(folder) == {"IPZZ-983.strm", "IPZZ-983-1080p.strm", "IPZZ-983-720p.strm", "IPZZ-983-480p.strm"}

        assert await engine.remove_from_library([v["id"]], 1) == 1  # 移出库：版本一起删，目录清掉
        assert not folder.exists()

        assert any("@" in p.read_text() for p in lib_dir.rglob("*.strm"))  # 别的片还有版本文件
        jobs = await engine.update_library(1, "全部", "全部", versions="")  # 关掉：清掉以前写的
        await wait_job(db, jobs["rewrite_job_id"])
        assert not any("@" in p.read_text() for p in lib_dir.rglob("*.strm"))
        assert classify(Path("x.strm"), "http://h:8080/play/ipzz-983@720p.m3u8\n", time.time()).kind == "version"
        await engine.stop()
        await db.close()

    asyncio.run(run())


def test_version_files_after_external_tool(make_store, boot, tmp_path):
    """外部整理库：还在收件目录时不写（免得外部工具把版本当成另一部片整理）；整理好以后在它旁边补，
    同步位置不会把版本文件当成主文件。"""

    async def run():
        db, store = await make_store()
        await store.update({"quality_capture": False})
        html, ids = model_fixture()
        engine = build(db, store, boot, FakeFetcher(html, ids))
        await engine.start()
        mdc = tmp_path / "mdc"
        lib_id = (await engine.create_library("外部", "收件", external_dir=str(mdc), versions="suffix"))["id"]
        inbox = store.output_dir / "收件"
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=lib_id))
        await db._write("UPDATE sources SET heights='720,480', height=720, quality_src='master'")
        await wait_job(db, await engine.create_rewrite(lib_id))
        assert all(p.name.count("-") == 1 for p in inbox.rglob("*.strm"))  # 收件目录里没有版本文件

        for p in list(inbox.rglob("*.strm")):  # mdcng：目录 {number}，文件 {number}-C
            dst = mdc / p.stem / f"{p.stem}-C.strm"
            dst.parent.mkdir(parents=True, exist_ok=True)
            p.rename(dst)
        await engine.strm.locate(engine.libs[lib_id])
        assert _names(mdc / "IPZZ-983") == {"IPZZ-983-C.strm", "IPZZ-983-C-720p.strm", "IPZZ-983-C-480p.strm"}
        v = await db.get_video("ipzz-983")
        assert (await db.get_output(v["id"], lib_id))["strm_path"] == str(mdc / "IPZZ-983" / "IPZZ-983-C.strm")
        engine.strm._index.clear()
        await engine.strm.locate(engine.libs[lib_id])  # 再同步一次：版本文件不会被当成主文件
        assert (await db.get_output(v["id"], lib_id))["strm_path"] == str(mdc / "IPZZ-983" / "IPZZ-983-C.strm")
        assert await engine.count_missing() == {}
        await engine.stop()
        await db.close()

    asyncio.run(run())
