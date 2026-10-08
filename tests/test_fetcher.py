import asyncio
import time

import pytest

import hls2strm.fetcher as fetcher_mod
from hls2strm.fetcher import Blocked, Fetcher, FetchError, NotFound, RateLimiter, SolverResult, split_proxy
from hls2strm.observability import Metrics


class Resp:
    def __init__(self, status=200, text="<html>ok</html>", headers=None, url=""):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.content = text.encode()


class FakeSession:
    """按域名返回预设响应；预设是函数时按 (地址, 请求参数) 现算。"""

    def __init__(self, by_host):
        self.by_host = by_host
        self.calls = []
        self.kwargs = []

    async def get(self, url, **kw):
        self.calls.append(url)
        self.kwargs.append(kw)
        host = url.split("/")[2]
        r = self.by_host[host]
        if callable(r):
            r = r(url, kw)
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
        # 停放域名的跳板：200 但只有一段跳转，不是站点内容
        f = make_fetcher(store, {"fs1.app": Resp(200, STUB), "jable.tv": Resp(200)})
        assert (await f.get_page("/")).domain == "https://jable.tv"
        assert f.domains[0].blocked == 1 and f.domains[0].cooldown_until > time.time()
        await db.close()

    asyncio.run(run())


STUB = "<html><head><title>Loading...</title></head><body><script>window.location.replace('/?ch=1');</script></body></html>"


def fake_solver(monkeypatch, results, delay=0.0):
    """call_solver 换成假的：按调用顺序返回 results（异常就抛），返回记下的请求地址。"""
    calls = []

    async def solve(solver_url, url, proxy, timeout):
        calls.append(url)
        await asyncio.sleep(delay)
        r = results[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(fetcher_mod, "call_solver", solve)
    return calls


def needs_cookie(url, kw):
    """带上解题拿到的 cookie 才放行。"""
    return Resp(200, "<html>page</html>") if (kw.get("cookies") or {}).get("cf_clearance") == "c" else CHALLENGE


def test_solver_right_after_block(make_store, monkeypatch):
    """被拦了当场解题：过了接着用这个域名，不冷却、不降速、不换域名；之后 curl_cffi 带 cookie 和 UA 抓。"""

    async def run():
        db, store = await make_store()
        await store.update({"solver_url": "http://byparr:8191", "sites": {"jable": {"rate_per_sec": 20}}})
        calls = fake_solver(monkeypatch, [SolverResult(200, "<html>solved</html>", "https://fs1.app/x/",
                                                       {"cf_clearance": "c"}, "UA-1")])
        f = make_fetcher(store, {"fs1.app": needs_cookie, "jable.tv": Resp(200)})
        page = await f.get_page("/x/")
        assert (page.via, page.domain, page.html) == ("solver", "https://fs1.app", "<html>solved</html>")
        dom = f.domains[0]
        assert dom.cooldown_until == 0 and dom.blocked == 0 and f.limiter.rate == f.limiter.limit
        page = await f.get_page("/y/")
        assert (page.via, page.domain) == ("curl", "https://fs1.app")
        assert calls == ["https://fs1.app/x/"]
        session = f.parent._session
        assert all("jable.tv" not in u for u in session.calls)
        assert session.kwargs[-1]["headers"] == {"User-Agent": "UA-1"}
        await db.close()

    asyncio.run(run())


def test_solver_fails_then_cools_and_rotates(make_store, monkeypatch):
    """解题没过（还是挑战页、落到别的站）才冷却、换下一个域名；域名都在冷却时再给解题服务一次机会。429 不解题。"""

    async def run():
        db, store = await make_store()
        await store.update({"solver_url": "http://byparr:8191", "sites": {"jable": {"rate_per_sec": 20}}})
        calls = fake_solver(monkeypatch, [
            SolverResult(403, CHALLENGE.text, "https://fs1.app/x/", {}, ""),
            SolverResult(200, "<html>ads</html>", "http://ads.example/visit", {}, ""),  # 停放域名把访客引走
            SolverResult(200, "<html>solved</html>", "https://fs1.app/x/", {"cf_clearance": "c"}, "UA-1"),
        ])
        f = make_fetcher(store, {"fs1.app": CHALLENGE, "jable.tv": Resp(200, STUB)})
        with pytest.raises(Blocked):
            await f.get_page("/x/")
        assert calls == ["https://fs1.app/x/", "https://jable.tv/x/"]
        assert all(d.cooldown_until > time.time() for d in f.domains)
        page = await f.get_page("/x/")  # 都在冷却：试首选域名
        assert (page.via, page.domain) == ("solver", "https://fs1.app") and f.domains[0].cooldown_until == 0

        calls = fake_solver(monkeypatch, [])
        f = make_fetcher(store, {"fs1.app": Resp(429), "jable.tv": Resp(200)})
        assert (await f.get_page("/x/")).domain == "https://jable.tv" and calls == []
        await db.close()

    asyncio.run(run())


def test_solver_once_for_concurrent_blocks(make_store, monkeypatch):
    """几个请求同时被拦：排队等解题的请求先带前一个解出来的 cookie 再试，不重复解题。"""

    async def run():
        db, store = await make_store()
        await store.update({"solver_url": "http://byparr:8191"})
        calls = fake_solver(monkeypatch, [SolverResult(200, "<html>solved</html>", "https://fs1.app/a/",
                                                       {"cf_clearance": "c"}, "UA-1")], delay=0.05)
        f = make_fetcher(store, {"fs1.app": needs_cookie, "jable.tv": Resp(200)})
        a, b = await asyncio.gather(f.get_page("/a/", priority=True), f.get_page("/b/", priority=True))
        assert (a.via, b.via) == ("solver", "curl") and b.domain == "https://fs1.app"
        assert calls == ["https://fs1.app/a/"]
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
