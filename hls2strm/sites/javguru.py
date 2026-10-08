"""JavGuru（jav.guru，WordPress）：curl_cffi 直接能抓（挂在 Cloudflare 后面，但目前不挑战）。

- 详情页 /{文章 id}/{slug}/，slug 写错会按 id 跳正，所以 key 用文章 id。
- 标题 `[番号] 英文标题`；方括号里 -SUBS 是英文字幕版，-MR 是无码破解版；没有中字。
- 列表：最新 /page/N/，分类 /category/{english-subbed|decensored|jav|amateur|idol|4k}/，女优 /actress/x/，
  发行商 /maker/x/，标签 /tag/x/，搜索 /?s=关键词；每页 24 条，总页数在 span.pages（Page 1 of 5,696）。
- 线路：详情页按钮 a[data-localize=V]（文字 STREAM XX），脚本 `var V = {…"iframe_url":"base64"}`。
  解出 https://jav.guru/searcho/?{L}d={HEX}&bg=…，把 HEX 倒过来请求 /searcho/?{L}r={倒过来的 HEX}，
  302 的 Location 就是嵌入页（不用 Referer）。
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..codes import code_key
from ..errors import FetchError, ParseError
from ..quality import from_labels
from .base import LineSpec, ListPage, Site, SourceDetail, SourceItem, StreamTraits
from .hosts import HostStream, resolve_embed

if TYPE_CHECKING:
    from ..fetcher import Fetcher, SiteFetcher

_KEY_RE = re.compile(r"^/(\d+)/")
_TITLE_RE = re.compile(r"^\s*\[([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*?-\d+)(?:-(SUBS|MR))?\]", re.I)
_PAGES_RE = re.compile(r"of\s+([\d,]+)")
_PAGE_RE = re.compile(r"/page/(\d+)/?$")
_LINE_BTN_RE = re.compile(r'data-localize="(\w+)"[^>]*>\s*([^<]+?)\s*</a>')
_LINE_DATA_RE = re.compile(r"\?(\w)d=([0-9a-f]+)")
_THUMB_SIZE_RE = re.compile(r"-\d+x\d+(?=\.\w+$)")


def parse_title(title: str) -> tuple[str, str, bool]:
    """'[START-628-SUBS] …' -> ('START-628', 'en', False)；'[FNS-258-MR] …' -> ('FNS-258', '', True)。"""
    m = _TITLE_RE.match(title)
    if not m:
        return "", "", False
    tag = (m.group(2) or "").upper()
    return m.group(1).upper(), "en" if tag == "SUBS" else "", tag == "MR"


class JavGuruSite(Site):
    name = "javguru"
    label = "JavGuru"
    default_domains = ["https://jav.guru"]
    stream = StreamTraits(direct=True, expires=True, ip_bound=True)
    lookup_verified = True
    line_specs = {
        "SB": LineSpec("vidhide", "StreamHG，m3u8，约 36 小时，带出口 ASN"),
        "VO": LineSpec("voe", "m3u8，约 4 小时，带出口 IP 前两段"),
        "JK": LineSpec("maxstream", "MaxStream，m3u8（AES），约 12 小时；CDN 只认浏览器，只能中转"),
        "LU": LineSpec("lulustream", "m3u8（AES），约 8 小时；CDN 只认浏览器，只能中转"),
        "VI": LineSpec("vidara", "分片伪装成 .css，只能中转"),
        "DD": LineSpec("dood", "mp4，要 Referer，只能中转"),
        "AV": LineSpec("", "JuicyCodes 混淆，暂不支持；只有老片有"),
    }
    sorts = {"": "最新"}
    presets = [
        {"name": "最新", "source": "/", "sort": ""},
        {"name": "英文字幕", "source": "/category/english-subbed", "sort": ""},
        {"name": "无码破解", "source": "/category/decensored", "sort": ""},
        {"name": "JAV", "source": "/category/jav", "sort": ""},
        {"name": "素人", "source": "/category/amateur", "sort": ""},
        {"name": "4K", "source": "/category/4k", "sort": ""},
        {"name": "女优", "source": "/actress/<slug>", "sort": ""},
        {"name": "发行商", "source": "/maker/<slug>", "sort": ""},
        {"name": "标签", "source": "/tag/<slug>", "sort": ""},
        {"name": "搜索", "source": "/search/<关键词>", "sort": ""},
    ]
    source_hint = ("最新 /、英文字幕 /category/english-subbed、无码破解 /category/decensored、女优 /actress/xxx、"
                   "搜索 /search/关键词，直接粘贴站点网址也行；每页 24 部")

    def detail_path(self, key: str) -> str:
        return f"/{key}/v/"  # slug 随便写，站点按文章 id 跳正

    def key_from_url(self, url: str) -> str | None:
        m = _KEY_RE.match(urlsplit(url if "://" in url else "https://x/" + url.lstrip("/")).path)
        return m.group(1) if m else None

    def normalize_source(self, value: str) -> str:
        """'https://jav.guru/category/decensored/page/3/' -> '/category/decensored'；'/?s=SSIS' -> '/search/SSIS'。"""
        value = value.strip()
        parts = urlsplit(value if "://" in value else "https://x/" + value.lstrip("/"))
        if (q := parse_qs(parts.query).get("s")) and q[0].strip():
            return "/search/" + quote(q[0].strip(), safe="")
        path = _PAGE_RE.sub("", parts.path.rstrip("/") + "/").strip("/")
        if not path:
            return "/"
        if _KEY_RE.match("/" + path + "/"):
            raise ValueError(f"不是列表地址：{value}")
        return "/" + "/".join(quote(unquote(p), safe="") for p in path.split("/"))

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        if source.startswith("/search/"):
            kw = source.removeprefix("/search/")
            return (f"/page/{page}/" if page > 1 else "/") + f"?s={kw}"
        base = "" if source == "/" else source
        return base + (f"/page/{page}/" if page > 1 else "/")

    def parse_list(self, html: str) -> ListPage:
        doc = LexborHTMLParser(html)
        items: list[SourceItem] = []
        for box in doc.css("div.inside-article"):
            link = box.css_first("div.grid1 h2 a[href]") or box.css_first("a[href]")
            if link is None:
                continue
            key = self.key_from_url(link.attributes.get("href") or "")
            title = (link.attributes.get("title") or link.text(strip=True)).strip()
            code, subtitle, uncensored = parse_title(title)
            if not key or not code:
                continue
            img = box.css_first("div.imgg img")
            thumb = (img.attributes.get("src") or "") if img else ""
            items.append(SourceItem(key=key, code=code, title=title, site_vid=key,
                                    thumb_url=_THUMB_SIZE_RE.sub("", thumb) if thumb.startswith("http") else "",
                                    subtitle=subtitle, uncensored=uncensored))
        last_page = None
        if (el := doc.css_first("span.pages")) is not None and (m := _PAGES_RE.search(el.text())):
            last_page = int(m.group(1).replace(",", ""))
        return ListPage(items=items, last_page=last_page or (1 if items else None))

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        doc = LexborHTMLParser(html)
        h1 = doc.css_first("h1.titl")
        title = h1.text(strip=True) if h1 else ""
        code, subtitle, uncensored = parse_title(title)
        info: dict[str, list] = {}
        for li in doc.css("div.infometa li"):
            label = li.css_first("strong")
            name = (label.text(strip=True) if label else "").rstrip(":").strip().lower()
            links = [(a.text(strip=True), (a.attributes.get("href") or "").rstrip("/").rsplit("/", 1)[-1])
                     for a in li.css("a")]
            text = li.text(strip=True)
            info[name] = links or [(text.split(":", 1)[-1].strip(), "")]
        if not code and info.get("code"):
            code = info["code"][0][0].upper()
        lines = []
        for var, label in _LINE_BTN_RE.findall(html):
            m = re.search(r"var %s = (\{.*?\});" % re.escape(var), html)
            if not m:
                continue
            try:
                url = base64.b64decode(json.loads(m.group(1)).get("iframe_url") or "").decode()
            except (ValueError, json.JSONDecodeError):
                continue
            lines.append((label.upper().removeprefix("STREAM").strip(), url))
        if not lines:
            raise ParseError("详情页没有播放线路")
        img = doc.css_first("div.large-screenimg img")

        def names(k: str) -> list[str]:
            return [n for n, _ in info.get(k, []) if n]

        return SourceDetail(
            key=key, code=code or f"JAVGURU-{key}", title=title, site_vid=key, subtitle=subtitle, uncensored=uncensored,
            cover_url=(img.attributes.get("src") or "") if img else "",
            release_date=(names("release date") or [""])[0][:10],
            models=[{"id": slug, "name": n} for n, slug in info.get("actress", []) if n],
            categories=[{"slug": slug, "name": n} for n, slug in info.get("category", []) if n],
            tags=[{"slug": slug, "name": n} for n, slug in info.get("tags", []) if n],
            maker=(names("studio") or [""])[0], director=(names("director") or [""])[0],
            series=(names("label") or [""])[0], lines=lines,
            claimed_height=(q.height if (q := from_labels(names("category"), "claimed")) else 0),
        )

    async def lookup(self, sf: SiteFetcher, code: str, uncensored: bool = False,
                     priority: bool = False) -> tuple[list[SourceItem], str]:
        ck = code_key(code)
        if not ck:
            return [], ""
        return await self.search(sf, f"/?s={quote(code.strip().upper())}", priority)  # find_by_code 按番号核对

    async def resolve_line(self, http: Fetcher, name: str, link: str) -> HostStream:
        """线路数据 https://jav.guru/searcho/?{L}d={HEX}… → /searcho/?{L}r={HEX 倒过来}，302 到嵌入页。"""
        m = _LINE_DATA_RE.search(link)
        if not m:
            raise ParseError(f"线路 {name} 的数据认不出：{link[:80]}")
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(link))
        resp = await http.fetch(f"{origin}/searcho/?{m.group(1)}r={m.group(2)[::-1]}", allow_redirects=False)
        embed = resp.headers.get("location") or ""
        if resp.status_code not in (301, 302, 303, 307) or not embed:
            raise FetchError(f"没有跳转到播放站（HTTP {resp.status_code}），线路数据可能已失效")
        return await resolve_embed(http, embed, origin + "/", name)
