import asyncio
import datetime as dt
import os
import time

from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.trash import TRASH_DIR, find_orphans, purge, retire
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher, fixture
from .test_engine import model_fixture, wait_job

TODAY = dt.date(2026, 10, 10)
OLD = time.time() - 3 * 86400


def scraped(d, code, *extra):
    """外部刮削器整理好的一部片：nfo、图片、extrafanart，外加 extra 里的文件。"""
    d.mkdir(parents=True, exist_ok=True)
    for name in (f"{code}.nfo", f"{code}-poster.jpg", f"{code}-fanart.jpg", *extra):
        (d / name).write_text("mdc")
    (d / "extrafanart").mkdir(exist_ok=True)
    (d / "extrafanart" / "fanart1.jpg").write_text("mdc")
    return d


def age(root, t=OLD):
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            os.utime(os.path.join(dirpath, name), (t, t))
    os.utime(root, (t, t))


def test_retire_moves_exclusive_movie_dir(tmp_path):
    ext = tmp_path / "asia"
    movie = scraped(ext / "SONE" / "SONE-123", "SONE-123", "SONE-123-trailer.mp4", "SONE-123.zh.srt")
    target = retire(movie, ext, 7, TODAY)
    assert target == ext / TRASH_DIR / "2026-10-10" / "SONE" / "SONE-123"
    assert (target / "SONE-123.nfo").is_file() and (target / "extrafanart" / "fanart1.jpg").is_file()
    assert not (ext / "SONE").exists() and ext.is_dir()  # 空出来的上级目录删掉，整理目录本身留着

    # 同一天又移进来一份同名的：加序号，不覆盖
    again = retire(scraped(ext / "SONE" / "SONE-123", "SONE-123"), ext, 7, TODAY)
    assert again.name == "SONE-123 (2)" and (target / "SONE-123-trailer.mp4").is_file()

    # 还有别的片、视频、软链接的目录不动；整理目录本身、整理目录外面、回收区里面都不动
    shared = scraped(ext / "SONE" / "SONE-124", "SONE-124", "SONE-125.strm")
    video = scraped(ext / "SONE" / "SONE-126", "SONE-126", "SONE-126.mkv")
    outside = scraped(tmp_path / "other" / "X-1", "X-1")
    for d in (shared, video, ext, outside, target, tmp_path / "missing"):
        assert retire(d, ext, 7, TODAY) is None
    assert (shared / "SONE-124.nfo").is_file() and (video / "SONE-126.nfo").is_file() and outside.is_dir()

    # 保留 0 天：不进回收区，直接删
    gone = scraped(ext / "ABF" / "ABF-001", "ABF-001")
    assert retire(gone, ext, 0, TODAY) == gone
    assert not gone.exists() and not (ext / "ABF").exists()


def test_purge_keeps_full_days(tmp_path):
    ext = tmp_path / "asia"
    for name in ("2026-10-02", "2026-10-03", "2026-10-09", "说明"):
        scraped(ext / TRASH_DIR / name / "A" / "A-1", "A-1")
    assert purge(ext, 7, TODAY) == 1  # 10-02 放满了 7 整天；10-03 还差一点
    assert sorted(p.name for p in (ext / TRASH_DIR).iterdir()) == ["2026-10-03", "2026-10-09", "说明"]
    assert purge(ext, 0, TODAY) == 2 and [p.name for p in (ext / TRASH_DIR).iterdir()] == ["说明"]
    assert purge(tmp_path / "missing", 7, TODAY) == 0


def test_find_orphans(tmp_path):
    ext = tmp_path / "asia"
    scraped(ext / "A" / "A-1", "A-1")  # 只剩元数据：残留
    scraped(ext / "A" / "A-2", "A-2", "A-2.strm")  # 还有 strm
    scraped(ext / "B" / "B-1", "B-1", "B-1-trailer.mp4")
    scraped(ext / "B" / "B-1" / "behind the scenes", "B-1-bts")  # 下层也有 nfo：跟着最上层走
    scraped(ext / "E-1", "E-1", "E-1.mkv")  # 还有视频
    scraped(ext / TRASH_DIR / "2026-10-01" / "D" / "D-1", "D-1")  # 回收区不算
    (ext / "root.nfo").write_text("mdc")  # 整理目录本身不算
    age(ext)
    scraped(ext / "C-1", "C-1")  # 刚改过：可能正在整理
    found = find_orphans(ext)
    assert [p.relative_to(ext).as_posix() for p, _ in found] == ["A/A-1", "B/B-1"]
    assert all(size > 0 for _, size in found)


