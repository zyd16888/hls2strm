import asyncio
import io
import os

from PIL import Image

from hls2strm.writer import OutputWriter, build_nfo, crop_poster, sanitize

from .conftest import FakeFetcher, jpeg

VIDEO = {
    "id": 62384,
    "slug": "ipzz-983",
    "code": "IPZZ-983",
    "title": 'IPZZ-983 標題 <a> & "b"',
    "duration": 7201,
    "release_date": "2026-10-01",
    "quality": "高清原片",
    "models": [{"id": "d93c", "name": "瀬緒凛"}],
    "categories": [{"slug": "bdsm", "name": "主奴調教"}],
    "tags": [{"slug": "creampie", "name": "中出"}],
    "cover_url": "https://assets.fs1.app/x/preview.jpg",
    "hls_url": "https://cdn/hls/t/1/62384.m3u8",
    "detail_at": 1,
    "strm_path": "",
}


def test_sanitize():
    assert sanitize('a/b:c*?"<>|d') == "a b c d"
    assert sanitize("...") == "_"
    assert len(sanitize("x" * 300)) == 100


def test_nfo_escapes_and_fields():
    nfo = build_nfo(VIDEO)
    assert "&lt;a&gt; &amp;" in nfo
    assert "<runtime>120</runtime>" in nfo
    assert "<premiered>2026-10-01</premiered>" in nfo
    assert "<genre>主奴調教</genre>" in nfo and "<tag>中出</tag>" in nfo
    assert "<name>瀬緒凛</name>" in nfo


def test_crop_poster():
    im = Image.open(io.BytesIO(crop_poster(jpeg(800, 538))))
    assert im.size == (377, 538)
    portrait = jpeg(400, 600)
    assert crop_poster(portrait) == portrait


def test_write_and_move(make_store):
    async def run():
        db, store = await make_store()
        w = OutputWriter(store)
        lib = {"id": 1, "dir": "全部", "path_template": ""}
        keep = frozenset({w.library_root(lib)})
        strm = w.write(VIDEO, lib)
        assert strm == store.output_dir / "全部" / "IPZZ-983" / "IPZZ-983.strm"
        assert strm.read_text().strip() == "http://hls2strm:8080/play/ipzz-983.m3u8"
        assert (strm.parent / "IPZZ-983.nfo").exists()
        assert await w.write_cover(FakeFetcher("", {}), VIDEO, strm)
        assert (strm.parent / "IPZZ-983-poster.jpg").exists()

        # 模板变化：nfo 和封面跟着搬，旧目录清掉，库根目录保留
        await store.update({"path_template": "{actor}/{slug}", "play_token": "s3cret"})
        moved = w.write(VIDEO, lib, str(strm), keep)
        assert moved == store.output_dir / "全部" / "瀬緒凛" / "IPZZ-983.strm"
        assert moved.read_text().strip().endswith("/play/ipzz-983.m3u8?t=s3cret")
        assert (moved.parent / "IPZZ-983-poster.jpg").exists() and (moved.parent / "IPZZ-983-fanart.jpg").exists()
        assert not strm.exists() and not strm.parent.exists()
        assert w.library_root(lib).exists()

        # 库级模板覆盖全局模板；库目录可以是绝对路径
        other = {"id": 2, "dir": str(store.output_dir.parent / "elsewhere"), "path_template": "{year}/{slug}"}
        assert w.base_path(VIDEO, other) == store.output_dir.parent / "elsewhere" / "2026" / "IPZZ-983"

        w.remove(moved, keep)
        assert not moved.exists() and not moved.parent.exists() and w.library_root(lib).exists()
        await db.close()

    asyncio.run(run())


def test_cover_hardlink_from_sibling(make_store):
    async def run():
        db, store = await make_store()
        w = OutputWriter(store)
        a = w.write(VIDEO, {"id": 1, "dir": "全部", "path_template": ""})
        b = w.write(VIDEO, {"id": 2, "dir": "中文字幕", "path_template": ""})
        fetcher = FakeFetcher("", {})
        calls = []
        orig = fetcher.get_bytes

        async def counting(url, **kw):
            calls.append(url)
            return await orig(url, **kw)

        fetcher.get_bytes = counting
        assert await w.write_cover(fetcher, VIDEO, a)
        assert await w.write_cover(fetcher, VIDEO, b, [a])
        assert len(calls) == 1  # 第二个库直接链接，不再下载
        fa, fb = (p.with_name("IPZZ-983-fanart.jpg") for p in (a, b))
        assert fb.exists() and os.path.samefile(fa, fb)
        await db.close()

    asyncio.run(run())
