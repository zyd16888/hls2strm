"""已有 strm 的扫描、纳管、批量改前缀与回滚，以及外部整理库的「同步位置」。

扫描把目录下每个 .strm 分类：
  ours     本服务格式：…/play/{slug}.m3u8（域名不限）
  version  本服务写的多画质版本：…/play/{slug}@720p.m3u8，跟着主 strm 走，不纳管、不当成影片的主文件
  cdn      CDN 直链：…/hls/{token}/{expires}/{n}/{videoId}/{videoId}.m3u8
           或 …&expires={expires}&…/vod/{n}/{videoId}/{videoId}.m3u8（大概率已过期）
  named    URL 不认识，但文件名或父目录里有番号（可能是别的片源，纳管需手动勾选）
  other    其他来源（115、alist 等），只参与统计和改前缀
  invalid  空文件

外部整理库（设置了外部整理目录）的 strm 会被 mdcng 等工具移走、改名；「同步位置」在库目录和外部整理目录里
按 strm 内容找回每部影片的文件，更新记录的路径，之后重写、改地址都在新位置原地进行。「清理残留」把外部整理目录里
已经没有 strm、只剩 nfo 和图片的影片目录移进回收区（见 trash.py）。
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
from .parser import VideoGone, hls_expires
from .trash import find_orphans, retire
from .writer import VERSION_PATH_RE, write_atomic

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)
INDEX_TTL = 600  # 按内容找文件的索引缓存多久（秒）：订阅连续翻多页时不用每页都扫一遍目录
RECHECK_AGE = 60  # 缓存里的位置不对时重扫；一分钟内刚扫过就不再扫（归并一次移出很多部时不用每部扫一遍）
TIDY_LOG_LIMIT = 200  # 清理残留时日志里最多列出多少个目录

OURS_RE = re.compile(r"/play/([a-z0-9][a-z0-9._-]{0,80})\.m3u8$", re.I)
CDN_RE = re.compile(r"/(?:hls/[^/]+/\d{9,11}/\d+|vod/\d+)/(\d+)/\1\.m3u8$")
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
        if m := VERSION_PATH_RE.search(parts.path):
            return StrmInfo("version", url, prefix, m.group(1).lower())
        if m := OURS_RE.search(parts.path):
            return StrmInfo("ours", url, prefix, m.group(1).lower())
        if m := CDN_RE.search(parts.path):
            return StrmInfo("cdn", url, prefix, guess_slug(path), int(m.group(1)), (hls_expires(url) or 0) < now)
    slug = guess_slug(path)
    return StrmInfo("named" if slug else "other", url, prefix, slug)


def iter_strm(root: Path, now: float):
    """遍历目录下的 .strm，产出 (路径, 修改时间, 分类结果)。"""
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if not name.lower().endswith(".strm"):
                continue
            p = os.path.join(dirpath, name)
            try:
                mtime = int(os.stat(p).st_mtime)
                with open(p, "rb") as f:
                    content = f.read(8192).decode("utf-8", "replace")
            except OSError:
                continue
            yield p, mtime, classify(Path(p), content, now)


def video_of(info: StrmInfo, slug_by_id: dict[int, str], id_by_slug: dict[str, int]) -> tuple[str, int | None]:
    """strm 对应库里的哪部影片，返回 (slug, 作品 id)；库里没有时 id 为 None。

    slug_by_id 是 Jable 的 videoId -> 作品 slug（CDN 直链里只有 videoId）。
    """
    slug = slug_by_id.get(info.video_id, info.slug) if info.kind == "cdn" else info.slug
    return slug, (id_by_slug.get(slug) if slug else None)


def scan_directory(
    root: Path,
    library_roots: list[tuple[int, Path]],
    managed: set[str],
    slug_by_id: dict[int, str],
    id_by_slug: dict[str, int],
    scan_id: int,
) -> list[tuple]:
    return list(iter_scan_rows(root, library_roots, managed, slug_by_id, id_by_slug, scan_id))


def iter_scan_rows(root, library_roots, managed, slug_by_id, id_by_slug, scan_id, after=""):
    """遍历目录，返回 strm_files 的行。同步函数，放到线程里跑。"""
    now = time.time()
    roots = sorted(((os.path.normcase(str(r)) + os.sep, lid) for lid, r in library_roots), key=lambda x: -len(x[0]))
    cursor = (Path(after).parent.parts, Path(after).name) if after else None
    for p, mtime, info in iter_strm(root, now):
        if cursor and (Path(p).parent.parts, Path(p).name) <= cursor:
            continue
        slug, vid = video_of(info, slug_by_id, id_by_slug)
        key = os.path.normcase(p)
        lib_id = next((lid for r, lid in roots if key.startswith(r)), None)
        yield (p, scan_id, info.url, info.prefix, info.kind, slug, vid, int(info.expired),
               int(key in managed), lib_id, mtime, "", int(now))


def find_by_video(roots: list[Path], slug_by_id: dict[int, str],
                  id_by_slug: dict[str, int]) -> tuple[dict[int, list[str]], list[int]]:
    """按 strm 内容（本服务地址或 CDN 直链）找出每部影片的文件，不看文件名，所以外部工具改名也认得出。
    返回 (每部影片的文件, 每个目录里认得出的 strm 数)。

    跳过软链接：外部工具用软链接模式时，真正的文件还在库目录里，记录它就行；但软链接算进数量（目录在用）。
    同步函数，放到线程里跑。
    """
    now = time.time()
    found: dict[int, list[str]] = {}
    counts = []
    for root in roots:
        n = 0
        for p, _, info in iter_strm(root, now) if root.is_dir() else ():
            if info.kind not in ("ours", "cdn"):
                continue
            _, vid = video_of(info, slug_by_id, id_by_slug)
            if vid is None:
                continue
            n += 1
            if not os.path.islink(p):
                found.setdefault(vid, []).append(p)
        counts.append(n)
    return {vid: sorted(paths) for vid, paths in found.items()}, counts


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


def _fmt_bytes(n: int) -> str:
    """'512 KB' / '3.2 MB' / '1.5 GB'。"""
    if n < 1024 ** 2:
        return f"{max(1, round(n / 1024))} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.1f} GB"


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
        self._index: dict[int, tuple[float, dict[int, list[str]], int]] = {}  # 库 id -> (时间, 文件, 外部整理目录 strm 数)

    # ---- 按内容找文件 ----

    async def index(self, lib: dict, max_age: float = INDEX_TTL) -> dict[int, list[str]]:
        """这个库每部影片的 strm 在哪：在库目录（收件目录）和外部整理目录里按内容找，不看文件名，
        所以外部刮削器改了目录、给文件名加了 -C / -破解 / -4K 之类的后缀也认得出。结果缓存一会儿。"""
        cached = self._index.get(lib["id"])
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        keys = await self.db.video_keys()
        ext = self.e.writer.external_root(lib)
        roots = [self.e.writer.library_root(lib)] + ([ext] if ext else [])
        found, counts = await asyncio.to_thread(find_by_video, roots, await self.db.cdn_video_map(),
                                                {s: i for i, s in keys})
        self._index[lib["id"]] = (time.time(), found, counts[1] if ext else 0)
        return found

    def pick(self, lib: dict, paths: list[str]) -> str:
        """同一部影片找到多个文件时优先外部整理目录里的（整理好的那份才是媒体库在用的）。"""
        ext = self.e.writer.external_root(lib)
        if ext is not None:
            prefix = os.path.normcase(str(ext)) + os.sep
            in_ext = [p for p in paths if os.path.normcase(p).startswith(prefix)]
            if in_ext:
                return in_ext[0]
        return paths[0]

    def external_problem(self, lib: dict) -> str:
        """外部整理目录能不能用：不存在返回 absent，里面一个认得出的 strm 都没有返回 empty，能用返回空串。
        前两种多半是挂载出了问题，不能往收件目录补（挂载恢复后会整库重刮）。

        只数外部整理目录（上次 index 时的结果）：挂载掉了、挂载点剩个空目录时，收件目录里订阅刚写的几个新 strm 不能算数。
        """
        ext = self.e.writer.external_root(lib)
        if ext is None or not ext.is_dir():
            return "absent"
        cached = self._index.get(lib["id"])
        return "" if cached and cached[2] else "empty"

    def remember(self, lib: dict, video_id: int, path: str) -> None:
        if (cached := self._index.get(lib["id"])) is not None:
            cached[1].setdefault(video_id, []).append(path)

    def forget(self, lib: dict, video_id: int) -> None:
        if (cached := self._index.get(lib["id"])) is not None:
            cached[1].pop(video_id, None)

    async def paths_of(self, lib: dict, video_id: int, recorded: str = "") -> list[str]:
        """这部片在这个库里现在的 strm：按内容在收件目录和外部整理目录里找（外部工具在上次同步位置之后又挪过、
        改过名也认得出），记录的路径还在也算上。"""
        def existing(paths: list[str]) -> list[str]:
            return [p for p in paths if os.path.isfile(p)]

        found = await self.index(lib)
        paths = await asyncio.to_thread(existing, found.get(video_id, []))
        if not paths and (recorded or video_id in found):  # 缓存的位置又被挪了，或者缓存时还没整理：重新扫一遍
            paths = await asyncio.to_thread(existing, (await self.index(lib, max_age=RECHECK_AGE)).get(video_id, []))
        if recorded and recorded not in paths and await asyncio.to_thread(os.path.isfile, recorded):
            paths.append(recorded)
        return paths

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
        lib_roots += [(lid, r) for lid, lib in self.e.libs.items() if (r := self.e.writer.external_root(lib))]
        managed = {os.path.normcase(p) for p in await self.db.all_output_paths()}
        keys = await self.db.video_keys()
        iterator = iter_scan_rows(root, lib_roots, managed, await self.db.cdn_video_map(), {s: i for i, s in keys}, job["id"], job["state"].get("scan_cursor", ""))
        from itertools import islice
        while True:
            await self.e.checkpoint(job["id"])
            rows = await asyncio.to_thread(lambda: list(islice(iterator, 128)))
            if not rows:
                break
            await self.db._write_many("INSERT OR REPLACE INTO scan_staging VALUES(" + ",".join("?" * 13) + ")", rows)
            await self.db.update_job(job["id"], state={"scan_cursor": rows[-1][0]})
        dir_prefix = str(root) + os.sep
        async with self.db._tx():
            await self.db.conn.execute("DELETE FROM strm_files WHERE path>=? AND path<?", (dir_prefix, dir_prefix + "\U0010ffff"))
            await self.db.conn.execute("INSERT OR REPLACE INTO strm_files SELECT * FROM scan_staging WHERE scan_id=?", (job["id"],))
            await self.db.conn.execute("DELETE FROM scan_staging WHERE scan_id=?", (job["id"],))
        summary = await self.db.strm_summary(job["id"])
        _, missing = await self.db.missing_outputs(dir_prefix, 0, 1)
        nfiles = sum(v["total"] for v in summary["kinds"].values())
        state = {"dir": str(root), "files": nfiles, "missing": missing, "adoptable": summary["adoptable"],
                 "kinds": {k: v["total"] for k, v in summary["kinds"].items()}}
        await self.db.update_job(job["id"], state=state)
        log.info("扫描 %s：%d 个 strm，%s，可纳管 %d，库里有记录但文件缺失 %d", root, nfiles,
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
        if not Path(path).is_file():
            await self.db.update_strm_file(path, note="文件已不存在")
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
                v = await self.e.fetch_detail("jable", slug)
            except (NotFound, VideoGone):
                await self.db.update_strm_file(path, note=f"站点上不存在 {slug}")
                raise
        if await self.db.in_libraries(v["id"], self.e.libs[lib_id]["excludes"]):
            await self.db.update_strm_file(path, note="影片已在这个库的排除库里，本库不收")
            return
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

    # ---- 外部整理库：同步位置 ----

    async def create_locate(self, library_id: int | None = None) -> int:
        """library_id 为空时同步所有外部整理库。"""
        libs = [self.e._library(library_id)] if library_id else list(self.e.libs.values())
        libs = [lib for lib in libs if lib["external_dir"]]
        if not libs:
            raise ValueError("没有设置外部整理目录的输出库")
        job_id = await self.db.create_job("locate", "同步位置：" + "、".join(lib["name"] for lib in libs),
                                          {"library_id": library_id})
        await self.db.add_tasks(job_id, "locate", [lib["id"] for lib in libs], 20)
        self.e.notify()
        return job_id

    async def do_locate(self, job: dict, task: dict) -> None:
        lib = self.e.libs.get(int(task["target"]))
        if lib is None or not lib["external_dir"]:
            return
        result = await self.locate(lib)
        state = (await self.db.get_job(job["id"]))["state"]
        for k, n in result.items():
            state[k] = state.get(k, 0) + n
        await self.db.update_job(job["id"], state=state)

    async def locate(self, lib: dict) -> dict:
        """在库目录和外部整理目录里找回这个库每部影片的 strm，更新记录的路径；不移动、不改任何文件。

        同一部影片找到多个文件时优先外部整理目录里的（整理好的那份才是媒体库在用的）。
        """
        ext = self.e.writer.external_root(lib)
        found = await self.index(lib, max_age=0)
        ext_prefix = os.path.normcase(str(ext)) + os.sep
        in_ext = {vid: [p for p in paths if os.path.normcase(p).startswith(ext_prefix)] for vid, paths in found.items()}
        updates: list[tuple[str, int]] = []
        seen: set[int] = set()
        result = {"checked": 0, "updated": 0, "missing": 0, "duplicates": 0, "extra": 0}
        async for v, out in self.db.iter_outputs(lib["id"]):
            seen.add(v["id"])
            result["checked"] += 1
            cur, ext_paths, paths = out["strm_path"], in_ext.get(v["id"], []), found.get(v["id"], [])
            result["duplicates"] += len(ext_paths) > 1
            if cur and (os.path.normcase(cur) in {os.path.normcase(p) for p in ext_paths}
                        or (not ext_paths and os.path.isfile(cur))):
                continue
            if ext_paths or paths:
                updates.append(((ext_paths or paths)[0], v["id"]))
            elif cur:
                result["missing"] += 1
        await self.db.set_output_paths(lib["id"], updates)
        result["updated"] = len(updates)
        if lib["versions"]:  # 外部工具刚整理好的，在整理后的位置旁边补上多画质版本
            for path, vid in updates:
                if (v := await self.db.get_video_by_id(vid)) is not None:
                    await self.e.sync_versions(v, lib, path)
        result["extra"] = sum(1 for vid, paths in in_ext.items() if paths and vid not in seen)
        log.info("同步位置「%s」：检查 %d 部，更新路径 %d，找不到 %d，外部整理目录里重复 %d，不在本库 %d",
                 lib["name"], result["checked"], result["updated"], result["missing"], result["duplicates"],
                 result["extra"])
        return result

    # ---- 外部整理库：清理残留 ----

    async def create_tidy(self, library_id: int | None = None, *, apply: bool = True) -> int:
        """library_id 为空时清理所有外部整理库；apply 为 False 只列出残留目录，不动文件。"""
        libs = [self.e._library(library_id)] if library_id else list(self.e.libs.values())
        libs = [lib for lib in libs if lib["external_dir"]]
        if not libs:
            raise ValueError("没有设置外部整理目录的输出库")
        name = "清理残留：" + "、".join(lib["name"] for lib in libs) + ("" if apply else "（只检查）")
        job_id = await self.db.create_job("tidy", name, {"library_id": library_id, "apply": apply})
        await self.db.add_tasks(job_id, "tidy", [lib["id"] for lib in libs], 20)
        self.e.notify()
        return job_id

    async def do_tidy(self, job: dict, task: dict) -> None:
        """外部整理目录里已经没有 strm、只剩 nfo 和图片的影片目录移进回收区（以前移出库时留下的）。"""
        lib = self.e.libs.get(int(task["target"]))
        if lib is None or not lib["external_dir"]:
            return
        ext = self.e.writer.external_root(lib)
        if not await asyncio.to_thread(ext.is_dir):
            log.warning("清理残留「%s」：外部整理目录 %s 不存在，跳过（检查挂载）", lib["name"], ext)
            return
        apply, days = job["params"].get("apply", True), self.e.store.current.trash_days
        orphans = await asyncio.to_thread(find_orphans, ext)
        moved = failed = 0
        for i, (d, size) in enumerate(orphans):
            await self.e.checkpoint(job["id"])
            if i < TIDY_LOG_LIMIT:
                log.info("残留目录：%s（%s）", d, _fmt_bytes(size))
            if not apply:
                continue
            try:
                moved += await asyncio.to_thread(retire, d, ext, days) is not None
            except OSError as e:
                failed += 1
                log.warning("残留目录 %s 没能移走：%s", d, e)
        if len(orphans) > TIDY_LOG_LIMIT:
            log.info("…另外还有 %d 个残留目录没列出", len(orphans) - TIDY_LOG_LIMIT)
        state = (await self.db.get_job(job["id"]))["state"]
        total = sum(size for _, size in orphans)
        for k, n in {"orphans": len(orphans), "bytes": total, "moved": moved, "failed": failed}.items():
            state[k] = state.get(k, 0) + n
        await self.db.update_job(job["id"], state=state)
        log.info("清理残留「%s」：找到 %d 个只剩 nfo、图片的目录，共 %s%s", lib["name"], len(orphans), _fmt_bytes(total),
                 (f"，{'直接删掉' if days <= 0 else '移进回收区'} {moved} 个" + (f"，失败 {failed} 个" if failed else ""))
                 if apply else "（只检查，没动文件）")

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
