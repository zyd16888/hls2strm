import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jable_strm.fetcher import Fetcher, ping_solver
from jable_strm.observability import Metrics


def serve(routes: dict) -> tuple[str, HTTPServer]:
    """起一个本地假服务：routes = {(方法, 路径): (状态码, 响应头, 响应体)}。"""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, method):
            status, headers, body = routes.get((method, self.path), (404, {}, b"not found"))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply("GET")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply("POST")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def j(obj) -> bytes:
    return json.dumps(obj).encode()


JSON = {"Content-Type": "application/json"}


@pytest.fixture
def servers():
    started = []

    def start(routes):
        url, srv = serve(routes)
        started.append(srv)
        return url

    yield start
    for srv in started:
        srv.shutdown()


def test_ping_identifies_services(servers):
    flaresolverr = servers({("GET", "/"): (200, JSON, j({"msg": "FlareSolverr is ready!", "version": "3.3.21"}))})
    byparr = servers({
        ("GET", "/"): (301, {"Location": "/docs"}, b""),
        ("GET", "/docs"): (200, {"Content-Type": "text/html"}, b"<html>Swagger UI</html>"),
        ("GET", "/openapi.json"): (200, JSON, j({"info": {"title": "Byparr", "version": "2.0.1"}})),
    })
    other = servers({("GET", "/"): (200, {"Content-Type": "text/html"}, b"<html>nginx</html>")})
    with socket.socket() as s:  # 拿一个没人监听的端口
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"

    async def run():
        r = await ping_solver(flaresolverr)
        assert r["ok"] and r["service"] == "FlareSolverr is ready!" and r["version"] == "3.3.21"
        r = await ping_solver(byparr)
        assert r["ok"] and (r["service"], r["version"]) == ("Byparr", "2.0.1")
        r = await ping_solver(other)
        assert r["ok"] and "不像" in r["warning"]
        r = await ping_solver(dead, timeout=3)
        assert not r["ok"] and r["error"].startswith("连接被拒绝")

    asyncio.run(run())


def test_try_solver(servers, make_store):
    ok = servers({("POST", "/v1"): (200, JSON, j({"status": "ok", "solution": {
        "status": 200, "url": "https://fs1.app/", "response": "<html>首页</html>", "userAgent": "UA",
        "cookies": [{"name": "cf_clearance", "value": "x"}, {"name": "PHPSESSID", "value": "y"}]}}))})
    challenged = servers({("POST", "/v1"): (200, JSON, j({"status": "ok", "solution": {
        "status": 403, "response": "<title>Just a moment...</title>", "cookies": []}}))})
    broken = servers({("POST", "/v1"): (500, JSON, j({"message": "browser crashed"}))})

    async def run():
        db, store = await make_store()
        f = Fetcher(store, Metrics())
        r = await f.try_solver(ok)
        assert r["ok"] and r["cookies"] == 2 and r["target"] == "https://fs1.app/"
        assert not f.domains[0].cookies  # 只是检测，不注入 cookie
        r = await f.try_solver(challenged)
        assert not r["ok"] and r["challenge"] and r["status"] == 403
        r = await f.try_solver(broken)
        assert not r["ok"] and "500" in r["error"]
        await db.close()

    asyncio.run(run())
