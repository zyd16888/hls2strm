import io
import re
from pathlib import Path

import pytest
from PIL import Image

from jable_strm.config import BootConfig, SettingsStore
from jable_strm.db import Database
from jable_strm.fetcher import NotFound, Page

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def jpeg(w: int = 800, h: int = 538) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


class FakeFetcher:
    """按路径返回样本页面；详情页按 slug 换掉 videoId，模拟不同影片。"""

    def __init__(self, list_html: str, ids: dict[str, int]) -> None:
        self.list_html = list_html
        self.ids = ids
        self.detail_html = fixture("detail.html")
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}

    async def get_page(self, path: str, *, priority: bool = False) -> Page:
        self.calls.append(path)
        for key, exc in self.fail.items():
            if key in path:
                raise exc
        if path.startswith("/videos/"):
            slug = path.split("/")[2]
            if slug not in self.ids:
                raise NotFound(path)
            html = re.sub(r"videoId: '\d+'", f"videoId: '{self.ids[slug]}'", self.detail_html)
            return Page(html, "https://fs1.app" + path, "https://fs1.app")
        return Page(self.list_html, "https://fs1.app" + path, "https://fs1.app")

    async def get_bytes(self, url: str, *, referer=None) -> bytes:
        if url.endswith(".m3u8"):
            return b"#EXTM3U\n#EXTINF:3600.0,\na.ts\n#EXTINF:1800.5,\nb.ts\n#EXT-X-ENDLIST\n"
        return jpeg()


@pytest.fixture
def boot(tmp_path) -> BootConfig:
    return BootConfig(data_dir=tmp_path, default_public_base_url="http://jable-strm:8080")


@pytest.fixture
def make_store(boot):
    async def _make():
        db = Database(boot.data_dir / "test.db")
        await db.open()
        store = SettingsStore(boot, db)
        await store.load()
        return db, store

    return _make
