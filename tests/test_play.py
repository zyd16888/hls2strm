import logging

from fastapi.testclient import TestClient

from jable_strm.app import create_app
from jable_strm.config import BootConfig

from .conftest import FakeFetcher
from .test_engine import model_fixture


def test_play_and_resolve(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path, default_public_base_url="http://jable-strm:8080"))
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
            assert r.status_code == 200 and "../hls/ipzz-983/a.ts" in r.text
            # 浏览器跨域：中转 + CORS
            r = c.get("/play/ipzz-983.m3u8", headers={"User-Agent": "Mozilla/5.0", "Origin": "http://emby:8096"})
            assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
            assert c.options("/hls/ipzz-983/a.ts").status_code == 204
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
            r = c.get("/api/resolve/ipzz-983", headers=h, params={"ua": "Lavf/61"})
            assert r.status_code == 409 and "Lavf" in r.json()["reason"]
            r = c.get("/api/resolve/ipzz-983", headers=h, params={"ua": "Mozilla/5.0", "origin": "http://emby:8096"})
            assert r.status_code == 409
            r = c.get("/api/resolve/ipzz-983", headers=h, params={"ua": "Mozilla/5.0", "fetch_mode": "cors"})
            assert r.status_code == 409 and "cors" in r.json()["reason"]
            r = c.get("/api/resolve/ipzz-983", headers=h, params={"ua": "Mozilla/5.0", "fetch_mode": "no-cors"})
            assert r.status_code == 200  # <video> 元素直接加载，不受 CORS 限制
            assert c.get("/api/resolve/not-exist-1.m3u8", headers=h).status_code == 404
            assert c.get("/api/resolve/ipzz-983?token=tk").status_code == 200

            # 解题服务检测：没填地址、地址格式不对都直接提示
            assert c.post("/api/solver/test", json={}).status_code == 400
            assert c.post("/api/solver/test", json={"url": "byparr:8191"}).status_code == 400
    finally:
        for h in logging.getLogger().handlers:
            h.close()
        logging.getLogger().handlers.clear()
