"""Jable（KVS 系统）：列表走异步块接口，详情页内联 hlsUrl（约 3 小时过期，不绑 IP，可以 302）。"""

from __future__ import annotations

from .. import parser, sources
from ..codes import code_key
from .base import ListPage, Site, SourceDetail, SourceItem, StreamTraits


class JableSite(Site):
    name = "jable"
    label = "Jable"
    default_domains = ["https://fs1.app", "https://jable.tv"]
    stream = StreamTraits(direct=True, expires=True, ua_block=True)
    sorts = sources.SORTS
    presets = sources.PRESETS
    default_sort = "post_date"
    source_hint = ("分类 /categories/xxx/、标签 /tags/xxx/、女优 /models/xxx/、搜索 /search/关键词/、热门 /hot/，"
                   "直接粘贴站点网址也行")

    def detail_path(self, key: str) -> str:
        return f"/videos/{key}/"

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        d = parser.parse_detail(html, key)
        cats = {c["slug"] for c in d.categories}
        return SourceDetail(
            key=d.slug,
            code=d.code,
            title=d.title,
            stream_url=d.hls_url,
            stream_expires=d.hls_expires,
            site_vid=str(d.video_id),
            subtitle="zh" if "中文字幕" in d.quality or "chinese-subtitle" in cats else "",
            uncensored="uncensored" in cats,
            cover_url=d.cover_url,
            release_date=d.release_date,
            quality=d.quality,
            views=d.views,
            favs=d.favs,
            models=d.models,
            categories=d.categories,
            tags=d.tags,
        )

    def parse_list(self, html: str) -> ListPage:
        lp = parser.parse_list(html)
        items = [
            SourceItem(key=it.slug, code=it.code, title=it.title, site_vid=str(it.video_id), duration=it.duration,
                       thumb_url=it.thumb_url, preview_url=it.preview_url, views=it.views, likes=it.likes)
            for it in lp.items
        ]
        return ListPage(items=items, last_page=lp.last_page, block_id=lp.block_id)

    def normalize_source(self, value: str) -> str:
        return sources.normalize_source(value)

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        return sources.page_url(source, page, sort, block_id)

    def key_from_url(self, url: str) -> str | None:
        return parser.slug_from_url(url)

    def key_for(self, code: str, subtitle: str = "", uncensored: bool = False) -> str | None:
        key = code_key(code)
        if key.startswith("FC2PPV-"):
            return "fc2ppv-" + key.split("-", 1)[1]  # Jable 的 FC2 写法
        return code.strip().lower() or None
