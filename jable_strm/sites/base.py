"""站点适配器接口：怎么拼列表页、详情页地址，怎么解析，播放地址怎么取。每个站点一个模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..errors import ParseError

if TYPE_CHECKING:
    from ..fetcher import SiteFetcher


@dataclass
class SourceItem:
    """列表页里的一部影片（某个站点上的一个源）。"""

    key: str
    code: str
    title: str
    site_vid: str = ""
    duration: int | None = None
    thumb_url: str = ""
    preview_url: str = ""
    views: int | None = None
    likes: int | None = None
    subtitle: str = ""  # zh / en / 空
    uncensored: bool = False


@dataclass
class ListPage:
    items: list[SourceItem]
    last_page: int | None
    block_id: str | None = None


@dataclass
class SourceDetail:
    key: str
    code: str
    title: str
    stream_url: str = ""
    stream_expires: int | None = None
    site_vid: str = ""
    subtitle: str = ""
    uncensored: bool = False
    duration: int | None = None
    cover_url: str = ""
    release_date: str = ""
    quality: str = ""
    views: int | None = None
    favs: int | None = None
    models: list[dict] = field(default_factory=list)
    categories: list[dict] = field(default_factory=list)
    tags: list[dict] = field(default_factory=list)
    maker: str = ""
    director: str = ""
    series: str = ""


@dataclass
class StreamTraits:
    direct: bool = True  # 播放器能否直连 CDN（302）；不能的一律由本服务中转
    expires: bool = True  # 地址会过期，要按剩余有效期换新
    headers: dict[str, str] = field(default_factory=dict)  # 中转时请求 CDN 要带的头
    disguised_segments: bool = False  # 分片伪装成图片（video0.jpeg），中转时改名 .ts


@dataclass
class Stream:
    url: str
    expires: int | None
    detail: SourceDetail | None = None  # 取地址时顺带拿到的详情，用来更新元数据


class Site:
    name = ""
    label = ""
    default_domains: list[str] = []
    stream = StreamTraits()
    sorts: dict[str, str] = {}
    presets: list[dict] = []
    default_sort = ""
    source_hint = ""  # 列表地址的写法说明，显示在任务页

    def detail_path(self, key: str) -> str:
        raise NotImplementedError

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        raise NotImplementedError

    def parse_list(self, html: str) -> ListPage:
        raise NotImplementedError

    def normalize_source(self, value: str) -> str:
        raise NotImplementedError

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        raise NotImplementedError

    def key_from_url(self, url: str) -> str | None:
        """影片网址 -> 站内 key；不是本站影片地址返回 None。"""
        return None

    def key_for(self, code: str, subtitle: str = "", uncensored: bool = False) -> str | None:
        """按番号直接构造站内 key（补源用）；站点不支持返回 None。"""
        return None

    async def fetch_detail(self, sf: SiteFetcher, key: str, *, priority: bool = False) -> SourceDetail:
        page = await sf.get_page(self.detail_path(key), priority=priority)
        try:
            return self.parse_detail(page.html, key)
        except ParseError as e:
            e.html = page.html
            raise

    async def fetch_stream(self, sf: SiteFetcher, key: str) -> Stream:
        """现取播放地址。默认就是详情页里的地址；要多跳才能拿到直链的站点覆盖它。"""
        d = await self.fetch_detail(sf, key, priority=True)
        if not d.stream_url:
            raise ParseError("详情页没有播放地址")
        return Stream(d.stream_url, d.stream_expires, d)
