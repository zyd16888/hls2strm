"""Web 控制台登录：会话 cookie、Basic 兼容、改密码后会话失效、防暴力猜。"""

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from hls2strm import auth as auth_module
from hls2strm.app import create_app
from hls2strm.auth import Auth
from hls2strm.config import BootConfig


def _client(tmp_path, password="pw"):
    return create_app(BootConfig(data_dir=tmp_path, ui_user="admin", ui_password=password))


def _cleanup():
    for h in logging.getLogger().handlers:
        h.close()
    logging.getLogger().handlers.clear()


def test_login_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, "FAILURE_DELAY", 0)
    app = _client(tmp_path)
    try:
        with TestClient(app) as c:
            # 没登录：接口 401，但不带 WWW-Authenticate（浏览器不弹原生登录框）；页面本身能打开
            r = c.get("/api/status")
            assert r.status_code == 401 and "www-authenticate" not in r.headers
            assert c.get("/api/session").json() == {"required": True, "user": None}
            assert c.get("/").status_code in (200, 503)

            r = c.post("/api/login", json={"username": "admin", "password": "wrong"})
            assert r.status_code == 401 and "hls2strm_session" not in r.cookies

            r = c.post("/api/login", json={"username": "admin", "password": "pw", "remember": True})
            assert r.status_code == 200 and r.json() == {"user": "admin"}
            cookie = r.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=2592000" in cookie
            assert c.get("/api/status").status_code == 200
            assert c.get("/api/session").json() == {"required": True, "user": "admin"}

            c.post("/api/logout")
            assert c.get("/api/status").status_code == 401

            # 脚本用的 HTTP Basic 照样能用
            assert c.get("/api/status", auth=("admin", "pw")).status_code == 200
            assert c.get("/api/status", auth=("admin", "x")).status_code == 401
    finally:
        _cleanup()


def test_no_password_means_no_login(tmp_path):
    app = _client(tmp_path, password="")
    try:
        with TestClient(app) as c:
            assert c.get("/api/session").json() == {"required": False, "user": None}
            assert c.get("/api/status").status_code == 200
    finally:
        _cleanup()


def test_token_tied_to_password_and_expiry():
    def boot(pw):
        return BootConfig(data_dir=Path("."), ui_user="admin", ui_password=pw)

    a = Auth(boot("pw"), "secret")
    token = a.issue(60)
    assert a.verify(token) == "admin"
    assert a.verify(token[:-2] + "xx") is None  # 签名被改
    assert a.verify(a.issue(-1)) is None  # 过期
    # 改了密码（或者换了密钥），原来的会话失效
    assert Auth(boot("new"), "secret").verify(token) is None
    assert Auth(boot("pw"), "other").verify(token) is None


def test_too_many_failures_blocks_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, "FAILURE_DELAY", 0)
    monkeypatch.setattr(auth_module, "MAX_FAILURES", 3)
    app = _client(tmp_path)
    try:
        with TestClient(app) as c:
            for _ in range(3):
                assert c.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401
            r = c.post("/api/login", json={"username": "admin", "password": "pw"})
            assert r.status_code == 429 and "分钟后再试" in r.json()["detail"]
    finally:
        _cleanup()
