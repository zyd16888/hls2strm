"""123AV（123av.com，原 nJAV）：curl_cffi 直接能抓，不挑战。njav.tv、123av.me 只剩「已迁到 123av.com」的空壳页，
njavtv.com、123av.org 是 MissAV 的镜像，都不是这个站。

- 详情页 /cn/v/{key}，key 就是番号小写（FC2 写成 fc2-ppv-{n}），无码泄露版加 -uncensored-leaked；没有中字版。
  不存在返回 404，所以按番号直接拼。
- 列表：/cn/new、/cn/recent、/cn/hot、/cn/all、/cn/censored、/cn/uncensored、/cn/uncensored-leaked，
  女优 /cn/actresses/{slug}、发行商 /cn/makers/{slug}、类型 /cn/genres/{slug}、标签 /cn/tags/{slug}、系列 /cn/series/{id}，
  搜索 /cn/search?keyword=…；筛选 type、year、actress，排序 sort，翻页 page；每页 12 条，页码最多显示 5000 页。
- 播放：详情页 x-data="player(JSON.parse('[分集]'), …)"，每集是嵌入页 https://{嵌入站}/e/{id}；
  请求 {嵌入站}/stream?id={id} 返回 {"media": {"stream": m3u8}}。m3u8 不带签名、不过期，CDN 只认 Referer 是嵌入站
  （不看 TLS 指纹和 UA），所以只能中转；嵌入站和 CDN 都是一次性域名，Referer 跟着嵌入页走，按多线路站点处理。
  分片扩展名在 .css、.svg、.woff2、.vtt 等之间轮换，内容是 TS。分成多集的片子拼不成一个流，跳过。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..codes import code_key
from ..errors import FetchError, NotFound, ParseError, VideoGone
from ..parser import parse_duration
from .base import LineSpec, ListPage, Site, SourceDetail, SourceItem, StreamTraits
from .hosts import HostStream

if TYPE_CHECKING:
    from ..fetcher import Fetcher

LINE = "123AV"
LEAK = "-uncensored-leaked"
LOCALES = {"en", "ja", "cn", "tw", "ko", "th", "ms", "vi", "id", "fil", "hi", "de", "fr"}
LISTS = {"new", "recent", "hot", "all", "censored", "uncensored", "uncensored-leaked"}
TERMS = {"actresses", "makers", "genres", "tags", "series"}
FILTERS = ("keyword", "type", "year", "actress")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,80}$")  # 一本道等日期型番号带下划线：100826_001
_PLAYER_RE = re.compile(r"JSON\.parse\('((?:[^'\\]|\\.)*)'\)")
_JS_ESCAPE_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)")
_EMBED_RE = re.compile(r"/e/([A-Za-z0-9_]+)")
_COVER_RE = re.compile(r"url\('([^']+)'\)")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PAGE_RE = re.compile(r"[?&]page=(\d+)")


def split_variant(key: str) -> tuple[str, bool]:
    """'ssis-001-uncensored-leaked' -> ('ssis-001', True)。"""
    return (key[: -len(LEAK)], True) if key.lower().endswith(LEAK) else (key, False)


def _js_string(s: str) -> str:
    """单引号 JS 字符串字面量的内容 -> 字符串（\\uXXXX、\\/、\\\\ 等转义）。"""
    def repl(m: re.Match) -> str:
        e = m.group(1)
        if e[0] == "u" and len(e) == 5:
            return chr(int(e[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(e, e)

    return _JS_ESCAPE_RE.sub(repl, s)


def episodes(html: str) -> list[dict] | None:
    """详情页播放器的分集：[{number, name, url（嵌入页）}]；没有播放器返回 None。"""
    doc = LexborHTMLParser(html)
    el = doc.css_first('[x-data^="player("]')
    m = _PLAYER_RE.search(el.attributes.get("x-data") or "") if el is not None else None
    if not m:
        return None
    try:
        eps = json.loads(_js_string(m.group(1)))
    except ValueError:
        return None
    return [e for e in eps if isinstance(e, dict) and e.get("url")]


def _code_title(text: str, code: str) -> str:
    """'SSIS-001-Uncensored-Leaked — 标题' -> 'SSIS-001 标题'（和其他站的写法一致）。"""
    rest = text.split(" — ", 1)[1].strip() if " — " in text else ""
    return f"{code} {rest}" if rest else code


def _slug(href: str) -> str:
    return unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])


class AV123Site(Site):
    name = "123av"
    label = "123AV"
    default_domains = ["https://123av.com"]
    url_hosts = ["www.123av.com", "njav.tv", "www.njav.tv", "123av.me"]
    stream =StreamTraits(direct=False, expires=False, disguised_segments=True)
    line_specs = {
        LINE: LineSpec("av123", "自家播放器，m3u8 不过期；CDN 只认嵌入站的 Referer，分片扩展名伪装，只能中转"),
    }
    sorts = {
        "": "默认",
        "release_date": "发布日期",
        "recent": "最近添加",
        "hot": "热门",
        "today": "今日观看",
        "week": "每周观看",
        "month": "每月观看",
        "views": "最受欢迎",
        "follows": "最多关注",
        "longest": "最长",
    }
    presets = [
        {"name": "新发布", "source": "/cn/new", "sort": ""},
        {"name": "最近添加", "source": "/cn/recent", "sort": ""},
        {"name": "无码泄露", "source": "/cn/uncensored-leaked", "sort": ""},
        {"name": "有码", "source": "/cn/censored", "sort": ""},
        {"name": "无码", "source": "/cn/uncensored", "sort": ""},
        {"name": "本周热门", "source": "/cn/all", "sort": "week"},
        {"name": "搜索", "source": "/cn/search/<关键词>", "sort": ""},
        {"name": "女优", "source": "/cn/actresses/<slug>", "sort": ""},
        {"name": "发行商", "source": "/cn/makers/<slug>", "sort": ""},
        {"name": "类型", "source": "/cn/genres/<slug>", "sort": ""},
        {"name": "标签", "source": "/cn/tags/<slug>", "sort": ""},
    ]
    default_sort = ""
    source_hint = ("新发布 /cn/new、无码泄露 /cn/uncensored-leaked、女优 /cn/actresses/xxx、发行商 /cn/makers/xxx、"
                   "搜索 /cn/search/关键词，可带 ?year=2024&type=censored 筛选，直接粘贴站点网址也行；"
                   "每页 12 部，最多 5000 页")

    def detail_path(self, key: str) -> str:
        return f"/cn/v/{key}"

    def key_from_url(self, url: str) -> str | None:
        segs = [p for p in urlsplit(url if "://" in url else "https://x/" + url.lstrip("/")).path.split("/") if p]
        if segs and segs[0] in LOCALES:
            segs = segs[1:]
        if len(segs) != 2 or segs[0] != "v":
            return None
        key = segs[1].lower()
        return key if _KEY_RE.fullmatch(key) else None

    def key_for(self, code: str, subtitle: str = "", uncensored: bool = False) -> str | None:
        ck = code_key(code)
        if not ck:
            return None
        base = f"fc2-ppv-{ck.split('-', 1)[1]}" if ck.startswith("FC2PPV-") else code.strip().lower()
        if not _KEY_RE.fullmatch(base):
            return None
        return base + (LEAK if uncensored else "")

    def variant_of(self, key: str) -> tuple[str, bool]:
        return "", split_variant(key)[1]

    def normalize_source(self, value: str) -> str:
        """'https://123av.com/en/censored?year=2024&page=3&sort=views' -> '/cn/censored?year=2024'；
        '/cn/search/SSIS' -> '/cn/search?keyword=SSIS'。排序、页码不进列表地址（任务里另有排序）。"""
        value = value.strip()
        if not value:
            raise ValueError("列表地址不能为空")
        parts = urlsplit(value if "://" in value else "https://x/" + value.lstrip("/"))
        segs = [unquote(p) for p in parts.path.split("/") if p]
        if segs and segs[0] in LOCALES:
            segs = segs[1:]
        q = {k: v[0].strip() for k, v in parse_qs(parts.query).items() if v and v[0].strip()}
        if segs[:1] == ["search"] and len(segs) <= 2:
            if len(segs) == 2:
                q["keyword"] = segs[1].strip()
            if not q.get("keyword"):
                raise ValueError(f"搜索地址缺关键词：{value}")
            segs = ["search"]
        elif not ((len(segs) == 1 and segs[0] in LISTS) or (len(segs) == 2 and segs[0] in TERMS)):
            hint = "看起来是影片地址，" if segs[:1] == ["v"] else ""
            raise ValueError(f"{hint}不是列表地址：{value}")
        path = "/cn/" + "/".join(quote(p, safe="") for p in segs)
        filters = [(k, q[k]) for k in FILTERS if q.get(k)]
        return path + ("?" + urlencode(filters, quote_via=quote) if filters else "")

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        path, _, query = source.partition("?")
        params = parse_qsl(query) + ([("sort", sort)] if sort else []) + ([("page", str(page))] if page > 1 else [])
        return path + ("?" + urlencode(params, quote_via=quote) if params else "")

    def parse_list(self, html: str) -> ListPage:
        doc = LexborHTMLParser(html)
        items: list[SourceItem] = []
        seen: set[str] = set()
        for card in doc.css("div.card"):
            link = card.css_first("a.card__link[href]")
            key = self.key_from_url(link.attributes.get("href") or "") if link is not None else None
            if not key or key in seen:
                continue
            seen.add(key)
            base, uncensored = split_variant(key)
            code = base.upper()
            img = card.css_first("img.card__img")
            dur = card.css_first("span.card__dur")
            items.append(SourceItem(
                key=key, code=code, title=_code_title(link.text(strip=True), code),
                duration=parse_duration(dur.text()) if dur else None,
                thumb_url=(img.attributes.get("src") or "") if img else "", uncensored=uncensored,
            ))
        pager = doc.css_first("div.pager")
        pages = [int(x) for x in _PAGE_RE.findall(pager.html or "")] if pager is not None else []
        if (total := doc.css_first("span.pager__total")) is not None and (m := re.search(r"\d+", total.text())):
            pages.append(int(m.group(0)))
        return ListPage(items=items, last_page=max(pages) if pages else (1 if items else None))

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        eps = episodes(html)
        if eps is None:
            raise ParseError("详情页没有播放器")
        if not eps:
            raise VideoGone("影片没有可播放的源（页面提示 No playable source）")
        if len(eps) > 1:
            raise ParseError(f"分成 {len(eps)} 集，拼不成一个流")
        doc = LexborHTMLParser(html)
        base, uncensored = split_variant(key)
        code, release, duration = base.upper(), "", None
        models, categories, tags, maker, series = [], [], [], "", ""
        for row in doc.css("dl.watch__info div.watch__info-row"):
            dt, dd = row.css_first("dt"), row.css_first("dd")
            if dd is None:
                continue
            label, text = (dt.text(strip=True) if dt else ""), dd.text(strip=True)
            if label in ("代码", "Code") and text:
                code = split_variant(text.lower())[0].upper()  # 变体页写成 SSIS-001-Uncensored-Leaked
            elif _DATE_RE.fullmatch(text):
                release = text
            elif re.fullmatch(r"\d+(?::\d{2}){1,2}", text):
                duration = parse_duration(text)
            for a in dd.css("a[href]"):
                href, name = a.attributes.get("href") or "", a.text(strip=True)
                if "/actresses/" in href:
                    models.append({"id": _slug(href), "name": name})
                elif "/genres/" in href:
                    categories.append({"slug": _slug(href), "name": name})
                elif "/tags/" in href:
                    tags.append({"slug": _slug(href), "name": name})
                elif "/makers/" in href:
                    maker = maker or name
                elif "/series/" in href:
                    series = series or name
        h1 = doc.css_first("h1.watch__title")
        player = doc.css_first("div.player")
        cover = _COVER_RE.search(player.attributes.get("style") or "") if player is not None else None
        return SourceDetail(
            key=key, code=code, title=_code_title(h1.text(strip=True) if h1 else "", code), uncensored=uncensored,
            duration=duration, cover_url=cover.group(1) if cover else "", release_date=release, models=models,
            categories=categories, tags=tags, maker=maker, series=series, lines=[(LINE, eps[0]["url"])],
        )

    async def resolve_line(self, http: Fetcher, name: str, link: str) -> HostStream:
        """嵌入页 https://{嵌入站}/e/{id} → {嵌入站}/stream?id={id} 拿 m3u8；中转时 Referer 带嵌入站。"""
        parts = urlsplit(link)
        m = _EMBED_RE.search(parts.path)
        if not m:
            raise ParseError(f"线路 {name} 的嵌入页地址认不出：{link[:80]}")
        origin = f"{parts.scheme}://{parts.netloc}"
        resp = await http.fetch(f"{origin}/stream?id={m.group(1)}", headers={"Referer": link})
        if resp.status_code == 404:
            raise NotFound(f"嵌入站没有这个视频（{parts.netloc}/e/{m.group(1)}）")
        if resp.status_code != 200:
            raise FetchError(f"嵌入站返回 HTTP {resp.status_code}")
        try:
            url = ((resp.json() or {}).get("media") or {}).get("stream") or ""
        except ValueError:
            url = ""
        if not url:
            raise ParseError("嵌入站没给播放地址")
        return HostStream(url, None, "av123", referer=origin + "/")
