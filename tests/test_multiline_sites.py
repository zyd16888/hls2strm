import asyncio
import json

from hls2strm.sites import SITES, find_by_code
from hls2strm.sites.hosts import detect, resolve_embed
from hls2strm.sites.javguru import parse_title

from .conftest import fixture
from .test_supjav import FakeHttp, FakeSiteFetcher, Resp

GURU = SITES["javguru"]
MOST = SITES["javmost"]


def test_javguru_pages():
    assert parse_title("[START-628-SUBS] Abstinence…") == ("START-628", "en", False)
    assert parse_title("[FNS-258-MR] …") == ("FNS-258", "", True)
    lp = GURU.parse_list(fixture("javguru_list.html"))
    assert len(lp.items) == 24 and lp.last_page == 67
    it = lp.items[0]
    assert (it.key, it.code, it.subtitle) == ("1057536", "START-628", "en") and not it.thumb_url.endswith("-550x374.jpg")

    d = GURU.parse_detail(fixture("javguru_detail.html"), "134963")
    assert (d.code, d.release_date, d.maker, d.director) == ("SSIS-001", "2021-02-19", "S1 NO.1 STYLE", "Hasami Kuka")
    assert [m["name"] for m in d.models] == ["Aoi Tsukasa", "Otoshiro Sayaka"]
    assert [n for n, _ in d.lines] == ["AV", "SB", "VO", "LU", "DD", "JK"]
    assert dict(d.lines)["SB"].startswith("https://jav.guru/searcho/?xd=")
    d = GURU.parse_detail(fixture("javguru_detail_mr.html"), "1")
    assert d.code == "FNS-258" and d.uncensored

    assert GURU.normalize_source("https://jav.guru/category/decensored/page/3/") == "/category/decensored"
    assert GURU.normalize_source("https://jav.guru/?s=SSIS-001") == "/search/SSIS-001"
    assert GURU.page_url("/", 2) == "/page/2/" and GURU.page_url("/search/SSIS-001", 1) == "/?s=SSIS-001"
    assert GURU.key_from_url("https://jav.guru/134963/ssis-001-after/") == "134963"

    sf = FakeSiteFetcher({"/?s=SSIS-001": fixture("javguru_search.html")}, FakeHttp({}))
    assert [x.key for x in asyncio.run(find_by_code(GURU, sf, "SSIS-001"))] == ["134963"]


def test_javguru_line_to_embed():
    link = "https://jav.guru/searcho/?xd=a687671627c6d69346566636&bg=x"
    http = FakeHttp({
        "https://jav.guru/searcho/?xr=":
            Resp(302, headers={"location": "https://javclan.com/e/cfed9mlravxj"}),
        "https://javclan.com/e/": Resp(200, fixture("host_streamhg.html")),
    })
    hs = asyncio.run(GURU.resolve_line(http, "SB", link))
    assert http.calls[0][0] == "https://jav.guru/searcho/?xr=63666564396d6c726176786a"  # 线路数据倒过来（实测的值）
    assert hs.host == "vidhide" and "/master.m3u8?" in hs.url


def test_javmost_pages():
    lp = MOST.parse_list(fixture("javmost_list.html"))
    assert len(lp.items) == 24 and lp.last_page == 13716
    assert lp.items[0].duration and lp.items[0].thumb_url.startswith("https://")
    d = MOST.parse_detail(fixture("javmost_detail.html"), "SSIS-001")
    assert d.code == "SSIS-001" and d.maker == "S1 NO.1 STYLE" and d.release_date == "2021-02-19"
    assert [n for n, _ in d.lines] == ["DOO", "MOST", "TURBO", "SB", "FEMBED", "DOOD"]
    data = json.loads(dict(d.lines)["DOO"])
    assert data["group"] == "62" and data["api"] == "ri3123o235r/" and data["value"]
    d = MOST.parse_detail(fixture("javmost_detail_new.html"), "START-647")
    assert [n for n, _ in d.lines] == ["DOO"]
    assert MOST.key_for("FC2-PPV-1066192") == "FC2PPV-1066192"
    assert MOST.key_for("SSIS-001", uncensored=True) == "SSIS-001-REDUCING-MOSAIC"
    assert MOST.variant_of("SSIS-001-Uncensored-Edit") == ("", True)
    assert MOST.normalize_source("https://www.javmost.ws/category/all/page/3/") == "/category/all"
    assert MOST.key_from_url("https://www.javmost.ws/star/x/") is None


def test_javmost_line_dooplayer():
    link = json.dumps({"api": "https://www.javmost.ws/ri3123o235r/", "group": "62", "c": ["a", "b", "c"],
                       "value": "v", "referer": "https://www.javmost.ws/START-647/"})
    http = FakeHttp({
        "https://www.javmost.ws/ri3123o235r/": Resp(200, json.dumps({"status": "success", "data": [
            "https://www.dooplayer.com/embed/e/MTE0MjQ3.cbd9aeef07cffe07"]})),
        "https://www.dooplayer.com/embed/": Resp(200, fixture("host_dooplayer.html")),
        "https://www.dooplayer.com/api/stream/": Resp(200, json.dumps({"ok": True, "url": "https://cdn.mostplayer.com/stream?t=x"})),
    })
    hs = asyncio.run(MOST.resolve_line(http, "DOO", link))
    assert hs.host == "dooplayer" and hs.url == "https://cdn.mostplayer.com/stream?t=x"
    assert http.bodies[0]["group"] == "62" and http.bodies[0]["code"] == "a"
    assert http.calls[2][0].startswith("https://www.dooplayer.com/api/stream/MTE0MjQ3")


def test_hosts_detect_and_dood():
    assert detect(fixture("host_maxstream.html"), "JK", "https://maxstream.org/embed-x.html") == "maxstream"
    assert detect(fixture("host_dood.html")) == "dood"
    assert detect(fixture("host_dooplayer.html")) == "dooplayer"
    # Dood：嵌入页不认这个 Referer 时不带 Referer 重来；/pass_md5/ 换回前缀，拼成 mp4 地址；中转要带嵌入页当 Referer
    http = FakeHttp({
        "https://dood.pm/e/abc": Resp(200, "Video embed restricted for this domain"),
    })
    real = http.fetch

    async def fetch(url, **kw):
        if url == "https://dood.pm/e/abc" and "Referer" not in (kw.get("headers") or {}):
            return Resp(200, fixture("host_dood.html"), url="https://playmogo.com/e/abc")
        if "/pass_md5/" in url:
            return Resp(200, "https://xx.cloudatacdn.com/u5kj/")
        return await real(url, **kw)

    http.fetch = fetch
    hs = asyncio.run(resolve_embed(http, "https://dood.pm/e/abc", "https://www.javmost.ws/x/"))
    assert hs.host == "dood" and hs.url.startswith("https://xx.cloudatacdn.com/u5kj/") and "?token=" in hs.url
    assert hs.referer == "https://playmogo.com/e/abc"
