import logging
import sys

import pytest
from curl_cffi.requests.exceptions import IncompleteRead
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.errors import RelayAborted
from hls2strm.observability import UvicornFilter, ring

from .conftest import FakeFetcher
from .test_engine import model_fixture


def test_play_and_resolve(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path, default_public_base_url="http://hls2strm:8080"))
    try:
        with TestClient(app) as c:
            html, ids = model_fixture()
            fake = FakeFetcher(html, ids)
            app.state.ctx.fetcher = fake
            app.state.ctx.resolver.fetcher = fake

            # 普通播放器：302 直连 CDN
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Infuse/7.8"}, follow_redirects=False)
            assert r.status_code == 302 and r.headers["location"].endswith("/62384.m3u8")
            # ffmpeg（Lavf）：中转，分片走相对地址
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Lavf/61.7.100"})
            src_id = c.get("/api/videos", params={"q": "ipzz-983"}).json()["items"][0]["sources"][0]["id"]
            assert r.status_code == 200 and f"../hls/{src_id}/a.ts" in r.text
            # 浏览器跨域：中转 + CORS
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Mozilla/5.0", "Origin": "http://emby:8096"})
            assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
            assert c.options(f"/hls/{src_id}/a.ts").status_code == 204
            # 同源的脚本请求（不带 Origin，但 Sec-Fetch-Mode: cors）也要中转
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Mozilla/5.0", "Sec-Fetch-Mode": "cors"},
                      follow_redirects=False)
            assert r.status_code == 200 and "../hls/" in r.text

            # resolve：没设令牌不开放；令牌错误 401
            assert c.get("/api/resolve/ipzz-983.m3u8").status_code == 403
            assert c.put("/api/settings", json={"resolve_token": "tk"}).status_code == 200
            assert c.get("/api/resolve/ipzz-983.m3u8", headers={"Authorization": "Bearer x"}).status_code == 401

            h = {"Authorization": "Bearer tk"}
            r = c.get("/api/resolve/play/ipzz-983.m3u8", headers=h, params={"ua": "Infuse/7.8"})
            data = r.json()
            assert r.status_code == 200 and data["slug"] == "ipzz-983" and data["url"].endswith("/62384.m3u8")
            assert 3600 < data["ttl"] <= 10800
            # 不按客户端判断回退：浏览器、ffmpeg 一律拿 CDN 地址
            for q in ({"ua": "Lavf/61"}, {"ua": "Mozilla/5.0", "origin": "http://emby:8096"},
                      {"ua": "Mozilla/5.0", "fetch_mode": "cors"}):
                r = c.get("/api/resolve/ipzz-983", headers=h, params=q)
                assert r.status_code == 200 and r.json()["url"].endswith("/62384.m3u8")
            assert c.get("/api/resolve/not-exist-1.m3u8", headers=h).status_code == 404
            assert c.get("/api/resolve/ipzz-983?token=tk").status_code == 200

            # 解题服务检测：没填地址、地址格式不对都直接提示
            assert c.post("/api/solver/test", json={}).status_code == 400
            assert c.post("/api/solver/test", json={"url": "byparr:8191"}).status_code == 400

            # 中转传到一半 CDN 断开：记一行 WARNING，抛 RelayAborted 让服务器断开连接
            fake.session = BrokenSession()
            with pytest.raises(RelayAborted):
                c.get(f"/hls/{src_id}/a.ts", headers={"User-Agent": "Lavf/61"})
            assert any(r["level"] == "WARNING" and r["msg"].startswith("中转 ipzz-983：Jable 的 a.ts 传到 1 KB 时 CDN 断开")
                       for r in ring.records)

            # 日志级别现场切换，不认的级别拒绝
            assert c.get("/api/logs/level").json() == {"level": "INFO", "default": "INFO"}
            assert c.put("/api/logs/level", json={"level": "DEBUG"}).json()["level"] == "DEBUG"
            assert logging.getLogger("hls2strm.fetcher").isEnabledFor(logging.DEBUG)
            assert c.put("/api/logs/level", json={"level": "TRACE"}).status_code == 422
            assert c.put("/api/logs/level", json={"level": "INFO"}).json()["level"] == "INFO"
    finally:
        for h in logging.getLogger().handlers:
            h.close()
        logging.getLogger().handlers.clear()


class BrokenSession:
    """CDN 传了 1 KB 就断开。"""

    async def get(self, url, **kw):
        return BrokenResp()


class BrokenResp:
    status_code = 200
    headers: dict = {}
    quit_now = None

    async def aiter_content(self):
        yield b"x" * 1024
        raise IncompleteRead("curl: (18) end of response with 1000 bytes missing")

    async def aclose(self):
        pass


def test_uvicorn_log_filter():
    """uvicorn.error 不是错误，显示成 uvicorn；中转断流已经记过一行，丢掉 uvicorn 带 traceback 的那条。"""
    f = UvicornFilter()
    rec = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, "Started server process", None, None)
    assert f.filter(rec) and rec.name == "uvicorn"
    try:
        raise RelayAborted("curl: (18)")
    except RelayAborted:
        rec = logging.LogRecord("uvicorn.error", logging.ERROR, "", 0, "Exception in ASGI application", None,
                                sys.exc_info())
    assert not f.filter(rec)
    try:
        raise ValueError("bug")
    except ValueError:
        rec = logging.LogRecord("uvicorn.error", logging.ERROR, "", 0, "Exception in ASGI application", None,
                                sys.exc_info())
    assert f.filter(rec)  # 别的异常照常打
