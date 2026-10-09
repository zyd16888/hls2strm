"""JAVMost（javmost.ws；javmost.com、javmost.cx 都 301 过来）：curl_cffi 直接能抓。

- 详情页 /{KEY}/，KEY 就是番号（FC2 写成 FC2PPV-{n}），按番号直接拼，不存在返回 404。
  版本写在 key 里：{番号}-REDUCING-MOSAIC（无码破解）、{番号}-UNCENSORED-EDIT。
- 列表：最新 /category/all/page/N/，类型 /category/{名称}/，女优 /star/{名字}/，发行商 /maker/{名称}/，
  标签 /tag/{X}/，搜索 /search/{关键词}/（模糊）；每页 24 条，最大页码在 /page/N 链接里。
- 线路：按钮 select_part('{part}','{group}',this,'parent|child','{c1}','{c2}','{c3}')，parent 是服务器，
  child 是这个服务器的分段。取嵌入页：POST {站点}/{url_source 路径}（页面脚本里 `url_source=…+'xxx/'`），
  表单 group、part、code=c1、code2=c2、code3=c3、value=页面变量、sound=av，带 Referer 和 X-Requested-With，
  返回 {"status":"success","data":["嵌入页"]}。
  服务器编号对应的播放站是固定的：62 DooPlayer、24 Dood、54 MostPlayer、60 TurboVid、0 PlayerSB、38 / 40 Fembed。
  分成多段的服务器没法拼成一个流，跳过。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..codes import code_key
from ..errors import FetchError, ParseError
from .base import LineSpec, ListPage, Site, SourceDetail, SourceItem, StreamTraits
from .hosts import HostStream, resolve_embed

if TYPE_CHECKING:
    from ..fetcher import Fetcher, SiteFetcher

GROUPS = {"62": "DOO", "24": "DOOD", "54": "MOST", "60": "TURBO", "0": "SB", "40": "FEMBED", "38": "FEMBED"}
VARIANTS = ("-REDUCING-MOSAIC", "-UNCENSORED-EDIT")
_KEY_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]*)/?$")
_LIST_ROOTS = ("category", "star", "maker", "director", "tag", "search", "page")
_PART_RE = re.compile(r"select_part\('(\d+)','(\d+)',this,'(\w+)','([^']*)','([^']*)','([^']*)'\)")
_API_RE = re.compile(r"url_source\s*=\s*\w+\s*\+\s*'([^']+)'")
_VALUE_VAR_RE = re.compile(r"'value'\s*:\s*(\w+)")
_PAGE_RE = re.compile(r"/page/(\d+)")
_RELEASE_RE = re.compile(r"Release\s+(\d{4}-\d{2}-\d{2})")
_TIME_RE = re.compile(r"Time\s+(\d+)")


def split_variant(key: str) -> tuple[str, bool]:
    up = key.upper()
    for v in VARIANTS:
        if up.endswith(v):
            return key[: -len(v)], True
    return key, False


class JavMostSite(Site):
    name = "javmost"
    label = "JAVMost"
    default_domains = ["https://www.javmost.ws"]
    stream = StreamTraits(direct=True, expires=True, ip_bound=True, ip_uncertain=True)
    line_specs = {
        "DOO": LineSpec("dooplayer", "MP4；跨 IP 限制待验证，可按实际环境允许外部直连；新片一般只有这一条"),
        "DOOD": LineSpec("dood", "mp4，要 Referer，只能中转"),
        "TURBO": LineSpec("auto", "emturbovid，没实测过，按页面内容识别"),
        "SB": LineSpec("auto", "playersb，没实测过，按页面内容识别"),
        "MOST": LineSpec("", "MostPlayer：CDN 封了非浏览器访问，暂不支持"),
        "FEMBED": LineSpec("", "Fembed 已失效"),
    }
    sorts = {"": "最新"}
    presets = [
        {"name": "最新", "source": "/category/all", "sort": ""},
        {"name": "类型", "source": "/category/<名称>", "sort": ""},
        {"name": "女优", "source": "/star/<名字>", "sort": ""},
        {"name": "发行商", "source": "/maker/<名称>", "sort": ""},
        {"name": "标签", "source": "/tag/<名称>", "sort": ""},
        {"name": "搜索", "source": "/search/<关键词>", "sort": ""},
    ]
    source_hint = "最新 /category/all、女优 /star/名字、发行商 /maker/名称、搜索 /search/关键词，直接粘贴站点网址也行；每页 24 部"

    def detail_path(self, key: str) -> str:
        return f"/{key}/"

    def key_from_url(self, url: str) -> str | None:
        path = urlsplit(url if "://" in url else "https://x/" + url.lstrip("/")).path
        m = _KEY_RE.match(path)
        if not m or m.group(1).lower() in _LIST_ROOTS:
            return None
        return m.group(1)

    def key_for(self, code: str, subtitle: str = "", uncensored: bool = False) -> str | None:
        ck = code_key(code)
        if not ck:
            return None
        base = f"FC2PPV-{ck.split('-', 1)[1]}" if ck.startswith("FC2PPV-") else code.strip().upper()
        if not _KEY_RE.match(f"/{base}/"):
            return None
        return base + ("-REDUCING-MOSAIC" if uncensored else "")

    def variant_of(self, key: str) -> tuple[str, bool]:
        return "", split_variant(key)[1]

    def normalize_source(self, value: str) -> str:
        """'https://www.javmost.ws/category/all/page/3/' -> '/category/all'。"""
        value = value.strip()
        path = urlsplit(value if "://" in value else "https://x/" + value.lstrip("/")).path
        path = _PAGE_RE.sub("", path).strip("/")
        segs = [unquote(p) for p in path.split("/") if p]
        if not segs:
            return "/category/all"
        if segs[0].lower() not in _LIST_ROOTS:
            raise ValueError(f"不是列表地址：{value}")
        return "/" + "/".join(quote(p, safe="") for p in segs)

    def page_url(self, source: str, page: int, sort: str = "", block_id: str | None = None) -> str:
        return source + (f"/page/{page}/" if page > 1 else "/")

    def parse_list(self, html: str) -> ListPage:
        doc = LexborHTMLParser(html)
        items: list[SourceItem] = []
        for card in doc.css("div.card"):
            link = card.css_first('a[id$="_tag"]') or card.css_first("a[href]")
            key = self.key_from_url(link.attributes.get("href") or "") if link else None
            if not key:
                continue
            base, uncensored = split_variant(key)
            heading = card.css_first("h1.card-title")
            code = (heading.text(strip=True) if heading else base).upper()
            code = split_variant(code)[0]
            titles = [a.attributes.get("alt") or "" for a in card.css("div.card-block a[alt]")]
            title = titles[1].strip() if len(titles) > 1 else ""
            text = card.css_first("p.card-text").text() if card.css_first("p.card-text") else ""
            minutes = _TIME_RE.search(text)
            img = card.css_first("img.card-img-top")
            items.append(SourceItem(key=key, code=code, title=f"{code} {title}".strip(),
                                    duration=int(minutes.group(1)) * 60 if minutes else None,
                                    thumb_url=(img.attributes.get("data-src") or "") if img else "",
                                    uncensored=uncensored))
        pages = [int(x) for x in _PAGE_RE.findall(html)]
        return ListPage(items=items, last_page=max(pages) if pages else (1 if items else None))

    def parse_detail(self, html: str, key: str) -> SourceDetail:
        doc = LexborHTMLParser(html)
        base, uncensored = split_variant(key)
        h1 = doc.css_first("h1.page-header")
        code = split_variant((h1.text(strip=True) if h1 else base).split()[0].upper())[0] if (h1 or base) else base
        og = {m.attributes.get("property"): m.attributes.get("content") or "" for m in doc.css("meta[property]")}
        text = " ".join(p.text() for p in doc.css("p.card-text"))
        models, categories, maker, director = [], [], "", ""
        for a in doc.css("p.card-text a[href]"):
            href = a.attributes.get("href") or ""
            name = a.text(strip=True)
            slug = unquote(href.rstrip("/").rsplit("/", 1)[-1])
            if "/star/" in href:
                models.append({"id": slug, "name": name})
            elif "/category/" in href:
                categories.append({"slug": slug, "name": name})
            elif "/maker/" in href:
                maker = maker or name
            elif "/director/" in href:
                director = director or name
        api = _API_RE.search(html)
        var = _VALUE_VAR_RE.search(html)
        value = re.search(r"var\s+%s\s*=\s*'([^']*)'" % re.escape(var.group(1)), html) if var else None
        parts: dict[str, set[str]] = {}
        buttons: dict[str, tuple[str, str, str]] = {}
        for part, group, kind, c1, c2, c3 in _PART_RE.findall(html):
            parts.setdefault(group, set()).add(part)
            if kind == "parent" and part == "1":
                buttons.setdefault(group, (c1, c2, c3))
        lines = []
        for group, (c1, c2, c3) in buttons.items():
            if parts[group] != {"1"}:
                continue  # 分成多段的服务器拼不成一个流
            name = GROUPS.get(group, f"G{group}")
            if any(n == name for n, _ in lines):
                continue
            lines.append((name, json.dumps({"api": api.group(1) if api else "ri3123o235r/", "group": group,
                                            "c": [c1, c2, c3], "value": value.group(1) if value else ""})))
        if not lines:
            raise ParseError("详情页没有可用的播放线路（没有服务器，或者都分成了多段）")
        minutes = _TIME_RE.search(text)
        release = _RELEASE_RE.search(text)
        return SourceDetail(
            key=key, code=code, title=og.get("og:title") or code, uncensored=uncensored,
            duration=int(minutes.group(1)) * 60 if minutes else None,
            cover_url=og.get("og:image", ""), release_date=(og.get("video:release_date") or
                                                             (release.group(1) if release else ""))[:10],
            models=models, categories=categories, maker=maker, director=director, lines=lines,
        )

    async def fetch_detail(self, sf: SiteFetcher, key: str, *, priority: bool = False) -> SourceDetail:
        """线路数据里补上取嵌入页接口的完整地址和详情页地址（请求接口要带它当 Referer）。"""
        page = await sf.get_page(self.detail_path(key), priority=priority)
        try:
            d = self.parse_detail(page.html, key)
        except ParseError as e:
            e.html = page.html
            raise
        lines = []
        for name, link in d.lines:
            data = json.loads(link)
            data["api"] = urljoin(page.domain + "/", data["api"])
            data["referer"] = page.url
            lines.append((name, json.dumps(data)))
        d.lines = lines
        return d

    async def resolve_line(self, http: Fetcher, name: str, link: str) -> HostStream:
        data = json.loads(link)
        c1, c2, c3 = data["c"]
        resp = await http.fetch(data["api"], method="POST", data={
            "group": data["group"], "part": "1", "code": c1, "code2": c2, "code3": c3, "value": data["value"],
            "sound": "av"}, headers={"Referer": data.get("referer", ""), "X-Requested-With": "XMLHttpRequest"})
        try:
            body = resp.json()
        except ValueError:
            body = {}
        embed = (body.get("data") or [""])[0] if isinstance(body, dict) and body.get("status") == "success" else ""
        if not embed:
            raise FetchError(f"取线路接口没给嵌入页（HTTP {resp.status_code}），线路数据可能已失效")
        return await resolve_embed(http, embed, data.get("referer", ""), name)
