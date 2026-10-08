"""Web 控制台登录：用户名密码换一个签名的会话 cookie，服务端不存会话。

- 签名密钥 = 数据库里的随机密钥 + 当前的用户名、密码：改了 HLS2STRM_UI_PASSWORD，已登录的会话全部失效
- 同一个来源 IP 连续输错密码会被暂时挡住，防止暴力猜
- 没设密码时不需要登录
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import BootConfig

COOKIE = "hls2strm_session"
REMEMBER_TTL = 30 * 86400  # 勾「记住我」：30 天
SESSION_TTL = 12 * 3600  # 不勾：浏览器关掉就失效，最长 12 小时
MAX_FAILURES = 10  # 同一 IP 在 FAILURE_WINDOW 内输错这么多次，暂时不让再试
FAILURE_WINDOW = 15 * 60
FAILURE_DELAY = 1.0  # 输错密码后多等一会儿再回应，拖慢暴力猜


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Auth:
    def __init__(self, boot: BootConfig, secret: str) -> None:
        self.boot = boot
        self._key = hmac.new(secret.encode(), f"{boot.ui_user}\0{boot.ui_password}".encode(), hashlib.sha256).digest()
        self._failures: dict[str, list[float]] = {}

    @property
    def required(self) -> bool:
        return bool(self.boot.ui_password)

    def check_password(self, user: str, password: str) -> bool:
        return secrets.compare_digest(user.encode(), self.boot.ui_user.encode()) and secrets.compare_digest(
            password.encode(), self.boot.ui_password.encode()
        )

    def issue(self, ttl: int) -> str:
        payload = _b64(json.dumps({"u": self.boot.ui_user, "exp": int(time.time()) + ttl}).encode())
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None) -> str | None:
        """有效返回用户名，否则 None。"""
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not secrets.compare_digest(sig, self._sign(payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if data.get("exp", 0) < time.time() or data.get("u") != self.boot.ui_user:
            return None
        return data["u"]

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    # ---- 防暴力猜 ----

    def blocked_for(self, ip: str) -> int:
        """这个 IP 还要等多少秒才能再试；0 表示可以试。"""
        now = time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW]
        self._failures[ip] = recent
        if len(recent) < MAX_FAILURES:
            return 0
        return int(FAILURE_WINDOW - (now - recent[0])) + 1

    def failed(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)


async def load_secret(db) -> str:
    """签名用的随机密钥：存在数据库里，重启后已登录的会话还有效。"""
    secret = await db.get_setting("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        await db.set_setting("session_secret", secret)
    return secret
