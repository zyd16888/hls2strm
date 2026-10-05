import asyncio
import io

from PIL import Image

from jable_strm.writer import OutputWriter, build_nfo, crop_poster, sanitize

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
        strm = w.write(VIDEO)
        assert strm == store.output_dir / "IPZZ-983" / "IPZZ-983.strm"
        assert strm.read_text().strip() == "http://jable-strm:8080/play/ipzz-983.m3u8"
        assert (strm.parent / "IPZZ-983.nfo").exists()
        assert await w.write_cover(FakeFetcher("", {}), VIDEO, strm)
        assert (strm.parent / "IPZZ-983-poster.jpg").exists()

        await store.update({"path_template": "{actor}/{slug}", "play_token": "s3cret"})
        moved = w.write({**VIDEO, "strm_path": str(strm)})
        assert moved == store.output_dir / "瀬緒凛" / "IPZZ-983.strm"
        assert moved.read_text().strip().endswith("/play/ipzz-983.m3u8?t=s3cret")
        assert not strm.exists() and not strm.parent.exists()  # 旧目录已清理
        await db.close()

    asyncio.run(run())
