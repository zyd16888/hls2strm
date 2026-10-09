import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from hls2strm.api import _parse_videos
from hls2strm.config import Settings
from hls2strm.errors import NotFound, ParseError, VideoGone
from hls2strm.play import Resolved, _upstream, rewrite_playlist
from hls2strm.sites import SITES, find_by_code
from hls2strm.sites.hosts import detect, resolve_embed
from hls2strm.sites.javguru import parse_title

from .conftest import fixture
from .test_supjav import FakeHttp, FakeSiteFetcher, Resp

GURU = SITES["javguru"]
MOST = SITES["javmost"]
AV = SITES["123av"]
AV_EMBED = ("https://sadie-shop.site/e/1RD82N?poster=https%3A%2F%2Ficdn.123av.me%2Fimg2%2Fs500%2F73%2F"
            "abf-392-uncensored-leaked%2Fcover.jpg%3F6ac862c6")


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


def test_av123_pages():
    lp = AV.parse_list(fixture("av123_list.html"))
    assert len(lp.items) == 12 and lp.last_page == 5000
    it = lp.items[0]
    assert (it.key, it.code, it.duration) == ("fc2-ppv-4988898", "FC2-PPV-4988898", 1901)
    assert it.title.startswith("FC2-PPV-4988898 *着床") and it.thumb_url.startswith("https://icdn.123av.me/")
    leak = next(x for x in lp.items if x.uncensored)
    assert leak.key == "abf-392-uncensored-leaked" and leak.code == "ABF-392" and leak.title.startswith("ABF-392 ")

    html = fixture("av123_detail.html")
    d = AV.parse_detail(html, "abf-392-uncensored-leaked")
    assert (d.code, d.uncensored, d.release_date, d.duration) == ("ABF-392", True, "2026-10-08", 7981)
    assert (d.maker, d.series, d.title) == ("Prestige", "极致滑溜高潮", "ABF-392 极致滑腻的高潮——泷本静叶")
    assert d.models == [{"id": "shizukuha-takimoto", "name": "Shizukuha Takimoto"}]
    assert d.categories[1] == {"slug": "big-tits", "name": "大胸部"} and d.tags == [{"slug": "abf", "name": "ABF"}]
    assert d.cover_url == "https://icdn.123av.me/img2/s500/73/abf-392-uncensored-leaked/cover.jpg?6ac862c6"
    assert d.lines == [("123AV", AV_EMBED)]

    # 播放器里没有分集是没有源；分成多集的拼不成一个流
    def with_episodes(js: str) -> str:
        return re.sub(r"JSON\.parse\('.*?'\)", lambda m: f"JSON.parse('{js}')", html, count=1)

    with pytest.raises(VideoGone):
        AV.parse_detail(with_episodes("[]"), "x")
    q, sl = "\\" + "u0022", "\\" * 3 + "/"  # 页面里 JSON 套在 JS 字符串里：引号写成 Unicode 转义，斜杠写成三个反斜杠加斜杠
    ep = '{"number":%d,"name":"%d","url":"https:||a.site|e|X%d"}'.replace('"', q).replace("|", sl)
    with pytest.raises(ParseError, match="2 集"):
        AV.parse_detail(with_episodes(f"[{ep % (1, 1, 1)},{ep % (2, 2, 2)}]"), "x")
    with pytest.raises(ParseError):
        AV.parse_detail("<html><main><section class='moved'>We have moved to 123av.com</section></main></html>", "x")

    assert AV.key_for("ABF-392") == "abf-392" and AV.key_for("FC2PPV-4981211") == "fc2-ppv-4981211"
    assert AV.key_for("SSIS-001", uncensored=True) == "ssis-001-uncensored-leaked"
    assert AV.variant_of("ssis-001-uncensored-leaked") == ("", True)
    assert AV.key_from_url("https://njav.tv/en/v/HBAD-742") == "hbad-742"
    assert AV.key_from_url("https://123av.com/cn/actresses/yua-mikami") is None
    assert AV.normalize_source("https://123av.com/en/censored?year=2024&page=3&sort=views") == "/cn/censored?year=2024"
    assert AV.normalize_source("/cn/search/三上") == "/cn/search?keyword=%E4%B8%89%E4%B8%8A"
    assert AV.normalize_source("hot") == "/cn/hot"
    for bad in ("https://123av.com/cn/v/ssis-001", "/cn/genres", "/cn/me/feed", "/cn/search"):
        with pytest.raises(ValueError):
            AV.normalize_source(bad)
    assert AV.page_url("/cn/new", 1) == "/cn/new"
    assert AV.page_url("/cn/search?keyword=SSIS%20001", 3, "week") == "/cn/search?keyword=SSIS%20001&sort=week&page=3"

    sf = FakeSiteFetcher({"/cn/v/abf-392-uncensored-leaked": html}, FakeHttp({}))
    found = asyncio.run(find_by_code(AV, sf, "ABF-392", uncensored=True))
    assert [x.key for x in found] == ["abf-392-uncensored-leaked"]

    # 旧域名 njav.tv 只剩跳转页，不抓取，但粘贴的旧网址要认得
    c = SimpleNamespace(store=SimpleNamespace(current=Settings()))
    assert _parse_videos(c, "https://njav.tv/en/v/HBAD-742 https://123av.com/cn/v/abf-392", "jable") == [
        ("123av", "hbad-742"), ("123av", "abf-392")]


def test_av123_line_and_relay():
    m3u8 = "https://8qx3.landon-blog.site/oo5/XEn0ez/video.m3u8"
    http = FakeHttp({"https://sadie-shop.site/stream?id=1RD82N": Resp(200, json.dumps(
        {"status": "ok", "media": {"stream": m3u8, "vtt": "https://8qx3.landon-blog.site/oo5/XEn0ez/preview.vtt"}}))})
    hs = asyncio.run(AV.resolve_line(http, "123AV", AV_EMBED))
    assert (hs.url, hs.expires, hs.host, hs.referer) == (m3u8, None, "av123", "https://sadie-shop.site/")
    assert http.calls == [("https://sadie-shop.site/stream?id=1RD82N", AV_EMBED)]
    with pytest.raises(NotFound):
        asyncio.run(AV.resolve_line(FakeHttp({}), "123AV", AV_EMBED))

    # 中转：Referer 跟着线路（嵌入站会换域名），不过期；分片扩展名轮换（.css、.svg、.vtt…），一律改名 .ts
    line = {"line": "123AV", "host": "av123", "referer": hs.referer, "stream_url": m3u8, "stream_expires": None}
    r = Resolved({}, {"id": 9}, AV, line)
    assert r.traits.headers == {"Referer": "https://sadie-shop.site/"} and not r.traits.direct
    assert not r.traits.expires and r.traits.disguised_segments
    root = m3u8.rsplit("/", 1)[0] + "/"
    sub = "#EXTM3U\n#EXTINF:3,\nMzAwLXYw.css\n#EXTINF:3,\nMzAwLXYx.svg\n#EXTINF:3,\nMzAwLXY4.vtt\n"
    out = rewrite_playlist(sub, root + "qc/v.m3u8", root, "../", "", True)
    segs = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert segs == ["../qc/MzAwLXYw.css.ts", "../qc/MzAwLXYx.svg.ts", "../qc/MzAwLXY4.vtt.ts"]
    assert _upstream(r, "qc/MzAwLXY4.vtt.ts") == root + "qc/MzAwLXY4.vtt"
