"""MissAV：主域的首页、列表被 Cloudflare 挑战，镜像（missav123.com、missav.live）能直接抓。

- 详情页 /cn/{key}，`dmN/` 前缀可省；不存在返回 404。每个变体是独立的 key：
  {番号}、{番号}-chinese-subtitle、{番号}-english-subtitle、{番号}-uncensored-leak。
- 播放地址在 packer（eval(function(p,a,c,k,e,d)…)）里，解包后是 surrit.com/{uuid}/playlist.m3u8：
  不带签名、不过期，但 CDN 要 Referer 是 missav 的域名、还要浏览器 TLS 指纹，所以只能由本服务中转。
  分片叫 video0.jpeg，实际是 TS。
- 列表：/cn/new、/cn/release、/cn/chinese-subtitle、/cn/uncensored-leak、/cn/search/{关键词}、
  /cn/genres|actresses|makers/{名称}；?page=N&sort=…，每页 12 条，最多 2000 页。
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..codes import code_key
from ..errors import ParseError
from ..parser import parse_duration
from .base import ListPage, Site, SourceDetail, SourceItem, StreamTraits

VARIANTS = (("-uncensored-leak", "uncensored"), ("-chinese-subtitle", "zh"), ("-english-subtitle", "en"))
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,80}$")  # 一本道等日期型番号带下划线：100826_001
_PATH_PREFIX_RE = re.compile(r"^/(?:dm\d+/)?(?:(?:cn|en|ja|ko|ms|th|de|fr|vi|id|fil|pt|zh)/)?")
_PACKER_RE = re.compile(r"}\('(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", re.S)
_SOURCE_RE = re.compile(r"source\s*=\s*\\?'(https://[^'\\]+?\.m3u8)")
_UUID_RE = re.compile(r"surrit\.com\\?/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})")
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_LAST_PAGE_RE = re.compile(r"/\s*(\d+)")
UNCENSORED_LISTS = {
    "heyzo": "HEYZO", "tokyohot": "东京热", "1pondo": "一本道",
    "caribbeancom": "Caribbeancom", "caribbeancompr": "Caribbeancompr",
    "10musume": "10musume", "pacopacomama": "pacopacomama", "gachinco": "Gachinco",
    "xxxav": "XXX-AV", "marriedslash": "人妻斩",
}


def split_variant(key: str) -> tuple[str, str, bool]:
    """'ssis-001-chinese-subtitle' -> ('ssis-001', 'zh', False)。"""
    subtitle, uncensored = "", False
    changed = True
    while changed:
        changed = False
        for suffix, kind in VARIANTS:
            if key.endswith(suffix):
                key = key[: -len(suffix)]
                if kind == "uncensored":
                    uncensored = True
                else:
                    subtitle = subtitle or kind
                changed = True
    return key, subtitle, uncensored


def _base_n(word: str, base: int) -> int:
    n = 0
    for ch in word:
        d = _DIGITS.index(ch)
        if d >= base:
            raise ValueError(word)
        n = n * base + d
    return n


def unpack(html: str) -> list[str]:
    """解开页面里所有 Dean Edwards packer 脚本（进制最大 62）。"""
    out = []
    for m in _PACKER_RE.finditer(html):
        payload, base, words = m.group(1).replace("\\'", "'"), int(m.group(2)), m.group(4).split("|")

        def repl(w: re.Match, base=base, words=words) -> str:
            s = w.group(0)
            try:
                i = _base_n(s, base)
            except ValueError:
                return s
            return words[i] if i < len(words) and words[i] else s

        out.append(re.sub(r"\b\w+\b", repl, payload))
    return out


def playlist_url(html: str) -> str | None:
    for js in unpack(html):
        if m := _SOURCE_RE.search(js):
            return m.group(1)
    if m := _UUID_RE.search(html):  # 解包失败时：预览缩略图等地方也带着 uuid
        return f"https://surrit.com/{m.group(1)}/playlist.m3u8"
    return None


def _meta(doc: LexborHTMLParser, prop: str) -> str:
    el = doc.css_first(f'meta[property="{prop}"]')
    return (el.attributes.get("content") or "").strip() if el else ""


def _last_part(href: str) -> str:
    return unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])


def _person(name: str) -> str:
    """'濑绪凛 (瀬緒凛)' -> '瀬緒凛'：括号里是原名，和其他站的写法一致。"""
    m = re.search(r"\(([^()]+)\)\s*$", name)
    return (m.group(1) if m else name).strip()


class MissAVSite(Site):
    name = "missav"
    label = "MissAV"
    default_domains = ["https://missav123.com", "https://missav.live", "https://njavtv.com", "https://missav.ws"]
    stream = StreamTraits(direct=False, expires=False, headers={"Referer": "https://missav.ws/"},
                          disguised_segments=True)
    sorts = {
        "": "默认",
        "published_at": "最近更新",
        "released_at": "发行日期",
        "saved": "最多收藏",
        "today_views": "今日热门",
        "weekly_views": "本周热门",
        "monthly_views": "本月热门",
        "views": "最多观看",
    }
    presets = [
        {"name": "最近更新", "source": "/cn/new", "sort": ""},
        {"name": "最新发行", "source": "/cn/release", "sort": ""},
        {"name": "中文字幕", "source": "/cn/chinese-subtitle", "sort": ""},
        {"name": "无码流出", "source": "/cn/uncensored-leak", "sort": ""},
        {"name": "FC2", "source": "/cn/fc2", "sort": "published_at"},
        *[{"name": f"原生无码 · {label}", "source": f"/cn/{key}", "sort": "published_at"}
          for key, label in UNCENSORED_LISTS.items()],
        {"name": "搜索", "source": "/cn/search/<关键词>", "sort": ""},
        {"name": "女优", "source": "/cn/actresses/<名字>", "sort": ""},
        {"name": "类型", "source": "/cn/genres/<名称>", "sort": ""},
        {"name": "发行商", "source": "/cn/makers/<名称>", "sort": ""},
    ]
    default_sort = ""
    source_hint = ("最近更新 /cn/new、最新发行 /cn/release、中文字幕 /cn/chinese-subtitle、无码流出 /cn/uncensored-leak、"
                   "搜索 /cn/search/关键词、女优 /cn/actresses/名字，直接粘贴站点网址也行；每页 12 部，最多 2000 页")

    def detail_path(self, key: str) -> str:
        return f"/cn/{key}"

    def key_from_url(self, url: str) -> str | None:
        path = urlsplit(url if "://" in url else "https://x/" + url.lstrip("/")).path
        key = _PATH_PREFIX_RE.sub("", path).strip("/").lower()
        return key if _KEY_RE.fullmatch(key) else None

    def key_for(self, code: str, subtitle: str = "", uncensored: bool = False) -> str | None:
        ck = code_key(code)
        if not ck:
            return None
        base = f"fc2-ppv-{ck.split('-', 1)[1]}" if ck.startswith("FC2PPV-") else code.strip().lower()
        if not _KEY_RE.fullmatch(base):
            return None
        if uncensored:
            return base + "-uncensored-leak"
        return base + {"zh": "-chinese-subtitle", "en": "-english-subtitle"}.get(subtitle, "")

    def variant_of(self, key: str) -> tuple[str, bool]:
        _, subtitle, uncensored = split_variant(key)
        return subtitle, uncensored

    def normalize_source(self, value: str) -> str:
        """'https://missav.ws/dm514/cn/genres/巨乳?page=2' -> '/cn/genres/%E5%B7%A8%E4%B9%B3'。"""
        value = value.strip()
        if not value:
            raise ValueError("列表地址不能为空")
        path = urlsplit(value).path if "://" in value else value.split("?", 1)[0]
        rest = _PATH_PREFIX_RE.sub("", "/" + path.strip("/")).strip("/")
        if not rest:
            raise ValueError(f"不是列表地址：{value}")
        if "/" not in rest and rest not in ("new", "release", "chinese-subtitle", "uncensored-leak", "today-hot",
                                            "weekly-hot", "monthly-hot", "fc2", "siro", "luxu", "gana", "maan",
                                            *UNCENSORED_LISTS):
            raise ValueError(f"看起来是影片地址，不是列表地址：{value}")
        parts = [quote(unquote(p), safe="") for p in rest.split("/")]
        return "/cn/" + "/".join(parts)

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        return f"{source}?page={page}" + (f"&sort={sort}" if sort else "")

    def parse_list(self, html: str) -> ListPage:
        doc = LexborHTMLParser(html)
        items: list[SourceItem] = []
        seen: set[str] = set()
        for box in doc.css("div.thumbnail"):
            link = box.css_first("div.my-2 a[href]") or box.css_first("a[href]")
            if link is None:
                continue
            key = self.key_from_url(link.attributes.get("href") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            base, subtitle, uncensored = split_variant(key)
            title = link.text(strip=True)
            img = box.css_first("img[data-src]")
            dur = box.css_first("span.right-1")
            badges = " ".join(s.text(strip=True) for s in box.css("span.left-1"))
            if not subtitle and "中文字幕" in badges:
                subtitle = "zh"
            code = base.upper()
            items.append(SourceItem(
                key=key, code=code, title=title or (img.attributes.get("alt") if img else "") or code,
                duration=parse_duration(dur.text()) if dur else None,
                thumb_url=(img.attributes.get("data-src") or "") if img else "",
                subtitle=subtitle, uncensored=uncensored,
            ))
        pages = [int(x) for x in _PAGE_RE.findall(html)]
        for el in doc.css("#price-currency"):
            if m := _LAST_PAGE_RE.search(el.text()):
                pages.append(int(m.group(1)))
        last_page = max(pages) if pages else (1 if items else None)
        return ListPage(items=items, last_page=last_page)

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        doc = LexborHTMLParser(html)
        stream = playlist_url(html)
        title = _meta(doc, "og:title")
        if not stream:
            raise ParseError("详情页没有播放地址")
        base, subtitle, uncensored = split_variant(key)
        code = base.upper()
        models, categories, maker, director, series, release = [], [], "", "", "", ""
        for box in doc.css("div.text-secondary"):
            label = box.css_first("span")
            text = label.text(strip=True) if label else ""
            if text.startswith(("番号", "番號")):
                val = box.css_first("span.font-medium")
                if val is not None and val.text(strip=True):
                    # 变体页的番号也带后缀（CJOD-538-CHINESE-SUBTITLE），去掉才能和其他站对上
                    code = split_variant(val.text(strip=True).lower())[0].upper()
            if (t := box.css_first("time[datetime]")) is not None and not release:
                release = (t.attributes.get("datetime") or "")[:10]  # 「发行日期」；og:video:release_date 是上架日期
            for a in box.css("a[href]"):
                href = a.attributes.get("href") or ""
                name = a.text(strip=True)
                if "/actresses/" in href:
                    models.append({"id": _last_part(href), "name": _person(name)})
                elif "/genres/" in href:
                    categories.append({"slug": _last_part(href), "name": name})
                elif "/makers/" in href:
                    maker = maker or name
                elif "/directors/" in href:
                    director = director or name
                elif "/series/" in href:
                    series = series or name
        if not subtitle and any(c["slug"] == "chinese-subtitle" or c["name"] == "中文字幕" for c in categories):
            subtitle = "zh"
        # 同一部片在本站的其他版本（页面上的「切换中字 / 无码」链接）
        variants = sorted({m.group(1) for m in re.finditer(
            rf'href="https?://[^"/]+/(?:dm\d+/)?(?:[a-z]{{2,3}}/)?({re.escape(base)}(?:-[a-z]+-[a-z]+)+)"', html)
            if m.group(1) != key and split_variant(m.group(1))[0] == base})
        duration = _meta(doc, "og:video:duration")
        return SourceDetail(
            key=key,
            code=code,
            title=title or code,
            stream_url=stream,
            stream_expires=None,
            subtitle=subtitle,
            uncensored=uncensored,
            duration=int(duration) if duration.isdigit() else None,
            cover_url=_meta(doc, "og:image"),
            release_date=(release or _meta(doc, "og:video:release_date"))[:10],
            models=models,
            categories=categories,
            maker=maker,
            director=director,
            series=series,
            variants=variants,
        )
