import asyncio
import json
import time

import pytest

from hls2strm.errors import ParseError
from hls2strm.fetcher import FetchError, Page
from hls2strm.sites import SITES, find_by_code
from hls2strm.sites.supjav import GATEWAY, parse_title

from .conftest import fixture

SUPJAV = SITES["supjav"]


class Resp:
    def __init__(self, status=200, text="", headers=None, url=""):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url  # 空串：调用方当作没跳转，用请求的地址

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """网关和播放站：按地址前缀返回预设响应，记下每次请求带的 Referer。"""

    def __init__(self, routes: dict[str, Resp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.bodies: list = []

    async def fetch(self, url, *, headers=None, allow_redirects=True, method="GET", json=None, data=None):
        self.calls.append((url, (headers or {}).get("Referer", "")))
        self.bodies.append(json or data)
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
    assert [n for n, _ in d.lines] == ["FST", "ST", "VOE"]

    assert SUPJAV.normalize_source("https://supjav.com/zh/category/chinese-subtitles/page/3?sort=views") == \
        "/zh/category/chinese-subtitles"
    assert SUPJAV.page_url("/zh/category/chinese-subtitles", 2, "views") == "/zh/category/chinese-subtitles/page/2?sort=views"
    src = SUPJAV.normalize_source("https://supjav.com/zh/?s=SSIS-001")
    assert src == "/zh/search/SSIS-001"
    assert SUPJAV.page_url(src, 1) == "/zh/?s=SSIS-001" and SUPJAV.page_url(src, 2) == "/zh/page/2/?s=SSIS-001"
    assert SUPJAV.key_from_url("https://supjav.com/zh/443625.html") == "443625"


def test_lookup_by_code():
    sf = FakeSiteFetcher({"/zh/?s=SSIS-001": fixture("supjav_search.html")}, FakeHttp({}))
    notes = []
    found = asyncio.run(find_by_code(SUPJAV, sf, "SSIS-001", notes=notes))
    assert [x.key for x in found] == ["443625"]  # 搜索结果里还有 [无码破解] 版，属于另一部作品
    assert notes == ["supjav.com 按番号 SSIS-001 搜到 2 条，不是这部的：SSIS-001（无码流出）"]
    found = asyncio.run(find_by_code(SUPJAV, sf, "ssis-001", uncensored=True))
    assert [x.key for x in found] == ["458958"]


def test_lookup_fake_page_is_not_none_found():
    """搜不到结果时确认是本站的搜索页：停放域名的跳板页报错（算失败），真的空搜索页才算「没有」。"""
    stub = "<html><head><title>Loading...</title></head><body><script>window.location.replace('/x');</script></body></html>"
    empty = '<html><form action="https://supjav.com/zh/" class="search-form"></form><div class="posts"></div></html>'
    sf = FakeSiteFetcher({"/zh/?s=HTTM-070": stub}, FakeHttp({}))
    with pytest.raises(ParseError, match="不是 SupJav 的搜索页") as e:
        asyncio.run(find_by_code(SUPJAV, sf, "HTTM-070"))
    assert e.value.html == stub  # 存快照用
    sf.pages["/zh/?s=HTTM-070"] = empty
    notes = []
    assert asyncio.run(find_by_code(SUPJAV, sf, "HTTM-070", notes=notes)) == []
    assert notes == ["supjav.com 按番号 HTTM-070 搜到 0 条"]


def test_server_streams():
    detail = fixture("supjav_detail_aarm370.html")
    servers = dict(SUPJAV.parse_detail(detail, "463322").lines)
    gw = lambda name: GATEWAY + servers[name][::-1]  # noqa: E731

    # EVS：网关 302 到播放站，packer 解包取 hls2；过期时间 = s + e
    http = FakeHttp({
        gw("EVS"): Resp(302, headers={"location": "https://evsishere.xyz/embed/kmsebjxu7891#supjav.com@AARM-370"}),
        "https://evsishere.xyz/embed/kmsebjxu7891": Resp(200, fixture("supjav_host_evs.html")),
    })
    st = asyncio.run(SUPJAV.resolve_line(http, "EVS", servers["EVS"]))
    assert "/master.m3u8?" in st.url and st.expires == 1791427648 + 129600 and st.host == "vidhide"
    assert http.calls[0][1] == "https://supjav.com/" and http.calls[1] == (
        "https://evsishere.xyz/embed/kmsebjxu7891", "https://lk1.supremejav.com/")

    # 网关没跳转：线路数据失效，报错（由播放解析换下一条线路）
    http.routes[gw("VOE")] = Resp(200, "404")
    try:
        asyncio.run(SUPJAV.resolve_line(http, "VOE", servers["VOE"]))
        raise AssertionError("网关没跳转时应当报错")
    except FetchError:
        pass

    # ST：拼出 get_video
    http.routes[gw("ST")] = Resp(302, headers={"location": "https://streamtape.com/e/6qxlM0ja1jf97A1/AARM-370.mp4"})
    http.routes["https://streamtape.com/e/"] = Resp(200, fixture("supjav_host_st.html"))
    st = asyncio.run(SUPJAV.resolve_line(http, "ST", servers["ST"]))
    assert st.url.startswith("https://streamtape.com/get_video?id=6qxlM0ja1jf97A1&expires=") and st.url.endswith("&stream=1")
    assert st.expires == 1791497652 and st.host == "streamtape"

    # VOE：跳转页 → 混淆的 JSON → source
    http.routes[gw("VOE")] = Resp(302, headers={"location": "https://voe.sx/e/g2a9jasyavw3#supjav.com@AARM-370.mp4"})
    http.routes["https://voe.sx/e/"] = Resp(200, fixture("supjav_host_voe_jump.html"))
    http.routes["https://teresapoliticallearn.com/e/"] = Resp(200, fixture("supjav_host_voe.html"))
    st = asyncio.run(SUPJAV.resolve_line(http, "VOE", servers["VOE"]))
    assert "/master.m3u8?" in st.url and st.expires > time.time() - 10 ** 7 and st.host == "voe"


def test_fst_hls2():
    detail = fixture("supjav_detail.html")
    servers = dict(SUPJAV.parse_detail(detail, "443625").lines)
    http = FakeHttp({
        GATEWAY + servers["FST"][::-1]: Resp(302, headers={"location": "https://fc2stream.tv/e/8san2ha28cfy#x"}),
        "https://fc2stream.tv/e/8san2ha28cfy": Resp(200, fixture("supjav_host_fst.html")),
    })
    st = asyncio.run(SUPJAV.resolve_line(http, "FST", servers["FST"]))
    assert "/hls2/" in st.url and st.url.split("?", 1)[0].endswith("master.m3u8") and st.expires == 1791427677 + 129600


def test_lines_order_failover_and_gateway(make_store):
    """多线路：按设置的顺序试，失败的进冷却换下一条；网关只拿不绑 IP、没强制中转的线路；中转剥假 PNG 头。"""
    from hls2strm.sites.hosts import HostStream, ts_start
    from hls2strm.observability import Metrics
    from hls2strm.play import NoDirectSource, Resolver
    from hls2strm.sites import SourceDetail

    async def run():
        db, store = await make_store()
        rank = store.current.site_rank
        await store.update({"play_discover": False,
                            "sites": {"supjav": {"enabled": True, "line_order": ["EVS", "ST", "VOE"]}}})
        await db.upsert_detail("supjav", SourceDetail(key="463322", code="AARM-370", title="t",
                                                      lines=[("EVS", "e"), ("ST", "s"), ("VOE", "v")]),
                               "aarm-370", rank)
        calls = []

        async def resolve_line(http, name, link):
            calls.append(name)
            if name == "EVS":
                raise FetchError("网关没有跳转")
            return HostStream(f"https://cdn/{name}.m3u8", int(time.time()) + 86400,
                              {"ST": "streamtape", "VOE": "voe"}[name])

        site = SITES["supjav"]
        orig = site.resolve_line
        site.resolve_line = resolve_line
        try:
            r = Resolver(db, None, store, Metrics())
            res = await r.resolve("aarm-370")
            assert calls == ["EVS", "ST"] and res.line["line"] == "ST" and res.url == "https://cdn/ST.m3u8"
            assert res.traits.direct and not res.traits.ip_bound  # ST 能 302，也能给网关
            lines = {ln["line"]: ln for ln in await db.get_lines(res.source["id"])}
            assert lines["EVS"]["fail_streak"] == 1
            # 再来一次：缓存的 ST 还新鲜，不再请求；EVS 在冷却中排到后面
            await r.resolve("aarm-370")
            assert calls == ["EVS", "ST"]
            # 指定线路试播
            res = await r.resolve("aarm-370", site="supjav", line="VOE")
            assert res.line["line"] == "VOE" and res.traits.ip_bound
            # 网关：只要不绑 IP 的线路；ST 设成强制中转后就没有能给网关的线路了
            assert (await r.resolve("aarm-370", direct_only=True)).line["line"] == "ST"
            await store.update({"sites": {"supjav": {"lines": {**{k: v.model_dump() for k, v in
                                                                  store.current.site("supjav").lines.items()},
                                                               "ST": {"enabled": True, "proxy": True}}}}})
            try:
                await r.resolve("aarm-370", direct_only=True)
                raise AssertionError("ST 强制中转后不该给网关")
            except NoDirectSource:
                pass
            res = await r.resolve("aarm-370")
            assert not res.traits.direct  # 强制中转
        finally:
            site.resolve_line = orig
        await db.close()

    asyncio.run(run())

    ts = bytes([0x47] + [0] * 187) * 6
    assert ts_start(b"\x89PNG\r\n\x1a\n" + b"x" * 62 + ts) == 70 and ts_start(ts) == 0 and ts_start(b"junk") == -1
