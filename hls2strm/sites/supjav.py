"""SupJav：整站在 Cloudflare 挑战后面，要配解题服务（Byparr / FlareSolverr）。

- 解一次拿到 cf_clearance，连同解题服务的 UA 一起注入 curl_cffi 会话就能一直抓（UA 必须一致，TLS 指纹不影响）。
- 详情页 /zh/{数字 id}.html；番号只能搜索 /zh/?s=番号（FC2 只搜数字）。标题前缀标出版本：
  [中文字幕]、[英文字幕]、[无码破解]、[4K]。
- 线路：详情页的线路按钮 a.btn-server[data-link] → 网关 lk1.supremejav.com/supjav.php?c={倒序的 data-link}
  （要 Referer: https://supjav.com/，302 到播放站）→ 播放站（要 Referer: lk1.supremejav.com）解出直链，见 hosts.py。
  线路名对应的播放站：EVS、FST = VidHide 一系，VOE，ST = Streamtape，VAS = Vidara，LUC = LuluStream；TV 没见过。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..codes import code_key
from ..errors import FetchError, ParseError
from .hosts import HostStream, resolve_embed
from .base import LineSpec, ListPage, Site, SourceDetail, SourceItem, StreamTraits

if TYPE_CHECKING:
    from ..fetcher import Fetcher, SiteFetcher

GATEWAYS = ("https://lk1.supremejav.com", "https://lk2.supremejav.com", "https://supremejav.com")
GATEWAY = GATEWAYS[0] + "/supjav.php?c="
GATEWAY_REFERER = "https://lk1.supremejav.com/"
_KEY_RE = re.compile(r"/(?:(?:zh|ja|en)/)?(\d+)\.html")
_TAGS_RE = re.compile(r"^\s*((?:\[[^\]]*\]\s*)+)")
_FC2_RE = re.compile(r"^FC2[\s-]*(?:PPV)?[\s-]*(\d{5,9})", re.I)
_CODE_RE = re.compile(r"^([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-\d+)(?=\s|$|[^\w-])")
_HEYZO_RE = re.compile(r"^(HEYZO)[\s-]*(\d+)", re.I)
_DATE_CODE_RE = re.compile(r"(?<!\d)(\d{6}[-_]\d{2,3})(?!\d)")
_PAGE_RE = re.compile(r"/page/(\d+)")


def parse_title(title: str) -> tuple[str, str, bool]:
    """'[中文字幕]SNOS-223 …' -> ('SNOS-223', 'zh', False)；认不出番号时番号为空。"""
    m = _TAGS_RE.match(title)
    tags = m.group(1) if m else ""
    rest = title[m.end():] if m else title.strip()
    low = tags.lower()
    subtitle = "zh" if "中文字幕" in tags or "chinese" in low else "en" if "英文字幕" in tags or "english" in low else ""
    uncensored = any(x in tags for x in ("无码破解", "無碼破解")) or "reducing mosaic" in low
    if m := _FC2_RE.match(rest):
        code = f"FC2-PPV-{m.group(1)}"
    elif m := _HEYZO_RE.match(rest):
        code = f"HEYZO-{m.group(2)}"
    elif m := _CODE_RE.match(rest):
        code = m.group(1).upper()
    elif m := _DATE_CODE_RE.search(rest):
        code = m.group(1).replace("_", "-")
    else:
        code = ""
    return code, subtitle, uncensored


class SupJavSite(Site):
    name = "supjav"
    label = "SupJav"
    default_domains = ["https://supjav.com", "https://supjav.net", "https://supjav.org"]
    default_enabled = False  # 整站要过 CF 挑战，配好解题服务再启用
    lookup_verified = True
    stream = StreamTraits(direct=True, expires=True, ip_bound=True)
    line_specs = {
        "ST": LineSpec("streamtape", "mp4，约 24 小时，播放器能直连"),
        "EVS": LineSpec("vidhide", "m3u8，约 36 小时，带出口 ASN"),
        "FST": LineSpec("vidhide", "老片常见，m3u8，约 36 小时，带出口 ASN"),
        "VOE": LineSpec("voe", "m3u8，约 4 小时，带出口 IP 前两段"),
        "VAS": LineSpec("vidara", "分片伪装成 .woff2，只能中转"),
        "LUC": LineSpec("lulustream", "CDN 只认浏览器，只能中转；1080p"),
        "TV": LineSpec("turbovip", "TurboVip，分片带假 PNG 头，只能中转"),
    }
    sorts = {"": "最新", "views": "最多观看", "day": "今日热门", "week": "本周热门", "month": "本月热门"}
    presets = [
        {"name": "有码", "source": "/zh/category/censored-jav", "sort": ""},
        {"name": "无码", "source": "/zh/category/uncensored-jav", "sort": ""},
        {"name": "中文字幕", "source": "/zh/category/chinese-subtitles", "sort": ""},
        {"name": "无码破解", "source": "/zh/category/reducing-mosaic", "sort": ""},
        {"name": "英文字幕", "source": "/zh/category/english-subtitles", "sort": ""},
        {"name": "素人", "source": "/zh/category/amateur", "sort": ""},
        {"name": "热门", "source": "/zh/popular", "sort": "week"},
        {"name": "女优", "source": "/zh/category/cast/<slug>", "sort": ""},
        {"name": "片商", "source": "/zh/category/maker/<slug>", "sort": ""},
        {"name": "标签", "source": "/zh/tag/<slug>", "sort": ""},
        {"name": "搜索", "source": "/zh/search/<关键词>", "sort": ""},
    ]
    default_sort = ""
    source_hint = ("分类 /zh/category/chinese-subtitles、女优 /zh/category/cast/xxx、标签 /zh/tag/xxx、搜索 /zh/search/关键词，"
                   "直接粘贴站点网址也行；整站要过 CF，需要配好解题服务")

    def detail_path(self, key: str) -> str:
        return f"/zh/{key}.html"

    def key_from_url(self, url: str) -> str | None:
        m = _KEY_RE.search(urlsplit(url if "://" in url else "https://x/" + url.lstrip("/")).path)
        return m.group(1) if m else None

    def normalize_source(self, value: str) -> str:
        """'https://supjav.com/zh/category/chinese-subtitles/page/3?sort=views' -> '/zh/category/chinese-subtitles'。"""
        value = value.strip()
        if not value:
            raise ValueError("列表地址不能为空")
        parts = urlsplit(value if "://" in value else "https://x/" + value.lstrip("/"))
        if (q := parse_qs(parts.query).get("s")) and q[0].strip():
            return "/zh/search/" + quote(q[0].strip(), safe="")
        path = _PAGE_RE.sub("", parts.path).strip("/")
        segs = [unquote(p) for p in path.split("/") if p]
        if segs and segs[0] in ("zh", "ja", "en"):
            segs = segs[1:]
        if not segs or _KEY_RE.search("/" + "/".join(segs)):
            raise ValueError(f"不是列表地址：{value}")
        return "/zh/" + "/".join(quote(p, safe="") for p in segs)

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        if source.startswith("/zh/search/"):
            kw = source.removeprefix("/zh/search/")
            return (f"/zh/page/{page}/" if page > 1 else "/zh/") + f"?s={kw}"
        url = source + (f"/page/{page}" if page > 1 else "")
        return url + (f"?sort={sort}" if sort else "")

    def parse_list(self, html: str) -> ListPage:
        doc = LexborHTMLParser(html)
        items: list[SourceItem] = []
        for post in doc.css("div.post"):
            link = post.css_first("h3 a[href]") or post.css_first("a[href]")
            if link is None:
                continue
            key = self.key_from_url(link.attributes.get("href") or "")
            if not key:
                continue
            title = (link.attributes.get("title") or link.text(strip=True)).strip()
            code, subtitle, uncensored = parse_title(title)
            img = post.css_first("img.thumb")
            thumb = (img.attributes.get("data-original") or img.attributes.get("src") or "") if img else ""
            items.append(SourceItem(key=key, code=code or f"SUPJAV-{key}", title=title, site_vid=key,
                                    thumb_url=thumb.split("!", 1)[0] if thumb.startswith("http") else "",
                                    subtitle=subtitle, uncensored=uncensored))
        pages = [int(x) for x in _PAGE_RE.findall(" ".join(a.attributes.get("href") or ""
                                                           for a in doc.css("div.pagination a")))]
        pages += [int(t) for a in doc.css("div.pagination a") if (t := a.text(strip=True)).isdigit()]
        last_page = max(pages) if pages else (1 if items else None)
        return ListPage(items=items, last_page=last_page)

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        doc = LexborHTMLParser(html)
        meta = doc.css_first("div.post-meta")
        h1 = doc.css_first("div.archive-title h1")
        title = (h1.text(strip=True) if h1 else "") or (meta.css_first("h2").text(strip=True)
                                                        if meta and meta.css_first("h2") else "")
        servers = [(a.text(strip=True).upper(), a.attributes.get("data-link") or "")
                   for a in doc.css("a.btn-server[data-link]")]
        if meta is None or not servers:
            raise ParseError("详情页没有播放线路")
        code, subtitle, uncensored = parse_title(title)
        img = meta.css_first("img.img")
        categories, models, maker = [], [], ""
        for a in meta.css("div.cats a[href]"):
            href = a.attributes.get("href") or ""
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            if "/category/cast/" in href:
                models.append({"id": slug, "name": a.text(strip=True)})
            elif "/category/maker/" in href:
                maker = maker or a.text(strip=True)
            else:
                categories.append({"slug": slug, "name": a.text(strip=True)})
        tags = [{"slug": (a.attributes.get("href") or "").rstrip("/").rsplit("/", 1)[-1], "name": a.text(strip=True)}
                for a in meta.css("div.tags a")]
        return SourceDetail(
            key=key, code=code or f"SUPJAV-{key}", title=title, site_vid=key, subtitle=subtitle, uncensored=uncensored,
            cover_url=(img.attributes.get("src") or "") if img else "", models=models, categories=categories,
            tags=tags, maker=maker, lines=[(n, link) for n, link in servers if n and link],
        )

    async def lookup(self, sf: SiteFetcher, code: str, uncensored: bool = False,
                     priority: bool = False) -> list[SourceItem]:
        """站内搜索番号（FC2 只搜数字），按番号匹配键和是否无码破解核对搜索结果。"""
        ck = code_key(code)
        if not ck:
            return []
        query = ck.split("-", 1)[1] if ck.startswith("FC2PPV-") else code.strip().upper()
        page = await sf.get_page(f"/zh/?s={quote(query)}", priority=priority)
        return [it for it in self.parse_list(page.html).items
                if code_key(it.code) == ck and it.uncensored == uncensored]

    async def resolve_line(self, http: Fetcher, name: str, link: str) -> HostStream:
        """线路按钮的 data-link → 网关（倒序后放进 c=，302 到播放站）→ 播放站解出直链。

        网关有几个域名，也认 l=（不倒序）；依次试，第一个跳转到播放站的为准。
        """
        status = 0
        for param in (f"c={link[::-1]}", f"l={link}"):
            for gw in GATEWAYS:
                try:
                    resp = await http.fetch(f"{gw}/supjav.php?{param}", headers={"Referer": "https://supjav.com/"},
                                            allow_redirects=False)
                except FetchError:
                    continue
                status = resp.status_code
                embed = (resp.headers.get("location") or "").split("#", 1)[0]
                if status in (301, 302, 303, 307) and embed:
                    return await resolve_embed(http, embed, gw + "/", name)
        raise FetchError(f"网关没有跳转到播放站（HTTP {status}），线路数据可能已失效")
