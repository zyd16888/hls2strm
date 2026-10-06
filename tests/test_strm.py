import asyncio
import time
from pathlib import Path

from jable_strm.engine import Engine
from jable_strm.observability import Metrics
from jable_strm.strm_manage import classify, guess_slug, replace_prefix_in_file
from jable_strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job

NOW = time.time()
CDN = "https://a-b.mushroomtrack.com/hls/tok/1791180954/62000/{vid}/{vid}.m3u8"


def test_classify_and_guess():
    p = Path("/x/IPZZ-983/IPZZ-983.strm")
    assert classify(p, "http://h:8080/play/ipzz-983.m3u8\n", NOW).kind == "ours"
    info = classify(p, CDN.format(vid=62384), NOW)
    assert (info.kind, info.video_id, info.expired, info.slug) == ("cdn", 62384, True, "ipzz-983")
    info = classify(Path("/x/SONE-001-C 中字.strm"), "http://alist:5244/d/115/a.mp4", NOW)
    assert (info.kind, info.slug, info.prefix) == ("named", "sone-001-c", "http://alist:5244")
    assert classify(Path("/x/movie.strm"), "﻿# c\nhttp://alist/d/m.mkv", NOW).kind == "other"
    assert classify(Path("/x/a.strm"), "  \n", NOW).kind == "invalid"
    assert guess_slug(Path("/m/FC2-PPV-1234567.strm")) == "fc2-ppv-1234567"
    assert guess_slug(Path("/m/300MIUM-1483 xx.strm")) == "300mium-1483"
    assert guess_slug(Path("/m/IPZZ-983-uncensored.strm")) == "ipzz-983"
    assert guess_slug(Path("/m/random movie.strm")) == ""


def test_replace_prefix_in_file(tmp_path):
    f = tmp_path / "a.strm"
    f.write_text("http://old:8080/play/x.m3u8\n", encoding="utf-8")
    assert replace_prefix_in_file(f, "http://old:8080", "https://new") is not None
    assert f.read_text(encoding="utf-8") == "https://new/play/x.m3u8\n"
    assert replace_prefix_in_file(f, "http://old:8080", "https://new") is None  # 已不是旧前缀，跳过


def test_scan_adopt_prefix_revert(make_store, boot):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        ids["xyz-001"] = 99999
        fetcher = FakeFetcher(html, ids)
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1))  # 24 部进「全部」

        outside = store.output_dir / "外部"
        outside.mkdir(parents=True)
        (outside / "IPZZ-983.strm").write_text(CDN.format(vid=62384))           # 库里已有 → 重复
        (outside / "XYZ-001.strm").write_text(CDN.format(vid=99999))            # 库里没有 → 先抓详情
        (outside / "SONE-001 alist.strm").write_text("http://alist:5244/d/115/a.mkv\n")  # 文件名识别
        (outside / "movie.strm").write_text("http://alist:5244/d/115/movie.mkv\n")       # 其他来源
        (outside / "empty.strm").write_text("")

        scan_id = await engine.strm.create_scan()
        scan = await wait_job(db, scan_id)
        assert scan["state"]["files"] == 29 and scan["state"]["missing"] == 0
        summary = await db.strm_summary(scan_id)
        assert summary["kinds"]["ours"] == {"total": 24, "managed": 24}
        assert summary["kinds"]["cdn"]["total"] == 2 and summary["kinds"]["named"]["total"] == 1
        assert summary["adoptable"] == 3 and summary["adoptable_unknown"] == 2  # xyz-001、sone-001 库里没有

        # 纳管 CDN 直链：指定进「全部」库
        adopt = await wait_job(db, await engine.strm.create_adopt(scan_id, library_id=1))
        assert await db.task_counts(adopt["id"]) == {"done": 2}
        dup = await db.get_strm_file(str(outside / "IPZZ-983.strm"))
        assert dup["managed"] == 0 and dup["note"].startswith("重复")
        moved = store.output_dir / "全部" / "XYZ-001" / "XYZ-001.strm"
        assert moved.read_text().strip() == "http://jable-strm:8080/play/xyz-001.m3u8"
        assert not (outside / "XYZ-001.strm").exists()
        assert (await db.get_strm_file(str(outside / "XYZ-001.strm")))["note"].startswith("已纳管")

        # 改前缀：旧前缀就是对外地址 → 同步改设置；然后回滚
        scan_id = await engine.strm.create_scan()
        await wait_job(db, scan_id)
        preview = await engine.strm.preview_prefix(scan_id, "http://jable-strm:8080", "https://jable.example.com")
        assert preview["count"] == 25 and preview["updates_setting"] and len(preview["samples"]) == 20
        job = await wait_job(db, await engine.strm.create_prefix(scan_id, "http://jable-strm:8080", "https://jable.example.com"))
        assert job["state"]["changed"] == 25
        assert moved.read_text().strip() == "https://jable.example.com/play/xyz-001.m3u8"
        assert store.public_base_url == "https://jable.example.com"
        sets = await db.list_change_sets()
        assert sets[0]["files"] == 25 and sets[0]["reverted"] == 0

        moved.write_text("http://changed-elsewhere/play/xyz-001.m3u8\n")  # 被别处改过的文件回滚时跳过
        revert = await wait_job(db, await engine.strm.create_revert(job["id"]))
        assert revert["state"] == {"restored": 24, "skipped": 1}
        assert store.public_base_url == "http://jable-strm:8080"
        assert (store.output_dir / "全部" / "IPZZ-983" / "IPZZ-983.strm").read_text().startswith("http://jable-strm:8080/")

        # 其他来源也能改前缀
        job = await wait_job(db, await engine.strm.create_prefix(scan_id, "http://alist:5244", "http://alist2:5244"))
        assert job["state"]["changed"] == 2
        assert (outside / "movie.strm").read_text() == "http://alist2:5244/d/115/movie.mkv\n"
        await engine.stop()

    asyncio.run(run())


