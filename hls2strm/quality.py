"""画质：从播放列表认出分辨率，不下载视频。

master 播放列表（多码率）每一档都写了 RESOLUTION，读一次（几 KB）就知道；只有一档、不写分辨率的媒体播放列表
（Jable），抽几个分片 HEAD 一下拿大小，除以时长得码率，按码率估档位。mp4 直链暂时不知道。
都只请求 CDN，不多访问源站。

画质的来源和可信度：master（主播放列表）> embed（播放页标注的档位）> estimate（按码率估）> claimed（站点标注）。
可信度低的结果不覆盖高的。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from .errors import FetchError, NotFound
from .runtime import background

if TYPE_CHECKING:
    from .db import Database
    from .fetcher import Fetcher

log = logging.getLogger(__name__)

TRUST = {"": 0, "claimed": 1, "estimate": 2, "embed": 3, "master": 4}
SOURCE_NAMES = {"master": "主播放列表", "embed": "播放页标注", "estimate": "按码率估计", "claimed": "站点标注"}
RETRY_AFTER = 7 * 86400  # 探测过但没认出来（mp4 等）的，多久后再试
MAX_PENDING = 200  # 后台排队等探测的上限，满了就不排（播放时还会再顺手探测）

_RES_RE = re.compile(r"RESOLUTION=\d+x(\d+)")
_BW_RE = re.compile(r"[:,]BANDWIDTH=(\d+)")
_BYTERANGE_RE = re.compile(r"#EXT-X-BYTERANGE:(\d+)")
# 按码率估档位（kbps → 高度）。实测 Jable 720p 的 H.264 约 1.8–2.2 Mbps
_KBPS_TIERS = ((3500, 1080), (1200, 720), (600, 480), (0, 360))


@dataclass
class Quality:
    heights: list[int]  # 各档分辨率的高度，从高到低
    src: str  # master / embed / estimate / claimed

    @property
    def height(self) -> int:
        return self.heights[0] if self.heights else 0


def label(height: int | None) -> str:
    if not height:
        return "未知"
    return "4K" if height >= 2000 else f"{height}p"


STANDARD_TIERS = (2160, 1440, 1080, 720, 480, 360, 240)


def tier(height: int) -> int:
    """归到常见档位：1920x800 这种宽银幕的算 720p 档。"""
    return min(STANDARD_TIERS, key=lambda t: (abs(t - height), -t))


def parse_label(text: str) -> int | None:
    """'720p' / '1080' / '4k' → 档位；认不出返回 None。"""
    s = text.strip().lower()
    if s in ("4k", "uhd"):
        return 2160
    m = re.fullmatch(r"(\d{3,4})p?", s)
    return tier(int(m.group(1))) if m else None


def pick_tier(available: set[int], want: int | None) -> int:
    """想要的档位：有就用它；没有用比它低里最高的；都比它高用最低的。want 为 None 取最高。"""
    if want is None or want in available:
        return max(available) if want is None else want
    lower = [t for t in available if t < want]
    return max(lower) if lower else min(available)


def filter_master(text: str, want: int | None) -> tuple[str, str] | None:
    """多码率主播放列表只留一档（want 档，没有就最接近的；None 取最高），返回（改好的播放列表, 这一档的子清单地址）。
    不是主播放列表、只有一档、或者有的档没写分辨率时返回 None（原样用）。"""
    lines = text.splitlines()
    variants: list[tuple[int, int, int | None]] = []  # (STREAM-INF 行, 子清单行, 档位)
    i = 0
    while i < len(lines):
        if not lines[i].startswith("#EXT-X-STREAM-INF"):
            i += 1
            continue
        m = _RES_RE.search(lines[i])
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
            j += 1
        if j < len(lines):
            variants.append((i, j, tier(int(m.group(1))) if m else None))
        i = j + 1
    tiers = {t for *_, t in variants}
    if len(variants) < 2 or None in tiers:
        return None
    target = pick_tier(tiers, want)
    drop: set[int] = set()
    uri = ""
    for i, j, t in variants:
        if t == target:
            uri = uri or lines[j].strip()
        else:
            drop.update(range(i, j + 1))
    return "\n".join(ln for k, ln in enumerate(lines) if k not in drop) + "\n", uri


def height_for_kbps(kbps: float) -> int:
    return next(h for floor, h in _KBPS_TIERS if kbps >= floor)


def parse_heights(text: str) -> list[int]:
    """'1080,720' / [1080, 720] → 去重、从高到低。"""
    vals = text.split(",") if isinstance(text, str) else text
    return sorted({int(x) for x in vals if str(x).strip().isdigit() and int(x) > 0}, reverse=True)


def from_master(text: str) -> Quality | None:
    """多码率主播放列表：有 RESOLUTION 按它，没有就按 BANDWIDTH 估。不是主播放列表返回 None。"""
    if "#EXT-X-STREAM-INF" not in text:
        return None
    if heights := parse_heights([int(h) for h in _RES_RE.findall(text)]):
        return Quality(heights, "master")
    if bws := [int(b) for b in _BW_RE.findall(text)]:
        return Quality(parse_heights([height_for_kbps(b / 1000) for b in bws]), "estimate")
    return None


def from_labels(labels: list[str], src: str) -> Quality | None:
    """'1080p' / '720P' / '4K' 这类标注 → 画质；认不出的标注忽略。"""
    heights = []
    for s in labels:
        s = s.strip().lower()
        if s in ("4k", "2160p", "uhd"):
            heights.append(2160)
        elif m := re.fullmatch(r"(\d{3,4})p", s):
            heights.append(int(m.group(1)))
    return Quality(parse_heights(heights), src) if heights else None


async def probe(fetcher: Fetcher, url: str, headers: dict | None = None, samples: int = 3) -> Quality | None:
    """读播放列表认出画质；单档媒体播放列表抽几个分片按码率估。不是 HLS（mp4 等）返回 None。"""
    if not url.split("?", 1)[0].lower().endswith(".m3u8"):
        return None
    text = (await getattr(fetcher, "get_playlist", fetcher.get_bytes)(url, headers=headers)).decode("utf-8", "replace")
    if (q := from_master(text)) is not None:
        return q
    segs: list[tuple[float, str, int]] = []  # (时长, 地址, 字节数：EXT-X-BYTERANGE 给了就不用再问)
    dur, size = 0.0, 0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line[8:].split(",", 1)[0])
            except ValueError:
                dur = 0.0
        elif m := _BYTERANGE_RE.match(line):
            size = int(m.group(1))
        elif line and not line.startswith("#"):
            segs.append((dur, urljoin(url, line.strip()), size))
            dur, size = 0.0, 0
    picks = [segs[len(segs) * (i + 1) // (samples + 1)] for i in range(samples)] if len(segs) > samples else segs
    rates = []
    for seconds, seg_url, size in picks:
        if seconds <= 0:
            continue
        if not size:
            resp = await fetcher.fetch(seg_url, method="HEAD", headers=headers)
            size = int(resp.headers.get("content-length") or 0) if resp.status_code == 200 else 0
        if size:
            rates.append(size * 8 / seconds / 1000)
    if not rates:
        return None
    return Quality([height_for_kbps(sum(rates) / len(rates))], "estimate")


def needed(row: dict) -> bool:
    """源或线路还要不要探测画质：还没有可靠的结果（只有站点标注或什么都没有），而且最近没试过。"""
    if TRUST.get(row.get("quality_src") or "", 0) >= TRUST["estimate"]:
        return False
    return not row.get("quality_at") or time.time() - row["quality_at"] > RETRY_AFTER


class QualityProber:
    """探测并记下画质；同一个源 / 线路同时只探一次，后台探测有并发和排队上限。"""

    def __init__(self, db: Database, fetcher: Fetcher, concurrency: int = 2, pending: int = MAX_PENDING) -> None:
        self.db = db
        self.fetcher = fetcher
        self.on_change: Callable[[int], Awaitable[None]] | None = None  # 源的各档画质变了（参数是源 id）
        self._busy: set[tuple[int, int | None]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency
        self.pending = pending

    def configure(self, concurrency: int, pending: int) -> None:
        if concurrency != self.concurrency:
            self._sem = asyncio.Semaphore(concurrency)
            self.concurrency = concurrency
        self.pending = pending

    async def save(self, source_id: int, line_id: int | None, q: Quality | None) -> None:
        """记下画质；源的各档变了就通知（多画质版本文件要跟着改）。"""
        if await self.db.set_quality(source_id, line_id, q) and self.on_change is not None:
            try:
                await self.on_change(source_id)
            except Exception:
                log.exception("画质变了之后更新多画质版本出错（源 #%d）", source_id)

    async def probe_and_save(self, source_id: int, line_id: int | None, url: str,
                             headers: dict | None = None) -> Quality | None:
        key = (source_id, line_id)
        if key in self._busy:
            return None
        self._busy.add(key)
        try:
            async with self._sem:
                try:
                    q = await probe(self.fetcher, url, headers)
                except (FetchError, NotFound) as e:
                    log.debug("探测画质失败 %s：%s", url, e)
                    return None
                except Exception as e:  # 多半在后台跑，出什么错都只记日志，不影响播放
                    log.warning("探测画质出错 %s：%s: %s", url, type(e).__name__, e)
                    return None
                await self.save(source_id, line_id, q)
                return q
        finally:
            self._busy.discard(key)

    def spawn(self, source_id: int, line_id: int | None, url: str, headers: dict | None = None) -> None:
        """放到后台探测，不耽误调用方（比如 302）。"""
        if (source_id, line_id) in self._busy or (self.pending and len(self._tasks) >= self.pending):
            return
        with background():
            task = asyncio.create_task(self.probe_and_save(source_id, line_id, url, headers))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel(self) -> None:
        """停止服务时取消还在后台排队的探测。"""
        for task in list(self._tasks):
            task.cancel()

    async def close(self) -> None:
        self.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
