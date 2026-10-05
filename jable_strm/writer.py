"""输出 strm / nfo / 封面（Emby、Jellyfin 通用命名：<名>.strm、<名>.nfo、<名>-poster.jpg、<名>-fanart.jpg）。"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from .config import SettingsStore
from .fetcher import Fetcher, NotFound

log = logging.getLogger(__name__)

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
OUTPUT_SUFFIXES = (".strm", ".nfo", "-poster.jpg", "-fanart.jpg")


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
    for m in v.get("models") or []:
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = m["name"]
        ET.SubElement(actor, "type").text = "Actor"
    add("uniqueid", v["id"], type="jable", default="true")
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

    def play_url(self, v: dict) -> str:
        s = self.store.current
        if s.play_mode == "direct" and v.get("hls_url"):
            return v["hls_url"]
        url = f"{self.store.public_base_url}/play/{v['slug']}.m3u8"
        if s.play_token:
            url += f"?t={quote(s.play_token)}"
        return url

    def base_path(self, v: dict) -> Path:
        """不含扩展名的输出路径。"""
        models = v.get("models") or []
        values = {
            "slug": v["slug"].upper(),
            "code": v.get("code") or v["slug"].upper(),
            "actor": models[0]["name"] if models else "未知演员",
            "year": (v.get("release_date") or "")[:4] or "未知年份",
        }
        rel = self.store.current.path_template.format(**{k: sanitize(x) for k, x in values.items()})
        parts = [sanitize(p) for p in rel.split("/") if p.strip()]
        root = self.store.output_dir
        path = root.joinpath(*parts)
        if root not in path.parents:
            raise ValueError(f"输出路径越界：{path}")
        return path

    def write(self, v: dict) -> Path:
        """写 strm（有详情时一并写 nfo），返回 strm 路径。同步函数，调用方放到线程里跑。"""
        base = self.base_path(v)
        base.parent.mkdir(parents=True, exist_ok=True)
        strm = base.with_name(base.name + ".strm")
        write_atomic(strm, (self.play_url(v) + "\n").encode())
        if self.store.current.write_nfo and v.get("detail_at"):
            write_atomic(base.with_name(base.name + ".nfo"), build_nfo(v).encode())
        old = v.get("strm_path")
        if old and Path(old) != strm:
            self._remove_old(Path(old))
        return strm

    def _remove_old(self, old_strm: Path) -> None:
        """路径模板变化后清理旧位置的输出文件（只删本程序生成的文件）。"""
        stem = old_strm.with_name(old_strm.name.removesuffix(".strm"))
        for suffix in OUTPUT_SUFFIXES:
            stem.with_name(stem.name + suffix).unlink(missing_ok=True)
        parent = old_strm.parent
        root = self.store.output_dir
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    async def write_cover(self, fetcher: Fetcher, v: dict, strm: Path) -> bool:
        """下载封面并裁 poster，返回是否已具备封面。网络失败抛 FetchError 由任务重试。"""
        s = self.store.current
        if not s.download_cover or not v.get("cover_url"):
            return False
        base = strm.with_name(strm.name.removesuffix(".strm"))
        fanart = base.with_name(base.name + "-fanart.jpg")
        poster = base.with_name(base.name + "-poster.jpg")
        if fanart.exists() and (poster.exists() or not s.poster_crop):
            return True
        try:
            data = await fetcher.get_bytes(v["cover_url"])
        except NotFound:
            log.warning("%s 封面不存在：%s", v["slug"], v["cover_url"])
            return False

        def save() -> None:
            write_atomic(fanart, data)
            if s.poster_crop:
                write_atomic(poster, crop_poster(data))

        await asyncio.to_thread(save)
        return True
