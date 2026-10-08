"""输出 strm / nfo / 封面（Emby、Jellyfin 通用命名：<名>.strm、<名>.nfo、<名>-poster.jpg、<名>-fanart.jpg）。"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlsplit

from PIL import Image

from .config import SettingsStore
from .fetcher import Fetcher, NotFound

log = logging.getLogger(__name__)

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
OUTPUT_SUFFIXES = (".strm", ".nfo", "-poster.jpg", "-fanart.jpg")
VERSION_PATH_RE = re.compile(r"/play/([a-z0-9][a-z0-9._-]{0,80})@([a-z0-9]+)\.m3u8$", re.I)  # 多画质版本的播放地址
VERSION_STYLES = ("emby", "suffix")  # 多画质版本文件的命名方式，见 OutputWriter.version_path


def sanitize(value: str, max_len: int = 100) -> str:
    v = _INVALID.sub(" ", value)
    v = re.sub(r"\s+", " ", v).strip().strip(".").strip()
    return v[:max_len].strip() or "_"


def write_atomic(path: Path, data: bytes) -> bool:
    """内容不变就跳过；否则写临时文件再替换。返回是否真的写了。"""
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def build_nfo(v: dict) -> str:
    root = ET.Element("movie")

    def add(tag: str, text, **attrs) -> None:
        el = ET.SubElement(root, tag, attrs)
        el.text = str(text)

    code = v.get("code") or v["slug"].upper()
    add("title", v.get("title") or code)
    add("originaltitle", v.get("title") or code)
    add("sorttitle", code)
    add("num", code)
    if v.get("title"):
        add("plot", v["title"])
    if rd := v.get("release_date"):
        add("premiered", rd)
        add("releasedate", rd)
        add("year", rd[:4])
    if v.get("duration"):
        add("runtime", round(v["duration"] / 60))
    for c in v.get("categories") or []:
        add("genre", c["name"])
    for t in v.get("tags") or []:
        add("tag", t["name"])
    if v.get("quality"):
        add("tag", v["quality"])
    if v.get("uncensored"):
        add("tag", "无码流出")
    if v.get("maker"):
        add("studio", v["maker"])
    if v.get("director"):
        add("director", v["director"])
    if v.get("series"):
        el = ET.SubElement(root, "set")
        ET.SubElement(el, "name").text = v["series"]
    for m in v.get("models") or []:
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = m["name"]
        ET.SubElement(actor, "type").text = "Actor"
    add("uniqueid", code, type="num", default="true")
    ET.indent(root)
    return '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def crop_poster(data: bytes) -> bytes:
    """DVD 封套（正反面拼接，约 1.49:1）取右侧正面；其他比例原样返回。"""
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    if w / h < 1.3:
        return data
    left = max(0, w - round(h * 0.7))
    buf = io.BytesIO()
    im.crop((left, 0, w, h)).convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


class OutputWriter:
    def __init__(self, store: SettingsStore) -> None:
        self.store = store

    def play_url(self, v: dict, version: str = "") -> str:
        """strm 里的播放地址；version 是多画质版本的档位（'720p'），写成 /play/{slug}@720p.m3u8。"""
        s = self.store.current
        if s.play_mode == "direct" and v.get("stream_url") and not version:
            return v["stream_url"]
        url = f"{self.store.public_base_url}/play/{v['slug']}{'@' + version.lower() if version else ''}.m3u8"
        if s.play_token:
            url += f"?t={quote(s.play_token)}"
        return url

    # ---- 多画质版本 ----

    @staticmethod
    def version_path(strm: Path, label: str, style: str) -> Path | None:
        """多画质版本文件的位置，和主 strm 同目录：
        emby    「目录名 - 720p.strm」（Emby 官方多版本命名）；目录名得是主 strm 文件名的开头（一部片一个目录），
                否则不写（合集目录里写了会串到别的片上）
        suffix  「主文件名-720p.strm」（和 mdcng 的 -4K 后缀一个写法）
        """
        if style == "emby":
            folder = strm.parent.name
            if not folder or not strm.stem.lower().startswith(folder.lower()):
                return None
            return strm.parent / f"{folder} - {label}.strm"
        if style == "suffix":
            return strm.with_name(f"{strm.stem}-{label}.strm")
        return None

    @staticmethod
    def own_versions(strm: Path, slug: str) -> dict[Path, str]:
        """主 strm 旁边本程序写的版本文件 {路径: 档位}：按内容认（/play/{slug}@档位.m3u8），两种命名都认得，
        换了命名方式、关掉多画质版本时能清干净，别人的文件不碰。"""
        out = {}
        folder, stem = strm.parent.name, strm.stem
        try:
            entries = list(os.scandir(strm.parent))
        except OSError:
            return out
        for e in entries:
            name = e.name
            if (not name.lower().endswith(".strm") or name == strm.name
                    or not (name.startswith(f"{folder} - ") or name.startswith(f"{stem}-"))):
                continue
            try:
                with open(e.path, "rb") as f:
                    first = f.read(1024).decode("utf-8", "replace").strip().splitlines()[:1]
            except OSError:
                continue
            m = VERSION_PATH_RE.search(urlsplit(first[0]).path) if first else None
            if m and m.group(1).lower() == slug:
                out[Path(e.path)] = m.group(2).lower()
        return out

    def sync_versions(self, v: dict, strm: Path, labels: list[str], style: str) -> tuple[int, int]:
        """把主 strm 旁边的多画质版本文件对齐到 labels（style 为空或 labels 为空就是全删）：缺的写、地址变了的改、
        不要的删（只删本程序写的）。返回（写了几个, 删了几个）。同步函数，调用方放到线程里跑。"""
        want = {p: label for label in labels if (p := self.version_path(strm, label, style)) is not None}
        removed = 0
        for p in self.own_versions(strm, v["slug"]):
            if p not in want:
                p.unlink(missing_ok=True)
                removed += 1
        wrote = sum(write_atomic(p, (self.play_url(v, label) + "\n").encode()) for p, label in want.items())
        return wrote, removed

    def library_root(self, lib: dict) -> Path:
        """输出库目录：相对路径挂在输出根目录下，绝对路径原样使用。"""
        d = Path(lib["dir"])
        return d if d.is_absolute() else self.store.output_dir / d

    def external_root(self, lib: dict) -> Path | None:
        """外部整理目录（mdcng 等把这个库的 strm 整理到哪里），没设置返回 None；路径规则同库目录。"""
        return self.library_root({"dir": lib["external_dir"]}) if lib.get("external_dir") else None

    def base_path(self, v: dict, lib: dict) -> Path:
        """不含扩展名的输出路径。"""
        models = v.get("models") or []
        values = {
            "slug": v["slug"].upper(),
            "code": v.get("code") or v["slug"].upper(),
            "actor": models[0]["name"] if models else "未知演员",
            "year": (v.get("release_date") or "")[:4] or "未知年份",
        }
        template = lib.get("path_template") or self.store.current.path_template
        rel = template.format(**{k: sanitize(x) for k, x in values.items()})
        parts = [sanitize(p) for p in rel.split("/") if p.strip()]
        root = self.library_root(lib)
        path = root.joinpath(*parts)
        if root not in path.parents:
            raise ValueError(f"输出路径越界：{path}")
        return path

    def write(self, v: dict, lib: dict, old_strm: str = "", keep_dirs: frozenset[Path] = frozenset()) -> Path | None:
        """写 strm（有详情时一并写 nfo），返回 strm 路径。同步函数，调用方放到线程里跑。

        old_strm 和新位置不同（模板或库目录改了）时，把 nfo、封面搬到新位置，再清掉旧文件和空目录；
        keep_dirs 里的目录（各输出库根目录）不会被删。

        外部整理库只在第一次按模板写进库目录，之后文件归外部工具移动、改名、刮削：这里只原地更新 strm 内容，
        不搬文件、不写 nfo；记录的文件已经不在时返回 None，等「同步位置」找回新位置。
        """
        url = (self.play_url(v) + "\n").encode()
        if lib.get("external_dir") and old_strm:
            if not Path(old_strm).is_file():
                return None
            write_atomic(Path(old_strm), url)
            return Path(old_strm)
        base = self.base_path(v, lib)
        base.parent.mkdir(parents=True, exist_ok=True)
        strm = base.with_name(base.name + ".strm")
        if old_strm and Path(old_strm) != strm:
            self._relocate(Path(old_strm), base, keep_dirs, v["slug"])
        write_atomic(strm, url)
        if self.store.current.write_nfo and v.get("detail_at") and not lib.get("external_dir"):
            write_atomic(base.with_name(base.name + ".nfo"), build_nfo(v).encode())
        return strm

    def _relocate(self, old_strm: Path, new_base: Path, keep_dirs: frozenset[Path], slug: str) -> None:
        """搬到新位置：nfo、封面搬过去；旧的 strm 和多画质版本删掉（版本由调用方在新位置重新对齐）。"""
        for p in self.own_versions(old_strm, slug):
            p.unlink(missing_ok=True)
        old_base = old_strm.with_name(old_strm.name.removesuffix(".strm"))
        for suffix in OUTPUT_SUFFIXES:
            src = old_base.with_name(old_base.name + suffix)
            if not src.exists():
                continue
            dst = new_base.with_name(new_base.name + suffix)
            if suffix == ".strm" or dst.exists():
                src.unlink()
            else:
                shutil.move(src, dst)
        self.remove_empty_dirs(old_strm.parent, keep_dirs)

    def remove(self, strm: Path, keep_dirs: frozenset[Path] = frozenset(), strm_only: bool = False,
               slug: str = "") -> None:
        """删除一部影片在某个库里的全部输出文件（只删本程序生成的文件），多画质版本一起删（给了 slug 时）。

        strm_only：外部整理库只删 strm，旁边的 nfo、图片是外部工具生成的，不动。
        """
        if slug:
            for p in self.own_versions(strm, slug):
                p.unlink(missing_ok=True)
        base = strm.with_name(strm.name.removesuffix(".strm"))
        for suffix in (".strm",) if strm_only else OUTPUT_SUFFIXES:
            base.with_name(base.name + suffix).unlink(missing_ok=True)
        self.remove_empty_dirs(strm.parent, keep_dirs)

    def remove_empty_dirs(self, start: Path, keep_dirs: frozenset[Path]) -> None:
        """向上清理空目录：不越过输出根目录和各库根目录；输出根目录之外最多清一层。"""
        out_root = self.store.output_dir
        d = start
        for level in range(4):
            if d in keep_dirs or d == out_root or (level and out_root not in d.parents):
                return
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent

    async def write_cover(self, fetcher: Fetcher, v: dict, strm: Path, siblings: list[Path] = ()) -> bool:
        """准备封面，返回是否已具备封面。网络失败抛 FetchError 由任务重试。

        siblings 是同一部影片在其他库里的 strm；那边已有封面就硬链接过来（跨文件系统时复制），不再下载。
        """
        s = self.store.current
        if not s.download_cover or not v.get("cover_url"):
            return False
        names = ["-fanart.jpg"] + (["-poster.jpg"] if s.poster_crop else [])
        targets = [cover_path(strm, n) for n in names]
        if all(t.exists() for t in targets):
            return True
        for other in siblings:
            sources = [cover_path(other, n) for n in names]
            if all(p.exists() for p in sources):
                await asyncio.to_thread(lambda: [link_or_copy(a, b) for a, b in zip(sources, targets)])
                return True
        try:
            data = await fetcher.get_bytes(v["cover_url"])
        except NotFound:
            log.warning("%s 封面不存在：%s", v["slug"], v["cover_url"])
            return False

        def save() -> None:
            write_atomic(targets[0], data)
            if s.poster_crop:
                write_atomic(targets[1], crop_poster(data))

        await asyncio.to_thread(save)
        return True


def cover_path(strm: Path, suffix: str) -> Path:
    return strm.with_name(strm.name.removesuffix(".strm") + suffix)


def link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
