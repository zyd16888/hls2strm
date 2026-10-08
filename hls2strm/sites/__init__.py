"""站点注册表。新增站点：写一个 Site 子类，加进 SITES。"""

from __future__ import annotations

from ..codes import code_key
from ..errors import NotFound, VideoGone
from .base import LineSpec, ListPage, Site, SourceDetail, SourceItem, Stream, StreamTraits
from .jable import JableSite
from .missav import MissAVSite
from .javguru import JavGuruSite
from .javmost import JavMostSite
from .supjav import SupJavSite

SITES: dict[str, Site] = {s.name: s for s in (JableSite(), MissAVSite(), SupJavSite(), JavGuruSite(), JavMostSite())}


def get_site(name: str) -> Site:
    site = SITES.get(name or "jable")
    if site is None:
        raise ValueError(f"未知站点：{name}")
    return site


async def find_by_code(site: Site, sf, code: str, uncensored: bool = False,
                       priority: bool = False) -> list[SourceItem | SourceDetail]:
    """按番号在站点上找影片，结果都核对过番号和是否无码流出。

    搜索类站点（lookup_verified）返回列表项；拼地址类站点抓详情核对后返回详情（站点可能把写错的番号纠正到别的片）。
    """
    ck = code_key(code)
    out: list[SourceItem | SourceDetail] = []
    for item in await site.lookup(sf, code, uncensored, priority=priority):
        if site.lookup_verified:
            out.append(item)
            continue
        try:
            d = await site.fetch_detail(sf, item.key, priority=priority)
        except (NotFound, VideoGone):
            continue
        if code_key(d.code) == ck and d.uncensored == uncensored:
            out.append(d)
    return out


__all__ = ["SITES", "LineSpec", "ListPage", "Site", "SourceDetail", "SourceItem", "Stream", "StreamTraits",
           "find_by_code", "get_site"]
