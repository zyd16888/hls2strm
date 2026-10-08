import asyncio
import time

from jable_strm.fetcher import Page
from jable_strm.sites import SITES
from jable_strm.sites.supjav import GATEWAY, parse_title

from .conftest import fixture

SUPJAV = SITES["supjav"]


class Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FakeHttp:
    """网关和播放站：按地址前缀返回预设响应，记下每次请求带的 Referer。"""

    def __init__(self, routes: dict[str, Resp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, url, *, headers=None, allow_redirects=True, method="GET", json=None):
        self.calls.append((url, (headers or {}).get("Referer", "")))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp
        return Resp(404, "404")


class FakeSiteFetcher:
    def __init__(self, pages: dict[str, str], http: FakeHttp) -> None:
        self.pages = pages
        self.parent = http

    async def get_page(self, path, *, priority=False):
        return Page(self.pages[path], "https://supjav.com" + path, "https://supjav.com")


def test_titles_lists_and_sources():
    assert parse_title("[中文字幕]SNOS-223 她…") == ("SNOS-223", "zh", False)
    assert parse_title("[无码破解]SSIS-001 禁欲…") == ("SSIS-001", "", True)
    assert parse_title("[4K][英文字幕]IPZZ-983 a") == ("IPZZ-983", "en", False)
    assert parse_title("FC2PPV 4988588 xx") == ("FC2-PPV-4988588", "", False)
    assert parse_title("HEYZO 3894 x")[0] == "HEYZO-3894"
    assert parse_title("Caribbeancom 加勒比 071926-001 xx")[0] == "071926-001"

    lp = SUPJAV.parse_list(fixture("supjav_list.html"))
    assert len(lp.items) == 24 and lp.last_page == 456
    it = lp.items[0]
    assert (it.key, it.code, it.subtitle) == ("429247", "SNOS-223", "zh")
    assert it.thumb_url == "https://img.supjav.com/images/2026/05/snos223pl.jpg"  # 去掉缩略图尺寸后缀

    d = SUPJAV.parse_detail(fixture("supjav_detail.html"), "443625")
    assert d.code == "SSIS-001" and d.maker == "S1 NO.1 STYLE" and len(d.models) == 2
    assert [s[0] for s in d.extra["servers"]] == ["FST", "ST", "VOE"]

    assert SUPJAV.normalize_source("https://supjav.com/zh/category/chinese-subtitles/page/3?sort=views") == \
        "/zh/category/chinese-subtitles"
    assert SUPJAV.page_url("/zh/category/chinese-subtitles", 2, "views") == "/zh/category/chinese-subtitles/page/2?sort=views"
    src = SUPJAV.normalize_source("https://supjav.com/zh/?s=SSIS-001")
    assert src == "/zh/search/SSIS-001"
    assert SUPJAV.page_url(src, 1) == "/zh/?s=SSIS-001" and SUPJAV.page_url(src, 2) == "/zh/page/2/?s=SSIS-001"
    assert SUPJAV.key_from_url("https://supjav.com/zh/443625.html") == "443625"


def test_lookup_by_code():
    sf = FakeSiteFetcher({"/zh/?s=SSIS-001": fixture("supjav_search.html")}, FakeHttp({}))
    found = asyncio.run(SUPJAV.lookup(sf, "SSIS-001"))
    assert [x.key for x in found] == ["443625"]  # 搜索结果里还有 [无码破解] 版，属于另一部作品
    found = asyncio.run(SUPJAV.lookup(sf, "ssis-001", uncensored=True))
    assert [x.key for x in found] == ["458958"]


def test_server_streams():
    detail = fixture("supjav_detail_aarm370.html")
    servers = dict(SUPJAV.parse_detail(detail, "463322").extra["servers"])
    gw = lambda name: GATEWAY + servers[name][::-1]  # noqa: E731

    # EVS：网关 302 到播放站，packer 解包取 hls2；过期时间 = s + e
    http = FakeHttp({
        gw("EVS"): Resp(302, headers={"location": "https://evsishere.xyz/embed/kmsebjxu7891#supjav.com@AARM-370"}),
        "https://evsishere.xyz/embed/kmsebjxu7891": Resp(200, fixture("supjav_host_evs.html")),
    })
    sf = FakeSiteFetcher({"/zh/463322.html": detail}, http)
    st = asyncio.run(SUPJAV.fetch_stream(sf, "463322"))
    assert "/master.m3u8?" in st.url and st.expires == 1791427648 + 129600
    assert http.calls[0][1] == "https://supjav.com/" and http.calls[1] == (
        "https://evsishere.xyz/embed/kmsebjxu7891", "https://lk1.supremejav.com/")

    # EVS 不行时换下一条：ST 拼出 get_video
    http.routes[gw("EVS")] = Resp(200, "404")
    http.routes[gw("ST")] = Resp(302, headers={"location": "https://streamtape.com/e/6qxlM0ja1jf97A1/AARM-370.mp4"})
    http.routes["https://streamtape.com/e/"] = Resp(200, fixture("supjav_host_st.html"))
    http.routes[gw("VOE")] = Resp(200, "404")
    st = asyncio.run(SUPJAV.fetch_stream(sf, "463322"))
    assert st.url.startswith("https://streamtape.com/get_video?id=6qxlM0ja1jf97A1&expires=") and st.url.endswith("&stream=1")
    assert st.expires == 1791497652

    # VOE：跳转页 → 混淆的 JSON → source
    http.routes[gw("VOE")] = Resp(302, headers={"location": "https://voe.sx/e/g2a9jasyavw3#supjav.com@AARM-370.mp4"})
    http.routes["https://voe.sx/e/"] = Resp(200, fixture("supjav_host_voe_jump.html"))
    http.routes["https://teresapoliticallearn.com/e/"] = Resp(200, fixture("supjav_host_voe.html"))
    url, expires = asyncio.run(SUPJAV._server_stream(http, "VOE", servers["VOE"]))
    assert "/master.m3u8?" in url and expires > time.time() - 10 ** 7


def test_fst_hls2():
    detail = fixture("supjav_detail.html")
    servers = dict(SUPJAV.parse_detail(detail, "443625").extra["servers"])
    http = FakeHttp({
        GATEWAY + servers["FST"][::-1]: Resp(302, headers={"location": "https://fc2stream.tv/e/8san2ha28cfy#x"}),
        "https://fc2stream.tv/e/8san2ha28cfy": Resp(200, fixture("supjav_host_fst.html")),
    })
    url, expires = asyncio.run(SUPJAV._server_stream(http, "FST", servers["FST"]))
    assert "/hls2/" in url and url.split("?", 1)[0].endswith("master.m3u8") and expires == 1791427677 + 129600
