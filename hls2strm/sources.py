"""列表来源：把用户输入的列表地址规范化，并构造 KVS 异步块分页地址。"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlencode, urlsplit

LATEST = "/latest-updates/"

SORTS = {
    "": "默认",
    "post_date": "最近更新",
    "post_date_and_popularity": "近期最佳",
    "video_viewed": "最多观看",
    "most_favourited": "最高收藏",
    "video_viewed_today": "今日热门",
    "video_viewed_week": "本周热门",
    "video_viewed_month": "本月热门",
}

PRESETS = [
    {"name": "最新更新（全站）", "source": LATEST, "sort": "post_date"},
    {"name": "热门", "source": "/hot/", "sort": "video_viewed_week"},
    {"name": "分类", "source": "/categories/<slug>/", "sort": "post_date"},
    {"name": "标签", "source": "/tags/<slug>/", "sort": "post_date"},
    {"name": "女优", "source": "/models/<id>/", "sort": "post_date"},
    {"name": "搜索", "source": "/search/<关键词>/", "sort": ""},
]

_PAGE_SUFFIX_RE = re.compile(r"/\d+/$")


def normalize_source(value: str) -> str:
    """'https://jable.tv/tags/creampie/3/?x=1' -> '/tags/creampie/'。"""
    value = value.strip()
    if not value:
        raise ValueError("列表地址不能为空")
    path = urlsplit(value).path if "://" in value else value.split("?", 1)[0]
    path = "/" + path.strip("/") + "/"
    path = _PAGE_SUFFIX_RE.sub("/", path)
    if path == "/" or path.startswith("/videos/"):
        raise ValueError(f"不是列表地址：{value}")
    parts = [quote(unquote(p), safe="") for p in path.strip("/").split("/")]
    return "/" + "/".join(parts) + "/"


def default_block_id(source: str) -> str:
    if source.startswith("/search/"):
        return "list_videos_videos_list_search_result"
    if source.startswith(LATEST):
        return "list_videos_latest_videos_list"
    return "list_videos_common_videos_list"


def page_url(source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
    params: dict[str, str | int] = {
        "mode": "async",
        "function": "get_block",
        "block_id": block_id or default_block_id(source),
    }
    if source.startswith("/search/"):
        params["q"] = unquote(source.strip("/").split("/", 1)[1])
    if sort:
        params["sort_by"] = sort
    params["from"] = page
    return f"{source}?{urlencode(params)}"