def test_leaving_external_library_retires_scraped_dir(make_store, boot, tmp_path):
    """影片后来进了中文字幕：「其他」里 mdcng 刮好的目录整个移进回收区，不留只剩 nfo、图片的残留；
    记录的路径还是收件目录（mdcng 在上次同步位置之后才挪走）也能找到。"""
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        mdc = tmp_path / "mdc"
        cn = (await engine.create_library("中字", "中字", external_dir=str(mdc / "cn")))["id"]
        other = (await engine.create_library("其他", "其他", external_dir=str(mdc / "other"), excludes=[cn]))["id"]
        inbox = store.output_dir / "其他"

        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=other))
        assert (await engine.settle())["written"] == 24

        # mdcng 移动模式：搬到 前缀/番号/，旁边生成 nfo、图片；本服务还没同步位置，按内容找的缓存也是旧的
        await engine.strm.index(engine.libs[other])
        for p in list(inbox.rglob("*.strm")):
            d = scraped(mdc / "other" / p.stem.split("-")[0] / p.stem, p.stem)
            p.rename(d / p.name)
        v = await db.get_video("ipzz-983")
        stale = (await db.get_output(v["id"], other))["strm_path"]
        assert not os.path.exists(stale) and str(inbox) in stale

        # 中字订阅抓到 IPZZ-983：归并把它从「其他」移出
        fetcher.list_html = fixture("list_category_full.html")
        await wait_job(db, await engine.create_crawl("/categories/chinese-subtitle/", end_page=1, detail=False,
                                                     library_id=cn))
        assert (await engine.settle())["removed"] == 1
        trashed = mdc / "other" / TRASH_DIR / dt.date.today().isoformat() / "IPZZ" / "IPZZ-983"
        assert not (mdc / "other" / "IPZZ" / "IPZZ-983").exists()
        assert (trashed / "IPZZ-983.nfo").is_file() and not list(trashed.rglob("*.strm"))
        assert len(list((mdc / "other").rglob("*.strm"))) == 23 and await db.get_output(v["id"], other) is None

        # 以前留下的残留：只检查不动文件；清理时移进回收区，回收区里的不再算
        leftover = scraped(mdc / "other" / "OLD" / "OLD-001", "OLD-001")
        age(leftover)
        job = await wait_job(db, await engine.strm.create_tidy(other, apply=False))
        assert (job["state"]["orphans"], job["state"]["moved"]) == (1, 0) and leftover.is_dir()
        job = await wait_job(db, await engine.strm.create_tidy(other))
        assert (job["state"]["orphans"], job["state"]["moved"]) == (1, 1) and not leftover.exists()
        assert (mdc / "other" / TRASH_DIR / dt.date.today().isoformat() / "OLD" / "OLD-001" / "OLD-001.nfo").is_file()

        # 自动清理：最近一天跑过就不再排；只检查的不算
        await engine._housekeep()
        assert sum(j["kind"] == "tidy" for j in await db.list_jobs()) == 2
        await db._write("UPDATE jobs SET created_at=created_at-86400 WHERE kind='tidy'")
        await engine._housekeep()
        auto = await wait_job(db, (await db.list_jobs())[0]["id"])
        assert auto["kind"] == "tidy" and auto["params"] == {"library_id": None, "apply": True}

        # 保留 0 天：手动移出时直接删掉整理目录
        await store.update({"trash_days": 0})
        v = await db.get_video("abf-156")
        assert await engine.remove_from_library([v["id"]], other) == 1
        assert not (mdc / "other" / "ABF" / "ABF-156").exists()
        assert not list((mdc / "other" / TRASH_DIR).rglob("ABF-156*"))
        await engine.stop()

    asyncio.run(run())
