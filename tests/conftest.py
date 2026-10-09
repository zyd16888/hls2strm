import asyncio
import io
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from hls2strm.config import BootConfig, SettingsStore
from hls2strm.db import Database
from hls2strm.fetcher import NotFound, Page

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def jpeg(w: int = 800, h: int = 538) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


class FakeMediaSession:
    async def get(self, url, **kw):
        class Response:
            status_code = 206
            headers = {"content-type": "video/mp2t"}

            async def aiter_content(self):
                yield b"\x47" + b"\0" * 32767

            async def aclose(self):
                pass
        return Response()


class FakeFetcher:
    """按路径返回样本页面；详情页按 slug 换掉 videoId，模拟不同影片。"""

    def __init__(self, list_html: str, ids: dict[str, int]) -> None:
        self.list_html = list_html
        self.ids = ids
        self.detail_html = fixture("detail.html")
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.pages: dict[str, dict[str, str]] = {}  # 其他站点：站点 -> {路径: 页面}
        self.files: dict[str, bytes] = {}  # get_bytes 按地址返回的内容
        self.session = FakeMediaSession()

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
            html = html.replace("IPZZ-983", slug.upper())  # 标题里的番号换成这部影片的
            # 样本里的播放地址早已过期：换成「现在 + 3 小时」，模拟刚抓到的地址
            html = re.sub(r"(/hls/[^/]+/)\d{9,11}/", rf"\g<1>{int(time.time()) + 10800}/", html)
            return Page(html, "https://fs1.app" + path, "https://fs1.app")
        return Page(self.list_html, "https://fs1.app" + path, "https://fs1.app")

    def site(self, name: str):
        """Jable 的抓取通道就是它自己；其他站点只认 pages 里登记过的页面，没登记的 404。"""
        return self if name == "jable" else FakeSite(self, name)

    async def get_bytes(self, url: str, *, referer=None, headers=None) -> bytes:
        if url in self.files:
            return self.files[url]
        if url.endswith(".m3u8"):
            return b"#EXTM3U\n#EXTINF:3600.0,\na.ts\n#EXTINF:1800.5,\nb.ts\n#EXT-X-ENDLIST\n"
        return jpeg()

    async def fetch(self, url: str, *, method: str = "GET", headers=None, **kw):
        """站外请求：只用来 HEAD 分片看大小（探测画质），按 files 里登记的内容算，没登记的当 450 MB。"""
        size = len(self.files[url]) if url in self.files else 450_000_000
        return SimpleNamespace(status_code=200, headers={"content-length": str(size)}, text="", url=url)


class FakeSite:
    def __init__(self, parent: FakeFetcher, name: str) -> None:
        self.parent = parent
        self.name = name

    async def get_page(self, path: str, *, priority: bool = False) -> Page:
        tag = f"{self.name}:{path}"
        self.parent.calls.append(tag)
        for key, exc in self.parent.fail.items():
            if key in tag:
                raise exc
        html = self.parent.pages.get(self.name, {}).get(path)
        if html is None:
            raise NotFound(path)
        return Page(html, "https://" + self.name + path, "https://" + self.name)


@pytest.fixture
def boot(tmp_path) -> BootConfig:
    return BootConfig(data_dir=tmp_path, default_public_base_url="http://hls2strm:8080")


@pytest.fixture
def make_store(boot):
    opened: list[Database] = []

    async def _make():
        db = Database(boot.data_dir / "test.db")
        await db.open()
        opened.append(db)
        store = SettingsStore(boot, db)
        await store.load()
        return db, store

    yield _make
    # 测试中途失败时也要关库：aiosqlite 的后台线程不是守护线程，不关会让进程退出时卡住
    for db in opened:
        if db.conn is not None:
            asyncio.run(db.close())
