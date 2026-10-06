import pytest

from jable_strm.parser import (
    ParseError,
    VideoGone,
    hls_expires,
    m3u8_duration,
    parse_count,
    parse_detail,
    parse_duration,
    parse_list,
    slug_from_url,
    split_code,
)
from jable_strm.sources import normalize_source, page_url

from .conftest import fixture


def test_parse_list_block():
    lp = parse_list(fixture("list_latest_block.html"))
    assert len(lp.items) == 24
    assert lp.last_page == 1641
    assert lp.block_id == "list_videos_latest_videos_list"
    it = lp.items[0]
    assert (it.video_id, it.slug, it.code, it.duration) == (62398, "club-936", "CLUB-936", 7191)
    assert it.thumb_url.endswith("/320x180/1.jpg")
    assert it.preview_url.endswith("_preview.mp4")
    assert it.views and it.likes is not None


@pytest.mark.parametrize(
    "name, last, block",
    [
        ("list_category_full.html", 152, "list_videos_common_videos_list"),
        ("list_search_full.html", 32, "list_videos_videos_list_search_result"),
        ("list_model_full.html", 2, "list_videos_common_videos_list"),
    ],
)
def test_parse_list_full_pages(name, last, block):
    lp = parse_list(fixture(name))
    assert len(lp.items) == 24
    assert lp.last_page == last
    assert lp.block_id == block


def test_parse_detail():
    d = parse_detail(fixture("detail.html"), "IPZZ-983")
    assert d.video_id == 62384
    assert d.slug == "ipzz-983"
    assert d.code == "IPZZ-983"
    assert d.hls_url.endswith("/62384.m3u8")
    assert d.hls_expires == hls_expires(d.hls_url) and d.hls_expires > 1_700_000_000
    assert d.release_date == "2026-10-01"
    assert d.quality == "高清原片"
    assert d.models == [{"id": "d93c2a9a227572b09e4697544154a30d", "name": "瀬緒凛"}]
    assert {"slug": "bdsm", "name": "主奴調教"} in d.categories
    assert {"slug": "creampie", "name": "中出"} in d.tags
    assert d.cover_url.endswith("/preview.jpg")
    assert d.views == 285871 and d.favs == 1974


def test_hls_expires_formats():
    old = "https://a.mushroomtrack.com/hls/tok/1791180954/62000/62384/62384.m3u8"
    new = ("https://ao-block-ater.mushroomtrack.com/bcdn_token=ujSbmq&expires=1791270819"
           "&token_path=%2Fvod%2F/vod/9000/9922/9922.m3u8")
    assert hls_expires(old) == 1791180954
    assert hls_expires(new) == 1791270819
    assert hls_expires("https://a.mushroomtrack.com/vod/9000/9922/9922.m3u8") is None


def test_parse_detail_gone_and_broken():
    with pytest.raises(VideoGone):
        parse_detail("<html><head><title>%title% - Jable.TV</title></head><body></body></html>", "x-1")
    with pytest.raises(ParseError):
        parse_detail(fixture("not_found.html"), "x-1")


def test_helpers():
    assert split_code("SONE-001 タイトル", "sone-001-c") == "SONE-001"
    assert split_code("FC2-PPV-1234567 title", "fc2-ppv-1234567") == "FC2-PPV-1234567"
    assert split_code("没有番号的标题", "abc-123") == "ABC-123"
    assert parse_duration("1:59:51") == 7191 and parse_duration("59:51") == 3591 and parse_duration("x") is None
    assert parse_count("285 871") == 285871 and parse_count("") is None
    assert slug_from_url("https://jable.tv/videos/IPZZ-983/?a=1") == "ipzz-983"


def test_sources():
    assert normalize_source("https://jable.tv/tags/creampie/3/?x=1") == "/tags/creampie/"
    assert normalize_source("models/abc") == "/models/abc/"
    assert normalize_source("/search/三上/") == "/search/%E4%B8%89%E4%B8%8A/"
    with pytest.raises(ValueError):
        normalize_source("https://jable.tv/videos/ipzz-983/")
    url = page_url("/latest-updates/", 5, "post_date")
    assert url == "/latest-updates/?mode=async&function=get_block&block_id=list_videos_latest_videos_list&sort_by=post_date&from=5"
    assert "q=%E4%B8%89%E4%B8%8A" in page_url("/search/%E4%B8%89%E4%B8%8A/", 2)
    assert "block_id=list_videos_common_videos_list" in page_url("/tags/creampie/", 2)


def test_m3u8_duration():
    text = "#EXTM3U\n#EXTINF:4.004000,\na.ts\n#EXTINF:6.373033,\nb.ts\n#EXT-X-ENDLIST\n"
    assert m3u8_duration(text) == 10
    assert m3u8_duration("#EXTM3U\n") is None