def test_external_library_survives_mdcng_move(make_store, boot, tmp_path):
    """外部整理库：strm 被 mdcng 移走、改名后，不在原位置补写，同步位置能找回，重写原地改内容。"""
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = Engine(db, FakeFetcher(html, ids), OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        mdc = tmp_path / "mdc"
        try:
            await engine.create_library("内嵌", "收件", external_dir="收件/整理好")
            raise AssertionError("外部整理目录嵌在库目录里应当报错")
        except ValueError:
            pass
        try:
            await engine.create_library("撞库", "收件", external_dir=str(store.output_dir / "全部" / "x"))
            raise AssertionError("外部整理目录和别的库重叠应当报错")
        except ValueError:
            pass
        lib_id = (await engine.create_library("外部", "收件", external_dir=str(mdc)))["id"]
        inbox = store.output_dir / "收件"

        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=lib_id))
        assert len(list(inbox.rglob("*.strm"))) == 24

        # mdcng 移动模式：搬到 女优/番号/ 并改名，旁边生成自己的 nfo 和图片
        for p in list(inbox.rglob("*.strm")):
            dst = mdc / "某女优" / p.stem / f"{p.stem} 标题.strm"
            dst.parent.mkdir(parents=True)
            p.rename(dst)
            for suffix in (".nfo", "-poster.jpg", "-fanart.jpg"):
                dst.with_name(dst.stem + suffix).write_text("mdc")
        moved = mdc / "某女优" / "IPZZ-983" / "IPZZ-983 标题.strm"
        mdc_nfo = moved.with_name("IPZZ-983 标题.nfo")

        # 增量跟踪、重抓详情都不在收件目录补写，也不碰 mdcng 的 nfo
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=lib_id,
                                                     incremental=True))
        await engine.fetch_detail("ipzz-983", priority=True)
        assert not list(inbox.rglob("*.strm")) and not list(inbox.rglob("*.nfo"))
        assert mdc_nfo.read_text() == "mdc"

        # 同步位置：按内容找回改了名的文件
        job = await wait_job(db, await engine.strm.create_locate(lib_id))
        assert job["state"] == {"checked": 24, "updated": 24, "missing": 0, "duplicates": 0, "extra": 0}
        v = await db.get_video("ipzz-983")
        assert (await db.get_output(v["id"], lib_id))["strm_path"] == str(moved)

        # mdcng 又挪了一次位置；改对外地址后重写：先自动同步位置，再原地改内容，不搬回收件目录
        moved2 = mdc / "另一女优" / "IPZZ-983.strm"
        moved2.parent.mkdir()
        moved.rename(moved2)
        await store.update({"public_base_url": "https://jable.example.com"})
        await wait_job(db, await engine.create_rewrite(lib_id))
        assert moved2.read_text().strip() == "https://jable.example.com/play/ipzz-983.m3u8"
        assert not list(inbox.rglob("*.strm")) and mdc_nfo.read_text() == "mdc"

        # 扫描 mdcng 目录：文件归到外部库、已纳管；纳管外部库的文件不移动
        await db.delete_output(v["id"], lib_id)
        scan_id = await engine.strm.create_scan(str(mdc))
        await wait_job(db, scan_id)
        f = await db.get_strm_file(str(moved2))
        assert (f["library_id"], f["managed"]) == (lib_id, 0)
        assert (await db.get_strm_file(str(mdc / "某女优" / "ABF-156" / "ABF-156 标题.strm")))["managed"] == 1
        await wait_job(db, await engine.strm.create_adopt(scan_id, kinds=("ours",)))
        assert moved2.exists() and (await db.get_output(v["id"], lib_id))["strm_path"] == str(moved2)

        # 删除库连同文件：只删 strm，mdcng 的 nfo、图片保留
        await wait_job(db, await engine.delete_library(lib_id, delete_files=True))
        assert not list(mdc.rglob("*.strm"))
        assert mdc_nfo.exists() and len(list(mdc.rglob("*.nfo"))) == 24
        await engine.stop()

    asyncio.run(run())
