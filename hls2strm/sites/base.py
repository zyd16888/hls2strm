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
    variants: list[str] = field(default_factory=list)  # 同一部片在本站其他版本的 key（中字、无码流出等）
    lines: list[tuple[str, str]] = field(default_factory=list)  # 多线路站点：[(线路名, 取直链要用的数据)]


@dataclass
class StreamTraits:
    direct: bool = True  # 播放器能否直连 CDN（302）；不能的一律由本服务中转
    expires: bool = True  # 地址会过期，要按剩余有效期换新
    headers: dict[str, str] = field(default_factory=dict)  # 中转时请求 CDN 要带的头
    disguised_segments: bool = False  # 分片伪装成图片（video0.jpeg），中转时改名 .ts
    ip_bound: bool = False  # 直链绑了取地址时的出口 IP：和本服务同一出口的播放器能 302，网关（外网客户端）不行
    ua_block: bool = False  # CDN 拒绝 ffmpeg 默认 UA（Lavf）等，这类客户端改走中转（设置里的「中转 UA 片段」）
    fake_header: bool = False  # 分片前面加了假文件头（比如 PNG），中转时从第一个 TS 同步字节开始转发


@dataclass
class LineSpec:
    """多线路站点上一条已知线路：背后是哪个播放站、默认特性。"""

    host: str  # hosts.py 里的播放站类型；空串表示还不支持
    note: str = ""
    default_enabled: bool = True

    @property
    def supported(self) -> bool:
        return bool(self.host)


@dataclass
class Stream:
    url: str
    expires: int | None
    detail: SourceDetail | None = None  # 取地址时顺带拿到的详情，用来更新元数据


class Site:
    name = ""
    label = ""
    default_domains: list[str] = []
    default_enabled = True
    lookup_verified = False  # lookup 返回的是搜索结果（带番号），按列表项核对就行，不用再抓详情确认
    line_specs: dict[str, LineSpec] = {}  # 多线路站点的已知线路（按默认优先顺序）；单线路站点为空
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

    def variant_of(self, key: str) -> tuple[str, bool]:
        """从站内 key 看出的 (字幕, 是否无码流出)；看不出返回 ('', False)。"""
        return "", False

    @property
    def multi_line(self) -> bool:
        return bool(self.line_specs)

    def line_traits(self, name: str, host: str = "") -> StreamTraits:
        """线路的播放特性：按解析时认出的播放站，没解析过按已知线路的默认播放站。"""
        from .hosts import HOST_TRAITS

        host = host or (self.line_specs.get(name).host if name in self.line_specs else "")
        return HOST_TRAITS.get(host, self.stream)

    async def resolve_line(self, http, name: str, link: str):
        """多线路站点：按详情页给的线路数据取直链，返回 hosts.HostStream。"""
        raise NotImplementedError

    @property
    def can_lookup(self) -> bool:
        """能不能按番号找到本站的影片（补源、现场找源用）。"""
        return type(self).lookup is not Site.lookup or bool(self.key_for("ABC-001"))

    async def lookup(self, sf: SiteFetcher, code: str, uncensored: bool = False,
                     priority: bool = False) -> list[SourceItem]:
        """按番号找本站的影片，返回候选（由 find_by_code 核对）。默认直接拼 key（还没确认存在，要抓详情核对）；
        要搜索的站点覆盖它，原样返回搜索结果。priority：不排限速队列（用户手动查、播放时现场找）。"""
        key = self.key_for(code, uncensored=uncensored)
        return [SourceItem(key=key, code=code, title="", uncensored=uncensored)] if key else []

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
