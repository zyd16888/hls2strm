"""站点注册表。新增站点：写一个 Site 子类，加进 SITES。"""

from __future__ import annotations

from .base import ListPage, Site, SourceDetail, SourceItem, Stream, StreamTraits
from .jable import JableSite
from .missav import MissAVSite

SITES: dict[str, Site] = {s.name: s for s in (JableSite(), MissAVSite())}


def get_site(name: str) -> Site:
    site = SITES.get(name or "jable")
    if site is None:
        raise ValueError(f"未知站点：{name}")
    return site


__all__ = ["SITES", "ListPage", "Site", "SourceDetail", "SourceItem", "Stream", "StreamTraits", "get_site"]
