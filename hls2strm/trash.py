"""外部整理库的回收区与残留清理。

影片离开外部整理库（归并、规则、手动移出）时，外部刮削器（mdcng 等）在整理目录里给它生成的 nfo、图片、
字幕就没用了：删掉 strm 后，如果它所在的目录只属于这部片（一片一目录，剩下的没有影片文件），整个移进回收区。
回收区在整理目录下的 .hls2strm-trash/移入日期/，保留原来的相对路径；和整理目录在同一个文件系统，只改名不复制；
里面没有 strm，媒体库不会当成影片。放满保留天数后自动删除，保留天数为 0 时不进回收区、直接删。

残留清理：找出整理目录里已经没有影片文件、只剩 nfo 和图片的影片目录（以前移出库时留下的），同样移进回收区。
都是同步函数，调用方放到线程里跑。
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
import time
from pathlib import Path

TRASH_DIR = ".hls2strm-trash"
ORPHAN_MIN_AGE = 86400  # 一天内有改动的目录不算残留：外部工具可能正整理到一半（先写 nfo、图片，再挪 strm）
MEDIA_SUFFIXES = frozenset((
    ".strm", ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".flv", ".webm",
    ".rmvb", ".rm", ".mpg", ".mpeg", ".vob", ".iso", ".3gp",
))


def is_media(path: str) -> bool:
    """影片文件（strm 或视频）。预告片（-trailer、trailers/ 下的）不算：刮削器顺手下的，跟着目录走。"""
    p = Path(path)
    if p.suffix.lower() not in MEDIA_SUFFIXES:
        return False
    stem = p.stem.lower()
    return not (stem == "trailer" or stem.endswith("-trailer") or p.parent.name.lower() == "trailers")


def _is_link(st: os.stat_result) -> bool:
    """软链接，以及 Windows 的目录联接等重解析点。"""
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def inside(path: Path, ext: Path) -> bool:
    """path 在整理目录下面（不含整理目录本身），也不在回收区里。"""
    try:
        rel = path.relative_to(ext)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] != TRASH_DIR


def exclusive(d: Path) -> bool:
    """目录里只剩元数据：没有影片文件、软链接和特殊文件，可以整个拿走。读不了也当作不行。"""
    try:
        for dirpath, dirs, files in os.walk(d, onerror=_raise):
            for name in dirs + files:
                p = os.path.join(dirpath, name)
                st = os.lstat(p)
                if _is_link(st) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    return False
                if stat.S_ISREG(st.st_mode) and is_media(p):
                    return False
    except OSError:
        return False
    return True


def _raise(e: OSError) -> None:
    raise e


def retire(d: Path, ext: Path, days: int, today: dt.date | None = None) -> Path | None:
    """把整理目录里只剩元数据的影片目录 d 移进回收区（days 为 0 直接删），再删掉因此空出来的上级目录。
    返回移到了哪里（直接删时返回 d）；d 不在整理目录下、已经不在了、里面还有影片文件，就不动，返回 None。"""
    try:
        if not inside(d, ext) or _is_link(os.lstat(d)) or not d.is_dir():
            return None
    except OSError:
        return None
    if not exclusive(d):
        return None
    if days <= 0:
        shutil.rmtree(d)
        target = d
    else:
        target = ext / TRASH_DIR / (today or dt.date.today()).isoformat() / d.relative_to(ext)
        n = 1
        while os.path.lexists(target):  # 同一天又移进来一份同名的
            n += 1
            target = target.with_name(f"{d.name} ({n})")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(d, target)
    prune(d.parent, ext)
    return target


def prune(start: Path, ext: Path) -> None:
    """从 start 往上删空目录，到整理目录为止（不删整理目录本身）。"""
    d = start
    while inside(d, ext):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def purge(ext: Path, days: int, today: dt.date | None = None) -> int:
    """删掉回收区里放满 days 天的（按移入日期，保证至少放了 days 整天），返回删了几天的。
    回收区里不是日期命名的目录不碰。"""
    root = ext / TRASH_DIR
    today = today or dt.date.today()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    n = 0
    for e in entries:
        try:
            day = dt.date.fromisoformat(e.name)
        except ValueError:
            continue
        if (today - day).days > days and e.is_dir(follow_symlinks=False):
            shutil.rmtree(e.path)
            n += 1
    try:
        root.rmdir()
    except OSError:
        pass
    return n


def find_orphans(ext: Path, min_age: float = ORPHAN_MIN_AGE) -> list[tuple[Path, int]]:
    """整理目录里已经没有影片文件、只剩 nfo 和图片的影片目录（直接含 nfo 的最上层目录），返回 [(目录, 字节数)]。

    最近 min_age 秒内有改动的不算；含软链接、特殊文件、读不了的子目录的当作在用。整理目录本身和回收区不算。
    """
    top, trash = str(ext), str(ext / TRASH_DIR)
    cutoff = time.time() - min_age
    seen: dict[str, tuple[bool, float, int]] = {}  # 已看过的子目录 -> (在用, 最新修改时间, 字节数)
    found: list[tuple[str, int]] = []
    for dirpath, dirs, files in os.walk(top, topdown=False):
        if dirpath == trash or dirpath.startswith(trash + os.sep):
            continue
        try:
            newest = os.lstat(dirpath).st_mtime
        except OSError:
            continue  # 上级会因为没看到它而当作在用
        busy, size, nfo = False, 0, False
        for name in files:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                busy = True
                continue
            if _is_link(st) or not stat.S_ISREG(st.st_mode) or is_media(p):
                busy = True
            nfo = nfo or name.lower().endswith(".nfo")
            newest, size = max(newest, st.st_mtime), size + st.st_size
        for name in dirs:
            child = os.path.join(dirpath, name)
            if child == trash:
                continue
            if child not in seen:  # 软链接目录、读不了的目录
                busy = True
                continue
            b, t, s = seen.pop(child)
            busy, newest, size = busy or b, max(newest, t), size + s
        seen[dirpath] = (busy, newest, size)
        if nfo and not busy and newest < cutoff and dirpath != top:
            found.append((dirpath, size))
    paths = {p for p, _ in found}
    # 下层目录也直接含 nfo 时（比如带 nfo 的花絮目录），跟着最上层的一起走
    return sorted((Path(p), s) for p, s in found if not any(str(a) in paths for a in Path(p).parents))
