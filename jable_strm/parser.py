"""Jable（KVS 系统）列表页与详情页解析。纯函数，不做网络请求。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser


class ParseError(Exception):
    """页面结构不符合预期（改版等）。"""


class VideoGone(ParseError):
    """影片已下架：站点返回 200 但只是兜底页，没有播放器。"""


@dataclass
class ListItem:
    video_id: int
    slug: str
    code: str
    title: str
    duration: int | None = None
    thumb_url: str = ""
    preview_url: str = ""
    views: int | None = None
    likes: int | None = None


@dataclass
class ListPage:
    items: list[ListItem]
    last_page: int | None
    block_id: str | None


@dataclass
class VideoDetail:
    video_id: int
    slug: str
    code: str
    title: str
    hls_url: str
    hls_expires: int | None
    cover_url: str = ""
    release_date: str = ""
    quality: str = ""
    views: int | None = None
    favs: int | None = None
    models: list[dict] = field(default_factory=list)
    categories: list[dict] = field(default_factory=list)
    tags: list[dict] = field(default_factory=list)


_CODE_RE = re.compile(r"^([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+)(?=\s|$)")
_SLUG_RE = re.compile(r"/videos/([^/?#]+)/?")
_HLS_RE = re.compile(r"""var\s+hlsUrl\s*=\s*['"]([^'"]+)['"]""")
_HLS_EXPIRES_RE = re.compile(r"/hls/[^/]+/(\d{9,11})/")
_VIDEO_ID_RE = re.compile(r"""videoId:\s*['"]?(\d+)""")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BLOCK_RE = re.compile(r'id="(list_videos_[a-z0-9_]+?)(?:_pagination|_sort_list)?"')


def split_code(title: str, slug: str) -> str:
    """从标题开头取番号，取不到就用 slug。"""
    m = _CODE_RE.match(title.strip())
    return m.group(1).upper() if m else slug.upper()


def parse_duration(text: str) -> int | None:
    """'1:59:51' / '59:51' -> 秒。"""
    parts = text.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs


def parse_count(text: str) -> int | None:
    """'285 871' / '1,974' -> 285871 / 1974。"""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def m3u8_duration(text: str) -> int | None:
    """HLS 媒体播放列表各分片 EXTINF 之和（秒）。"""
    total = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text))
    return round(total) if total else None


def slug_from_url(url: str) -> str | None:
    m = _SLUG_RE.search(urlsplit(url).path)
    return m.group(1).lower() if m else None


def hls_expires(url: str) -> int | None:
    m = _HLS_EXPIRES_RE.search(url)
    return int(m.group(1)) if m else None


def _last_path_part(href: str) -> str:
    return urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]


def parse_list(html: str) -> ListPage:
    doc = LexborHTMLParser(html)
    items: list[ListItem] = []
    for box in doc.css(".video-img-box"):
        link = box.css_first(".detail .title a") or box.css_first("a[href*='/videos/']")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        slug = slug_from_url(href)
        fav = box.css_first("[data-fav-video-id]")
        if not slug or fav is None:
            continue
        title = link.text(strip=True)
        img = box.css_first("img")
        label = box.css_first(".label")
        counts = []
        sub = box.css_first(".sub-title")
        if sub is not None:
            counts = [parse_count(t) for t in sub.text(separator="|").split("|") if t.strip()]
        items.append(
            ListItem(
                video_id=int(fav.attributes["data-fav-video-id"]),
                slug=slug,
                code=split_code(title, slug),
                title=title,
                duration=parse_duration(label.text()) if label else None,
                thumb_url=(img.attributes.get("data-src") or "") if img else "",
                preview_url=(img.attributes.get("data-preview") or "") if img else "",
                views=counts[0] if len(counts) > 0 else None,
                likes=counts[1] if len(counts) > 1 else None,
            )
        )

    last_page = None
    block_id = None
    for a in doc.css(".pagination [data-parameters]"):
        block_id = block_id or a.attributes.get("data-block-id")
        m = re.search(r"from[^:]*:(\d+)", a.attributes.get("data-parameters") or "")
        if m:
            last_page = max(last_page or 0, int(m.group(1)))
    if doc.css_first(".pagination") is not None and last_page is None:
        last_page = 1
    if block_id is None:
        m = _BLOCK_RE.search(html)
        block_id = m.group(1) if m else None
    return ListPage(items=items, last_page=last_page, block_id=block_id)


def parse_detail(html: str, slug: str) -> VideoDetail:
    doc = LexborHTMLParser(html)
    m = _HLS_RE.search(html)
    if not m:
        if "video-info" not in html and "%title%" in html[:3000]:
            raise VideoGone("影片已下架（站点返回兜底页）")
        raise ParseError("详情页没有 hlsUrl")
    hls_url = m.group(1)
    m = _VIDEO_ID_RE.search(html)
    if not m:
        raise ParseError("详情页没有 videoId")
    video_id = int(m.group(1))

    info = doc.css_first("section.video-info")
    if info is None:
        raise ParseError("详情页没有 video-info 区块")
    h4 = info.css_first("h4")
    og_title = doc.css_first('meta[property="og:title"]')
    title = (h4.text(strip=True) if h4 else "") or (og_title.attributes.get("content") if og_title else "") or ""
    og_image = doc.css_first('meta[property="og:image"]')

    models = []
    for a in info.css(".models a.model"):
        named = a.css_first("[title]") or a.css_first("[data-original-title]")
        name = ""
        if named is not None:
            name = named.attributes.get("title") or named.attributes.get("data-original-title") or ""
        name = name or a.text(strip=True)
        models.append({"id": _last_path_part(a.attributes.get("href") or ""), "name": name.strip()})

    categories, tags = [], []
    for a in info.css("h5.tags a"):
        entry = {"slug": _last_path_part(a.attributes.get("href") or ""), "name": a.text(strip=True)}
        (categories if "cat" in (a.attributes.get("class") or "").split() else tags).append(entry)

    release_date = ""
    quality = ""
    right = info.css_first(".header-right")
    if right is not None:
        m = _DATE_RE.search(right.text())
        release_date = m.group(0) if m else ""
        h6 = right.css_first("h6")
        quality = h6.text(strip=True).lstrip("●").strip() if h6 else ""

    views = None
    spans = info.css(".header-left h6 span.mr-3")
    if len(spans) >= 2:
        views = parse_count(spans[1].text())
    fav = info.css_first("button.fav .count")

    return VideoDetail(
        video_id=video_id,
        slug=slug.lower(),
        code=split_code(title, slug),
        title=title,
        hls_url=hls_url,
        hls_expires=hls_expires(hls_url),
        cover_url=(og_image.attributes.get("content") or "") if og_image else "",
        release_date=release_date,
        quality=quality,
        views=views,
        favs=parse_count(fav.text()) if fav else None,
        models=models,
        categories=categories,
        tags=tags,
    )
