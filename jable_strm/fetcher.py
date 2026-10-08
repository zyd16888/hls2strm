"""分层抓取器。

每个站点一条抓取通道（SiteFetcher），域名、冷却、限速各管各的；所有通道共用一个 curl_cffi 会话（代理、指纹）。
L1：curl_cffi 模拟浏览器指纹，按顺序轮换该站的多个域名；某个域名被拦就冷却（连续被拦翻倍），切到下一个。
L2：全部域名都被拦时，如果配置了 Byparr / FlareSolverr，用它拿 cookie 和页面；成功后 cookie 注入 L1 会话。
全部失败抛 Blocked，由任务引擎暂停这个站点，等冷却结束。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession

from .config import Settings, SettingsStore, SiteConfig
from .observability import Metrics
from .sites import SITES, Site

log = logging.getLogger(__name__)

MAX_COOLDOWN = 3600


class FetchError(Exception):
    """可重试的失败：网络错误、超时、5xx 等。"""


class NotFound(Exception):
    """页面不存在（404），不再重试。"""


class Blocked(Exception):
    """所有渠道都被拦截。"""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = max(5.0, retry_after)


@dataclass
class Page:
    html: str
    url: str
    domain: str
    via: str = "curl"


@dataclass
class DomainState:
    base: str
    cooldown_until: float = 0.0
    block_streak: int = 0
    ok: int = 0
    blocked: int = 0
    errors: int = 0
    last_status: str = ""
    last_ok_at: float = 0.0
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""

    @property
    def host(self) -> str:
        return urlsplit(self.base).hostname or self.base

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cookies")
        d["host"] = self.host
        d["cooling"] = max(0, int(self.cooldown_until - time.time()))
        d["solver_cookie"] = bool(self.cookies)
        return d


class RateLimiter:
    """按间隔放行的限速器（带 ±20% 抖动）；被拦时速率减半，连续成功后缓慢回升到上限。"""

    def __init__(self, limit: float) -> None:
        self.limit = limit
        self.rate = limit
        self._next_at = 0.0
        self._streak = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + random.uniform(0.8, 1.2) / self.rate

    def penalize(self) -> None:
        self.rate = max(self.limit / 16, self.rate / 2)
        self._streak = 0

    def slow_down(self) -> None:
        """超时等软错误：降到 3/4，可能是对方在软限流。"""
        self.rate = max(self.limit / 16, self.rate * 0.75)
        self._streak = 0

    def reward(self) -> None:
        self._streak += 1
        if self._streak >= 30 and self.rate < self.limit:
            self.rate = min(self.limit, self.rate * 1.25)
            self._streak = 0

    def set_limit(self, limit: float) -> None:
        self.limit = limit
        self.rate = limit
        self._streak = 0


def looks_like_challenge(html: str) -> bool:
    head = html[:8000]
    return "<title>Just a moment" in head or "_cf_chl_opt" in head or "<title>请稍候" in head


def split_proxy(proxy: str) -> tuple[str, str, str]:
    """'socks5://u:p@h:1080' -> ('socks5://h:1080', 'u', 'p')。"""
    parts = urlsplit(proxy)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    server = urlunsplit((parts.scheme, netloc, "", "", ""))
    return server, parts.username or "", parts.password or ""


@dataclass
class SolverResult:
    status: int
    html: str
    url: str
    cookies: dict[str, str]
    user_agent: str


async def call_solver(solver_url: str, url: str, proxy: str, timeout: int) -> SolverResult:
    """调用 Byparr / FlareSolverr 的 /v1 接口。"""
    body: dict = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}
    headers = {}
    if proxy:
        server, user, password = split_proxy(proxy)
        body["proxy"] = {"url": server, **({"username": user, "password": password} if user else {})}
        headers["X-Proxy-Server"] = server
        if user:
            headers["X-Proxy-Username"] = user
            headers["X-Proxy-Password"] = password
    async with AsyncSession(trust_env=False) as s:
        resp = await s.post(f"{solver_url}/v1", json=body, headers=headers, timeout=timeout + 15)
    if resp.status_code != 200:
        raise FetchError(f"解题服务返回 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    sol = data.get("solution") or {}
    if data.get("status") != "ok" or not sol:
        raise FetchError(f"解题失败：{data.get('message', '')[:200]}")
    cookies = {c["name"]: c["value"] for c in sol.get("cookies") or [] if "name" in c}
    return SolverResult(
        status=int(sol.get("status") or 0),
        html=sol.get("response") or "",
        url=sol.get("url") or url,
        cookies=cookies,
        user_agent=sol.get("userAgent") or "",
    )


_CONN_HINTS = (
    ("curl: (6)", "域名解析失败：主机名不对，或者和本服务不在同一个 Docker 网络"),
    ("curl: (7)", "连接被拒绝：端口不对，或者服务没有启动"),
    ("curl: (28)", "连接超时：地址不可达，或者被防火墙拦了"),
    ("curl: (35)", "TLS 握手失败：http / https 写反了"),
)


def explain_conn_error(e: Exception) -> str:
    """把 curl 的连接错误换成看得懂的提示，后面附原始错误码。"""
    text = str(e)
    for code, hint in _CONN_HINTS:
        if code in text:
            return f"{hint}（curl 错误 {code.removeprefix('curl: (').rstrip(')')}）"
    return text[:300]


async def ping_solver(solver_url: str, timeout: int = 10) -> dict:
    """检查解题服务能不能连上，并尽量识别类型和版本。

    FlareSolverr 的 / 直接返回 {"msg": "FlareSolverr is ready!", "version": ...}；
    Byparr 是 FastAPI，/ 跳到 /docs，名称和版本在 /openapi.json 里。
    """
    t0 = time.monotonic()
    try:
        async with AsyncSession(trust_env=False) as s:
            resp = await s.get(f"{solver_url}/", timeout=timeout, allow_redirects=True)
            ms = int((time.monotonic() - t0) * 1000)
            info: dict = {"ok": resp.status_code < 500, "status": resp.status_code, "ms": ms,
                          "service": "", "version": ""}
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("msg"):
                    info["service"] = str(data["msg"])
                    info["version"] = str(data.get("version") or "")
                    return info
            except Exception:
                pass
            spec = await s.get(f"{solver_url}/openapi.json", timeout=timeout)
            if spec.status_code == 200:
                try:
                    meta = spec.json().get("info") or {}
                    info["service"] = str(meta.get("title") or "")
                    info["version"] = str(meta.get("version") or "")
                except Exception:
                    pass
    except Exception as e:
        return {"ok": False, "error": explain_conn_error(e), "ms": int((time.monotonic() - t0) * 1000)}
    if info["ok"] and not info["service"]:
        info["warning"] = "能连上，但看起来不像 Byparr / FlareSolverr，确认地址和端口"
    return info


class Fetcher:
    """共享的会话 + 各站点的抓取通道；CDN、封面、播放器页这些站外请求也走这里。"""

    def __init__(self, store: SettingsStore, metrics: Metrics) -> None:
        self.store = store
        self.metrics = metrics
        self._session: AsyncSession | None = None
        self.sites: dict[str, SiteFetcher] = {name: SiteFetcher(self, site) for name, site in SITES.items()}
        store.on_change(self._on_settings)

    def site(self, name: str) -> SiteFetcher:
        sf = self.sites.get(name or "jable")
        if sf is None:
            raise ValueError(f"未知站点：{name}")
        return sf

    # ---- 会话与设置 ----

    def _on_settings(self, old: Settings, new: Settings) -> None:
        if old.proxy != new.proxy or old.impersonate != new.impersonate:
            self._reset_session()
        for name, sf in self.sites.items():
            sf.sync(old.site(name), new.site(name))

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            s = self.store.current
            self._session = AsyncSession(
                impersonate=s.impersonate or "chrome",
                proxy=s.proxy or None,
                trust_env=False,
                max_clients=32,
            )
        return self._session

    def _reset_session(self) -> None:
        old, self._session = self._session, None
        if old is not None:
            asyncio.get_running_loop().create_task(self._close_later(old))

    @staticmethod
    async def _close_later(session: AsyncSession) -> None:
        await asyncio.sleep(120)  # 等进行中的请求（例如播放中转）结束
        await session.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def reset_cooldowns(self, site: str | None = None) -> None:
        for name, sf in self.sites.items():
            if site in (None, name):
                sf.reset_cooldowns()

    async def test_domains(self, site: str | None = None) -> list[dict]:
        """测试启用站点的域名连通性（site 指定时只测这个站）。"""
        out = []
        for name, sf in self.sites.items():
            if site in (None, name) and (site or sf.cfg.enabled):
                out += [{"site": name, **r} for r in await sf.test_domains()]
        return out

    # ---- 站外请求：CDN、封面、播放器页 ----

    async def get_bytes(self, url: str, *, referer: str | None = None, headers: dict | None = None) -> bytes:
        headers = dict(headers or {})
        if referer:
            headers["Referer"] = referer
        try:
            resp = await self.session.get(url, timeout=self.store.current.request_timeout, headers=headers or None)
        except Exception as e:
            raise FetchError(f"{urlsplit(url).hostname}: {e}") from e
        if resp.status_code == 404:
            raise NotFound(url)
        if resp.status_code != 200:
            raise FetchError(f"{urlsplit(url).hostname}: HTTP {resp.status_code}")
        return resp.content


class SiteFetcher:
    """一个站点的抓取通道：域名轮换、冷却、限速、解题服务。"""

    def __init__(self, parent: Fetcher, site: Site) -> None:
        self.parent = parent
        self.site = site
        self.name = site.name
        self.metrics = parent.metrics
        cfg = self.cfg
        self.domains = [DomainState(base) for base in cfg.domains]
        self.limiter = RateLimiter(cfg.rate_per_sec)
        self._solver_lock = asyncio.Lock()

    @property
    def cfg(self) -> SiteConfig:
        return self.parent.store.current.site(self.name)

    @property
    def session(self) -> AsyncSession:
        return self.parent.session

    def sync(self, old: SiteConfig, new: SiteConfig) -> None:
        if old.domains != new.domains:
            existing = {d.base: d for d in self.domains}
            self.domains = [existing.get(base) or DomainState(base) for base in new.domains]
        if old.rate_per_sec != new.rate_per_sec:
            self.limiter.set_limit(new.rate_per_sec)

    def reset_cooldowns(self) -> None:
        for d in self.domains:
            d.cooldown_until = 0
            d.block_streak = 0
        self.limiter.set_limit(self.cfg.rate_per_sec)

    def blocked_for(self) -> float:
        """距离最早一个域名解除冷却还有多少秒；有可用域名时为 0。"""
        now = time.time()
        if not self.domains or any(d.cooldown_until <= now for d in self.domains):
            return 0.0
        return min(d.cooldown_until for d in self.domains) - now

    def status(self) -> dict:
        cfg = self.cfg
        return {
            "name": self.name,
            "label": self.site.label,
            "enabled": cfg.enabled,
            "direct": self.site.stream.direct,
            "domains": [d.to_dict() for d in self.domains],
            "rate": {"limit": self.limiter.limit, "current": round(self.limiter.rate, 3)},
            "concurrency": cfg.concurrency,
            "blocked_for": int(self.blocked_for()),
        }

    # ---- 站点页面 ----

    @staticmethod
    def _to_path(path_or_url: str) -> str:
        if "://" in path_or_url:
            p = urlsplit(path_or_url)
            return p.path + (f"?{p.query}" if p.query else "")
        return path_or_url if path_or_url.startswith("/") else "/" + path_or_url

    def _mark_blocked(self, dom: DomainState, reason: str) -> None:
        dom.blocked += 1
        dom.block_streak += 1
        cool = min(MAX_COOLDOWN, self.parent.store.current.domain_cooldown * 2 ** (dom.block_streak - 1))
        dom.cooldown_until = time.time() + cool
        dom.last_status = f"被拦截（{reason}），冷却 {cool}s"
        self.limiter.penalize()
        self.metrics.inc("fetch_blocked")
        log.warning("%s 被拦截（%s），冷却 %ss，限速降到 %.2f 次/秒", dom.host, reason, cool, self.limiter.rate)

    async def get_page(self, path: str, *, priority: bool = False) -> Page:
        """抓取站点页面。priority=True 时跳过限速（播放请求用）。"""
        path = self._to_path(path)
        s = self.parent.store.current
        now = time.time()
        candidates = [d for d in self.domains if d.cooldown_until <= now]
        errors: list[str] = []
        for dom in candidates:
            if not priority:
                await self.limiter.acquire()
            url = dom.base + path
            headers = {"User-Agent": dom.user_agent} if dom.user_agent else None
            try:
                resp = await self.session.get(
                    url, timeout=s.request_timeout, headers=headers, cookies=dom.cookies or None
                )
            except Exception as e:
                dom.errors += 1
                dom.last_status = f"网络错误：{e}"[:200]
                self.metrics.inc("fetch_error")
                self.limiter.slow_down()
                errors.append(f"{dom.host}: {e}")
                log.info("请求失败 %s：%s", url, e)
                continue
            self.metrics.request()
            status = resp.status_code
            mitigated = (resp.headers.get("cf-mitigated") or "").lower()
            if "challenge" in mitigated or status in (403, 429, 503) or looks_like_challenge(resp.text):
                self._mark_blocked(dom, f"HTTP {status}" + (" challenge" if mitigated else ""))
                continue
            if status == 404:
                dom.ok += 1
                self.metrics.inc("fetch_404")
                raise NotFound(url)
            if status != 200:
                dom.errors += 1
                dom.last_status = f"HTTP {status}"
                self.metrics.inc("fetch_error")
                errors.append(f"{dom.host}: HTTP {status}")
                continue
            dom.ok += 1
            dom.block_streak = 0
            dom.last_ok_at = time.time()
            dom.last_status = "正常"
            self.limiter.reward()
            self.metrics.inc("fetch_ok")
            return Page(resp.text, str(resp.url), dom.base)

        if errors:
            raise FetchError("；".join(errors))
        page = await self._solve(path)
        if page is not None:
            return page
        raise Blocked(f"{self.site.label} 的域名都被拦截", self.blocked_for())

    async def _solve(self, path: str) -> Page | None:
        s = self.parent.store.current
        if not s.solver_url or not self.cfg.solver or not self.domains:
            return None
        async with self._solver_lock:  # 解题慢且占资源，串行
            dom = self.domains[0]
            url = dom.base + path
            log.info("%s 的域名全部被拦，调用解题服务：%s", self.site.label, url)
            try:
                result = await call_solver(s.solver_url, url, s.proxy, s.solver_timeout)
            except Exception as e:
                self.metrics.inc("solver_error")
                log.warning("解题服务失败：%s", e)
                return None
            if result.status == 404:
                raise NotFound(url)
            if result.status != 200 or looks_like_challenge(result.html):
                self.metrics.inc("solver_fail")
                log.warning("解题服务未能通过挑战（HTTP %s）", result.status)
                return None
            dom.cookies = result.cookies
            dom.user_agent = result.user_agent
            dom.cooldown_until = 0
            dom.block_streak = 0
            dom.last_status = "解题成功，已注入 cookie"
            self.metrics.inc("solver_ok")
            log.info("解题成功，%s 恢复使用（cookie %d 个）", dom.host, len(result.cookies))
            return Page(result.html, result.url, dom.base, via="solver")

    async def try_solver(self, solver_url: str) -> dict:
        """让解题服务实际打开一次首选域名首页（走当前代理），只返回结果，不注入 cookie。"""
        s = self.parent.store.current
        target = self.domains[0].base + "/"
        t0 = time.monotonic()
        try:
            r = await call_solver(solver_url, target, s.proxy, s.solver_timeout)
        except Exception as e:
            return {"ok": False, "target": target, "error": explain_conn_error(e),
                    "ms": int((time.monotonic() - t0) * 1000)}
        passed = r.status == 200 and not looks_like_challenge(r.html)
        return {"ok": passed, "target": target, "status": r.status, "challenge": not passed,
                "cookies": len(r.cookies), "user_agent": r.user_agent, "ms": int((time.monotonic() - t0) * 1000)}

    async def test_domains(self) -> list[dict]:
        """逐个测试域名连通性（忽略冷却），成功的解除冷却。"""
        out = []
        for dom in self.domains:
            t0 = time.monotonic()
            try:
                resp = await self.session.get(dom.base + "/", timeout=self.parent.store.current.request_timeout,
                                              headers={"User-Agent": dom.user_agent} if dom.user_agent else None,
                                              cookies=dom.cookies or None)
                ms = int((time.monotonic() - t0) * 1000)
                blocked = "challenge" in (resp.headers.get("cf-mitigated") or "") or looks_like_challenge(resp.text)
                ok = resp.status_code == 200 and not blocked
                if ok:
                    dom.cooldown_until = 0
                    dom.block_streak = 0
                    dom.last_status = "正常"
                out.append({"host": dom.host, "status": resp.status_code, "blocked": blocked, "ok": ok, "ms": ms})
            except Exception as e:
                out.append({"host": dom.host, "status": 0, "blocked": False, "ok": False, "error": str(e)[:200]})
        return out
