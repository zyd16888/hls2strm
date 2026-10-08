"""播放连通性：各播放站（CDN）能不能播、快不快，挑源时连通性好的排前面。

按播放站打分，不按单部影片：同一个播放站的连通性基本一致，按它评就够了，也不用为了检测把每部片都访问一遍。
单线路站点的播放站就是站点本身（jable、missav）；多线路站点按线路背后的播放站（vidhide、voe、streamtape……）。

数据来源：
  真实播放  真的去取了地址（缓存命中不算）成功、失败，中转时 CDN 出错；站点拦截不算（那是源站的事）
  定时检测  隔一阵给每个播放站抽几部最近播过的片：取地址（有现成没过期的就不访问源站）、读播放列表、
           下载一个分片的开头一段，记首字节时间和速度
分数是越新权重越高的成功率（指数平均），分档：正常 / 慢 / 不稳 / 不通；太久没有新数据的当作不知道（不再压后）。
服务器测的是本服务到 CDN 的网络：中转时完全准；302 时客户端的网络可能不一样，只能参考。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from .fetcher import FetchError, close_stream

if TYPE_CHECKING:
    from .config import SettingsStore
    from .fetcher import Fetcher
    from .sites import Site

TIERS = ("正常", "慢", "不稳", "不通")
ALPHA = 0.3  # 指数平均里新数据的权重
FORGET_AFTER = 6 * 3600  # 这么久没有新数据，当作不知道


@dataclass
class HostHealth:
    key: str
    score: float = 1.0  # 成功率的指数平均
    kbps: float = 0.0  # 检测测到的速度（指数平均）
    ttfb_ms: float = 0.0  # 检测测到的首字节时间（指数平均）
    ok: int = 0
    fail: int = 0
    streak: int = 0  # 连续失败次数
    last_ok_at: int = 0
    last_fail_at: int = 0
    last_error: str = ""
    checked_at: int = 0  # 上次定时检测（或手动检测）


def host_key(site: Site, line: dict | None = None, line_name: str = "") -> str:
    """源、线路属于哪个播放站：单线路站点是站点本身；多线路按线路实际的播放站，没取过直链按设置里的已知播放站。"""
    if not site.multi_line:
        return site.name
    name = line["line"] if line else line_name
    spec = site.line_specs.get(name)
    host = (line or {}).get("host") or (spec.host if spec else "")
    return host if host and host != "auto" else f"{site.name}:{name}"


def host_label(key: str) -> str:
    from .sites import SITES
    from .sites.hosts import HOST_LABELS

    if key in SITES:
        return SITES[key].label
    if ":" in key:
        site, line = key.split(":", 1)
        return f"{SITES[site].label if site in SITES else site} {line}"
    return HOST_LABELS.get(key, key)


class HealthTracker:
    """各播放站的连通性，放在内存里，引擎定时存进数据库、启动时读回来。"""

    def __init__(self, store: SettingsStore) -> None:
        self.store = store
        self.hosts: dict[str, HostHealth] = {}
        self.dirty = False

    def load(self, rows: list[dict]) -> None:
        names = {f.name for f in fields(HostHealth)}
        self.hosts = {r["key"]: HostHealth(**{k: v for k, v in r.items() if k in names}) for r in rows}

    def dump(self) -> list[dict]:
        self.dirty = False
        return [asdict(h) for h in self.hosts.values()]

    def record(self, key: str, ok: bool, *, error: str = "", ttfb_ms: float | None = None,
               kbps: float | None = None, checked: bool = False) -> None:
        h = self.hosts.setdefault(key, HostHealth(key))
        t = int(time.time())
        if not h.ok and not h.fail:
            h.score = 1.0 if ok else 0.0
        else:
            h.score = (1 - ALPHA) * h.score + ALPHA * (1.0 if ok else 0.0)
        if ok:
            h.ok += 1
            h.streak = 0
            h.last_ok_at = t
        else:
            h.fail += 1
            h.streak += 1
            h.last_fail_at = t
            h.last_error = error[:300]
        if kbps is not None:
            h.kbps = kbps if not h.kbps else (1 - ALPHA) * h.kbps + ALPHA * kbps
        if ttfb_ms is not None:
            h.ttfb_ms = ttfb_ms if not h.ttfb_ms else (1 - ALPHA) * h.ttfb_ms + ALPHA * ttfb_ms
        if checked:
            h.checked_at = t
        self.dirty = True

    def touch(self, key: str) -> None:
        """记下检测过（结果已经在别处记了）。"""
        self.hosts.setdefault(key, HostHealth(key)).checked_at = int(time.time())
        self.dirty = True

    def tier(self, key: str) -> int:
        """0 正常（也包括还不知道的）/ 1 慢 / 2 不稳 / 3 不通。"""
        h = self.hosts.get(key)
        if h is None or not (h.ok or h.fail) or time.time() - max(h.last_ok_at, h.last_fail_at) > FORGET_AFTER:
            return 0
        if h.streak >= 3 or h.score < 0.5:
            return 3
        if h.score < 0.8:
            return 2
        slow = self.store.current.health_slow_kbps
        return 1 if slow and h.kbps and h.kbps < slow else 0

    def source_tier(self, site: Site) -> int:
        """一个源的档：单线路看站点；多线路看启用的线路里最好的那条（真正播哪条由线路排序决定）。"""
        if not site.multi_line:
            return self.tier(site.name)
        cfg = self.store.current.site(site.name)
        tiers = [self.tier(host_key(site, None, n)) for n, spec in site.line_specs.items()
                 if cfg.line(n).enabled and spec.supported]
        return min(tiers, default=0)

    def view(self) -> list[dict]:
        out = []
        for h in sorted(self.hosts.values(), key=lambda x: x.key):
            t = self.tier(h.key)
            out.append({**asdict(h), "label": host_label(h.key), "tier": t, "tier_name": TIERS[t]})
        return out


async def measure(fetcher: Fetcher, url: str, headers: dict | None, nbytes: int) -> tuple[float, float]:
    """下载一小段视频测首字节时间（毫秒）和速度（kbps）：HLS 取中间的一个分片（多码率的先进第一档），mp4 从头取。"""
    target = url
    if url.split("?", 1)[0].lower().endswith(".m3u8"):
        text = (await fetcher.get_bytes(url, headers=headers)).decode("utf-8", "replace")
        base = url
        if "#EXT-X-STREAM-INF" in text:
            sub = next((ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")), "")
            if not sub:
                raise FetchError("主播放列表里没有子清单")
            base = urljoin(url, sub)
            text = (await fetcher.get_bytes(base, headers=headers)).decode("utf-8", "replace")
        segs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not segs:
            raise FetchError("播放列表里没有分片")
        target = urljoin(base, segs[len(segs) // 2])
    req = {**(headers or {}), "Range": f"bytes=0-{nbytes - 1}"}
    t0 = time.monotonic()
    try:
        resp = await fetcher.session.get(target, stream=True, headers=req,
                                         timeout=fetcher.store.current.request_timeout)
    except Exception as e:
        raise FetchError(f"请求分片失败：{e}") from e
    try:
        if resp.status_code not in (200, 206):
            raise FetchError(f"分片返回 HTTP {resp.status_code}")
        got, first = 0, 0.0
        async for chunk in resp.aiter_content():
            first = first or time.monotonic()
            got += len(chunk)
            if got >= nbytes:
                break
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"下载分片中断：{e}") from e
    finally:
        await close_stream(resp)
    if not got:
        raise FetchError("分片是空的")
    secs = max(time.monotonic() - first, 0.001)
    return (first - t0) * 1000, got * 8 / secs / 1000
