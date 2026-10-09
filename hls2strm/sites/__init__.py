"""站点注册表。新增站点：写一个 Site 子类，加进 SITES。"""

from __future__ import annotations

from ..codes import code_key
from ..errors import NotFound, VideoGone
from .base import LineSpec, ListPage, Site, SourceDetail, SourceItem, Stream, StreamTraits
from .av123 import AV123Site
from .jable import JableSite
from .missav import MissAVSite
from .javguru import JavGuruSite
from .javmost import JavMostSite
from .supjav import SupJavSite

SITES: dict[str, Site] = {s.name: s for s in (JableSite(), MissAVSite(), SupJavSite(), JavGuruSite(), JavMostSite(),
                                              AV123Site())}


def get_site(name: str) -> Site:
    site = SITES.get(name or "jable")
    if site is None:
        raise ValueError(f"未知站点：{name}")
    return site


async def find_by_code(site: Site, sf, code: str, uncensored: bool = False, priority: bool = False,
                       notes: list[str] | None = None) -> list[SourceItem | SourceDetail]:
    """按番号在站点上找影片，结果都核对过番号和是否无码流出。

    搜索类站点（lookup_verified）返回列表项；拼地址类站点抓详情核对后返回详情（站点可能把写错的番号纠正到别的片）。
    notes：追加查找过程（查了哪个地址、搜索结果为什么不算），写日志用。
    """
    ck = code_key(code)
    notes = [] if notes is None else notes
    items, domain = await site.lookup(sf, code, uncensored, priority=priority)
    if site.lookup_verified:
        out: list[SourceItem | SourceDetail] = []
        rest: list[SourceItem] = []
        for it in items:
            (out if code_key(it.code) == ck and it.uncensored == uncensored else rest).append(it)
        notes.append((f"{domain} " if domain else "") + f"按番号 {code} 搜到 {len(items)} 条"
                     + (f"，不是这部的：{_brief(rest)}" if rest else ""))
        return out
    if not items:
        notes.append(f"番号 {code} 拼不出站内地址")
    found: list[SourceItem | SourceDetail] = []
    for item in items:
        try:
            d = await site.fetch_detail(sf, item.key, priority=priority)
        except (NotFound, VideoGone) as e:
            notes.append(f"{site.detail_path(item.key)} {'已下架' if isinstance(e, VideoGone) else '不存在'}")
            continue
        if code_key(d.code) != ck:
            notes.append(f"{site.detail_path(item.key)} 是 {d.code}")
        elif d.uncensored != uncensored:
            notes.append(f"{site.detail_path(item.key)} {'是' if d.uncensored else '不是'}无码流出版")
        else:
            found.append(d)
    return found


def _brief(items: list[SourceItem], limit: int = 5) -> str:
    """'SSIS-001（无码流出）、SSIS-010 等 8 条'。"""
    names = [it.code + ("（无码流出）" if it.uncensored else "") for it in items[:limit]]
    return "、".join(names) + (f" 等 {len(items)} 条" if len(items) > limit else "")


__all__ = ["SITES", "LineSpec", "ListPage", "Site", "SourceDetail", "SourceItem", "Stream", "StreamTraits",
           "find_by_code", "get_site"]
