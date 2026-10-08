import asyncio
import time

import pytest

from jable_strm.fetcher import Blocked, Fetcher, FetchError, NotFound, RateLimiter, split_proxy
from jable_strm.observability import Metrics


class Resp:
    def __init__(self, status=200, text="<html>ok</html>", headers=None, url=""):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.content = text.encode()


class FakeSession:
    """按域名返回预设响应。"""

    def __init__(self, by_host):
        self.by_host = by_host
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append(url)
        host = url.split("/")[2]
        r = self.by_host[host]
        if isinstance(r, Exception):
            raise r
        r.url = url
        return r

    async def close(self):
        pass


CHALLENGE = Resp(403, "<html><head><title>Just a moment...</title>", {"cf-mitigated": "challenge"})


def make_fetcher(store, by_host):
    """返回 Jable 站点的抓取通道，底层会话换成假的。"""
    f = Fetcher(store, Metrics())
    f._session = FakeSession(by_host)
    return f.site("jable")


def test_rotates_to_next_domain_when_blocked(make_store):
    async def run():
        db, store = await make_store()
        await store.update({"sites": {"jable": {"rate_per_sec": 20}}})
        f = make_fetcher(store, {"fs1.app": CHALLENGE, "jable.tv": Resp(200, "<html>page</html>")})
        page = await f.get_page("/videos/abc-1/")
        assert page.domain == "https://jable.tv"
        fs1 = f.domains[0]
        assert fs1.blocked == 1 and fs1.cooldown_until > time.time()
        # 冷却中的域名不再请求
        await f.get_page("/videos/abc-2/")
        assert f.parent._session.calls[-1].startswith("https://jable.tv")
        await db.close()

    asyncio.run(run())


def test_all_blocked_and_errors(make_store):
    async def run():
        db, store = await make_store()
        await store.update({"sites": {"jable": {"rate_per_sec": 20}}})
        f = make_fetcher(store, {"fs1.app": CHALLENGE, "jable.tv": CHALLENGE})
        with pytest.raises(Blocked) as e:
            await f.get_page("/x/")
        assert e.value.retry_after >= 5
        assert f.blocked_for() > 0
        f.reset_cooldowns()
        assert f.blocked_for() == 0

        f = make_fetcher(store, {"fs1.app": OSError("timeout"), "jable.tv": Resp(502)})
        with pytest.raises(FetchError):
            await f.get_page("/x/")

        f = make_fetcher(store, {"fs1.app": Resp(404), "jable.tv": Resp(200)})
        with pytest.raises(NotFound):
            await f.get_page("/videos/gone/")
        await db.close()

    asyncio.run(run())


def test_challenge_page_with_200_is_blocked(make_store):
    async def run():
        db, store = await make_store()
        await store.update({"sites": {"jable": {"rate_per_sec": 20}}})
        f = make_fetcher(store, {"fs1.app": Resp(200, "<title>Just a moment...</title>"), "jable.tv": Resp(200)})
        assert (await f.get_page("/")).domain == "https://jable.tv"
        await db.close()

    asyncio.run(run())


def test_rate_limiter_adapts():
    rl = RateLimiter(2.0)
    rl.penalize()
    assert rl.rate == 1.0
    for _ in range(30):
        rl.reward()
    assert rl.rate == 1.25
    rl.set_limit(4)
    assert rl.rate == 4


def test_split_proxy():
    assert split_proxy("socks5://u:p@1.2.3.4:1080") == ("socks5://1.2.3.4:1080", "u", "p")
    assert split_proxy("http://h:8080") == ("http://h:8080", "", "")
