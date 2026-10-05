"""已有 strm 的扫描、纳管、批量改前缀与回滚。

扫描把目录下每个 .strm 分类：
  ours     本服务格式：…/play/{slug}.m3u8（域名不限）
  cdn      CDN 直链：/hls/{token}/{expires}/{n}/{videoId}/{videoId}.m3u8（大概率已过期）
  named    URL 不认识，但文件名或父目录里有番号（可能是别的片源，纳管需手动勾选）
  other    其他来源（115、alist 等），只参与统计和改前缀
  invalid  空文件
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .fetcher import NotFound
from .parser import VideoGone
from .writer import write_atomic

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

OURS_RE = re.compile(r"/play/([a-z0-9][a-z0-9._-]{0,80})\.m3u8$", re.I)
CDN_RE = re.compile(r"/hls/[^/]+/(\d{9,11})/\d+/(\d+)/\2\.m3u8$")
NAME_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])([0-9]{0,4}[A-Za-z][A-Za-z0-9]{1,9}(?:-[A-Za-z]{2,5})?-\d{2,8}(?:-[A-Za-z][A-Za-z0-9]{0,2})?)"
    r"(?![A-Za-z0-9])"
)
ADOPTABLE_KINDS = ("ours", "cdn", "named")


@dataclass
class StrmInfo:
    kind: str
    url: str
    prefix: str
    slug: str = ""
    video_id: int | None = None
    expired: bool = False


def first_url_line(content: str) -> str:
    for line in content.splitlines():
        s = line.strip().lstrip("﻿")
        if s and not s.startswith("#"):
            return s
    return ""


def url_prefix(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return f"{parts.scheme}://" if parts.scheme else ""


def guess_slug(path: Path) -> str:
    for name in (path.stem, path.parent.name):
        m = NAME_CODE_RE.search(name)
        if m:
            return m.group(1).lower()
    return ""


def classify(path: Path, content: str, now: float) -> StrmInfo:
    url = first_url_line(content)
    if not url:
        return StrmInfo("invalid", "", "")
    parts = urlsplit(url)
    prefix = url_prefix(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        if m := OURS_RE.search(parts.path):
            return StrmInfo("ours", url, prefix, m.group(1).lower())
        if m := CDN_RE.search(parts.path):
            return StrmInfo("cdn", url, prefix, guess_slug(path), int(m.group(2)), int(m.group(1)) < now)
    slug = guess_slug(path)
    return StrmInfo("named" if slug else "other", url, prefix, slug)


def scan_directory(
    root: Path,
    library_roots: list[tuple[int, Path]],
    managed: set[str],
    slug_by_id: dict[int, str],
    id_by_slug: dict[str, int],
    scan_id: int,
) -> list[tuple]:
    """遍历目录，返回 strm_files 的行。同步函数，放到线程里跑。"""
    now = time.time()
    roots = sorted(((os.path.normcase(str(r)) + os.sep, lid) for lid, r in library_roots), key=lambda x: -len(x[0]))
    rows = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(".strm"):
                continue
            p = os.path.join(dirpath, name)
            try:
                mtime = int(os.stat(p).st_mtime)
                with open(p, "rb") as f:
                    content = f.read(8192).decode("utf-8", "replace")
            except OSError:
                continue
            info = classify(Path(p), content, now)
            slug, vid = info.slug, None
            if info.kind == "cdn" and info.video_id in slug_by_id:
                vid = info.video_id
                slug = slug_by_id[vid]  # videoId 就是库里的主键，直接换出 slug
            elif slug:
                vid = id_by_slug.get(slug)
            key = os.path.normcase(p)
            lib_id = next((lid for r, lid in roots if key.startswith(r)), None)
            rows.append((p, scan_id, info.url, info.prefix, info.kind, slug, vid, int(info.expired),
                         int(key in managed), lib_id, mtime, "", int(now)))
    return rows


def replace_prefix_in_file(path: Path, old: str, new: str) -> tuple[str, str] | None:
    """把 strm 第一条 URL 的前缀 old 换成 new；内容已不是 old 开头（被别处改过）就跳过。"""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        s = line.strip().lstrip("﻿")
        if not s or s.startswith("#"):
            continue
        if not s.startswith(old):
            return None
        idx = line.index(old)
        lines[i] = line[:idx] + new + line[idx + len(old):]
        new_text = "".join(lines)
        write_atomic(path, new_text.encode("utf-8"))
        return text, new_text
    return None


def restore_file(path: Path, expected: str, original: str) -> bool:
    try:
        if path.read_bytes().decode("utf-8") != expected:
            return False
    except (OSError, UnicodeDecodeError):
        return False
    write_atomic(path, original.encode("utf-8"))
    return True


def _check_prefixes(old: str, new: str) -> tuple[str, str]:
    old, new = old.strip(), new.strip()
    if not old or not new:
        raise ValueError("旧前缀和新前缀都不能为空")
    if old == new:
        raise ValueError("新旧前缀相同")
    if any(c in old + new for c in "\r\n"):
        raise ValueError("前缀不能包含换行")
    return old, new


class StrmManager:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.db = engine.db

    # ---- 扫描 ----

    async def create_scan(self, dir: str = "") -> int:
        root = Path(dir).expanduser() if dir.strip() else self.e.store.output_dir
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"目录不存在：{root}")
        job_id = await self.db.create_job("scan", f"扫描 {root}", {"dir": str(root)})
        await self.db.add_tasks(job_id, "scan", ["all"], 20)
        self.e.notify()
        return job_id

    async def do_scan(self, job: dict, task: dict) -> None:
        root = Path(job["params"]["dir"])
        if not root.is_dir():
            raise ValueError(f"目录不存在：{root}")
        lib_roots = [(lid, self.e.writer.library_root(lib)) for lid, lib in self.e.libs.items()]
        managed = {os.path.normcase(p) for p in await self.db.all_output_paths()}
        keys = await self.db.video_keys()
        rows = await asyncio.to_thread(
            scan_directory, root, lib_roots, managed, dict(keys), {s: i for i, s in keys}, job["id"]
        )
        dir_prefix = str(root) + os.sep
        await self.db.replace_strm_files(dir_prefix, rows)
        summary = await self.db.strm_summary(job["id"])
        _, missing = await self.db.missing_outputs(dir_prefix, 0, 1)
        state = {"dir": str(root), "files": len(rows), "missing": missing, "adoptable": summary["adoptable"],
                 "kinds": {k: v["total"] for k, v in summary["kinds"].items()}}
        await self.db.update_job(job["id"], state=state)
        log.info("扫描 %s：%d 个 strm，%s，可纳管 %d，库里有记录但文件缺失 %d", root, len(rows),
                 "、".join(f"{k} {v}" for k, v in state["kinds"].items()) or "无", summary["adoptable"], missing)

    # ---- 纳管 ----

    async def create_adopt(self, scan_id: int, *, library_id: int | None = None, fetch_missing: bool = True,
                           kinds: tuple[str, ...] = ("ours", "cdn"), prefix: str = "") -> int:
        kinds = tuple(k for k in kinds if k in ADOPTABLE_KINDS)
        if not kinds:
            raise ValueError("至少选择一种可纳管的类型")
        if library_id:
            self.e._library(library_id)
        paths = await self.db.adopt_candidates(scan_id, kinds, prefix)
        if not paths:
            raise ValueError("没有符合条件的可纳管文件")
        target = self.e.libs[library_id]["name"] if library_id else "按所在目录"
        job_id = await self.db.create_job(
            "adopt", f"纳管 {len(paths)} 个 strm（{target}）",
            {"scan_id": scan_id, "library_id": library_id, "fetch_missing": fetch_missing, "kinds": list(kinds)},
        )
        await self.db.add_tasks(job_id, "adopt", paths, 1)
        self.e.notify()
        return job_id

    async def do_adopt(self, job: dict, task: dict) -> None:
        p = job["params"]
        path = task["target"]
        row = await self.db.get_strm_file(path)
        if row is None or row["managed"]:
            return
        lib_id = p.get("library_id") or row["library_id"]
        if not lib_id or lib_id not in self.e.libs:
            await self.db.update_strm_file(path, note="不在任何输出库目录下，需要指定目标库")
            return
        slug = row["slug"]
        v = await self.db.get_video(slug)
        if v is None:
            if not p.get("fetch_missing", True):
                await self.db.update_strm_file(path, note="库里没有这部影片（未勾选先抓详情）")
                return
            try:
                v = await self.e.fetch_detail(slug)
            except (NotFound, VideoGone):
                await self.db.update_strm_file(path, note=f"站点上不存在 {slug}")
                raise
        existing = await self.db.get_output(v["id"], lib_id)
        if (existing and existing["strm_path"] and Path(existing["strm_path"]).exists()
                and os.path.normcase(existing["strm_path"]) != os.path.normcase(path)):
            await self.db.update_strm_file(path, note=f"重复：该库已有 {existing['strm_path']}")
            return
        await self.db.ensure_output(v["id"], lib_id, via="adopt")
        await self.e._output_one(v, lib_id, cover=bool(v["detail_at"]), old_strm=path)
        out = await self.db.get_output(v["id"], lib_id)
        moved = os.path.normcase(out["strm_path"]) != os.path.normcase(path)
        url = self.e.writer.play_url(v)
        await self.db.update_strm_file(
            path, managed=1, kind="ours", url=url, prefix=url_prefix(url), slug=v["slug"], video_id=v["id"],
            library_id=lib_id, note=f"已纳管，移到 {out['strm_path']}" if moved else "已纳管",
        )

    # ---- 改前缀 / 回滚 ----

    async def preview_prefix(self, scan_id: int, old: str, new: str) -> dict:
        old, new = _check_prefixes(old, new)
        counts = await self.db.count_strm_prefix(scan_id, old)
        samples = await self.db.strm_prefix_matches(scan_id, old, limit=20)
        return {
            "count": counts["n"],
            "managed": counts["managed"],
            "updates_setting": self._is_public_base(old),
            "samples": [{"path": r["path"], "old": r["url"], "new": new + r["url"][len(old):]} for r in samples],
        }

    def _is_public_base(self, prefix: str) -> bool:
        return prefix.rstrip("/") == self.e.store.public_base_url.rstrip("/")

    async def create_prefix(self, scan_id: int, old: str, new: str) -> int:
        old, new = _check_prefixes(old, new)
        n = (await self.db.count_strm_prefix(scan_id, old))["n"]
        if not n:
            raise ValueError("没有以该前缀开头的 strm")
        job_id = await self.db.create_job("prefix", f"改前缀 {old} → {new}（{n} 个）",
                                          {"scan_id": scan_id, "old": old, "new": new})
        await self.db.add_tasks(job_id, "prefix", ["all"], 20)
        self.e.notify()
        return job_id

    async def do_prefix(self, job: dict, task: dict) -> None:
        p = job["params"]
        old, new = p["old"], p["new"]
        state = job["state"]
        changed = skipped = 0
        for r in await self.db.strm_prefix_matches(p["scan_id"], old):
            result = await asyncio.to_thread(replace_prefix_in_file, Path(r["path"]), old, new)
            if result is None:
                skipped += 1
                await self.db.update_strm_file(r["path"], note="改前缀时跳过：文件已不存在或内容已变")
                continue
            await self.db.add_strm_change(job["id"], r["path"], *result)
            new_url = new + r["url"][len(old):]
            await self.db.update_strm_file(r["path"], url=new_url, prefix=url_prefix(new_url))
            changed += 1
        state["changed"] = state.get("changed", 0) + changed
        state["skipped"] = state.get("skipped", 0) + skipped
        if self._is_public_base(old):
            await self.e.store.update({"public_base_url": new.rstrip("/")})
            state["setting_updated"] = {"from": old.rstrip("/"), "to": new.rstrip("/")}
            log.info("旧前缀就是对外地址，设置已同步改为 %s", new)
        await self.db.update_job(job["id"], state=state)
        log.info("改前缀 %s → %s：修改 %d 个，跳过 %d 个", old, new, changed, skipped)

    async def create_revert(self, change_set: int) -> int:
        changes = await self.db.strm_changes(change_set)
        if not changes:
            raise ValueError("这批改动没有可回滚的文件")
        job_id = await self.db.create_job("revert", f"回滚改前缀 #{change_set}（{len(changes)} 个）",
                                          {"change_set": change_set})
        await self.db.add_tasks(job_id, "revert", ["all"], 20)
        self.e.notify()
        return job_id

    async def do_revert(self, job: dict, task: dict) -> None:
        change_set = job["params"]["change_set"]
        restored = skipped = 0
        for c in await self.db.strm_changes(change_set):
            if await asyncio.to_thread(restore_file, Path(c["path"]), c["new"], c["old"]):
                await self.db.mark_change_reverted(c["id"])
                url = first_url_line(c["old"])
                await self.db.update_strm_file(c["path"], url=url, prefix=url_prefix(url))
                restored += 1
            else:
                skipped += 1
        source = await self.db.get_job(change_set)
        updated = (source or {}).get("state", {}).get("setting_updated")
        if updated and self.e.store.public_base_url.rstrip("/") == updated["to"]:
            await self.e.store.update({"public_base_url": updated["from"]})
            log.info("对外地址已改回 %s", updated["from"])
        await self.db.update_job(job["id"], state={"restored": restored, "skipped": skipped})
        log.info("回滚改前缀 #%d：恢复 %d 个，跳过 %d 个（内容已被改动）", change_set, restored, skipped)
