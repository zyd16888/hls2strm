import asyncio
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI

from hls2strm.observability import Metrics
from hls2strm.play import Resolver, router
from hls2strm.sites import SourceDetail

from .conftest import FakeFetcher


def test_mp4_probe_and_range_requests_pin_the_resolved_session(make_store):
    async def run():
        db, store = await make_store()
        store.current.quality_capture = False
        store.current.play_token = "test-token"
        vid = await db.upsert_detail("javmost", SourceDetail(
            key="ABC-001", code="ABC-001", title="ABC", lines=[("DOO", "embed")],
        ), "abc-001", store.current.site_rank)
        src = (await db.get_sources(vid))[0]
        line = (await db.get_lines(src["id"]))[0]
        original = "https://cdn.invalid/stream?t=original"
        await db.set_line_stream(line["id"], original, int(time.time()) + 14400, "dooplayer")

        class Response:
            def __init__(self, partial):
                self.status_code = 206 if partial else 200
                self.headers = {"content-type": "video/mp4", "content-length": "4", "accept-ranges": "bytes"}
                if partial:
                    self.headers["content-range"] = "bytes 4-7/8"
                self.closed = self.consumed = False

            async def aiter_content(self):
                self.consumed = True
                yield b"ftyp"

            async def aclose(self):
                self.closed = True

        class Session:
            def __init__(self):
                self.calls = []

            async def get(self, url, **kw):
                response = Response(bool(kw.get("headers", {}).get("Range")))
                self.calls.append((url, kw, response))
                return response

        fetcher = FakeFetcher("", {})
        fetcher.session = Session()
        metrics = Metrics()
        resolver = Resolver(db, fetcher, store, metrics)
        app = FastAPI()
        app.include_router(router)
        app.state.ctx = SimpleNamespace(db=db, store=store, resolver=resolver, fetcher=fetcher, metrics=metrics)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            probe = await client.head("/play/abc-001.m3u8?proxy=1&t=test-token")
            assert probe.status_code == 200 and probe.headers["content-type"] == "video/mp4"
            assert not probe.content
            assert fetcher.session.calls[0][2].closed and fetcher.session.calls[0][2].consumed  # 有限媒体预检
            assert fetcher.session.calls[1][2].closed and not fetcher.session.calls[1][2].consumed  # HEAD 本身
            media_url = urljoin(str(probe.url), probe.headers["content-location"])
            assert urlsplit(media_url).path == f"/media/{src['id']}"
            prepared = (await client.get("/play/abc-001.m3u8?prepare=1&t=test-token")).json()
            assert (prepared["source_id"], prepared["line_id"], prepared["label"], prepared["media_type"]) == (
                src["id"], line["id"], "JAVMost", "file")

            # 另一个播放器或刷新任务改了数据库直链，不影响这次播放后续的 Range。
            await db.set_line_stream(line["id"], "https://cdn.invalid/changed", int(time.time()) + 14400, "dooplayer")
            response = await client.get(media_url, headers={"Range": "bytes=4-7", "Origin": "https://player.invalid"})
            assert response.status_code == 206 and response.content == b"ftyp"
            assert response.headers["content-range"] == "bytes 4-7/8"
            assert response.headers["access-control-allow-origin"] == "*"
            assert "Content-Range" in response.headers["access-control-expose-headers"]
            preflight = await client.options(media_url, headers={"Origin": "https://player.invalid",
                                                                 "Access-Control-Request-Method": "GET"})
            assert preflight.status_code == 204 and preflight.headers["access-control-allow-origin"] == "*"
            url, kw, upstream = fetcher.session.calls[-1]
            assert url == original and kw["headers"]["Range"] == "bytes=4-7" and upstream.closed
            assert (await client.get(media_url.replace("test-token", "wrong"))).status_code == 403
            wrong_source = media_url.replace(f"/media/{src['id']}", f"/media/{src['id'] + 1}")
            assert (await client.get(wrong_source)).status_code == 404
            assert len(fetcher.session.calls) == 3
    asyncio.run(run())


def test_hls_probe_pins_format_and_preserves_variant_choice(make_store):
    async def run():
        db, store = await make_store()
        store.current.quality_capture = False
        from .test_quality import MASTER
        url = "https://cdn.invalid/master.m3u8"
        vid = await db.upsert_detail("jable", SourceDetail(
            key="abc-002", code="ABC-002", title="ABC", stream_url=url,
            stream_expires=int(time.time()) + 14400,
        ), "abc-002", store.current.site_rank)
        src = (await db.get_sources(vid))[0]
        fetcher = FakeFetcher("", {})
        fetcher.files[url] = MASTER.encode()
        metrics = Metrics()
        resolver = Resolver(db, fetcher, store, metrics)
        app = FastAPI()
        app.include_router(router)
        app.state.ctx = SimpleNamespace(db=db, store=store, resolver=resolver, fetcher=fetcher, metrics=metrics)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            probe = await client.head("/play/abc-002@480p.m3u8?proxy=1&variants=all")
            assert probe.status_code == 200 and "mpegurl" in probe.headers["content-type"]
            await db.set_stream(src["id"], "https://cdn.invalid/changed.mp4", int(time.time()) + 14400)
            media_url = urljoin(str(probe.url), probe.headers["content-location"])
            response = await client.get(media_url, headers={"Origin": "https://player.invalid"})
            assert response.status_code == 200 and "mpegurl" in response.headers["content-type"]
            assert response.headers["access-control-allow-origin"] == "*"
            refs = [ln for ln in response.text.splitlines() if ln and not ln.startswith("#")]
            assert len(refs) == 1 and "480p/video.m3u8" in refs[0]
    asyncio.run(run())
