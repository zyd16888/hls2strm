"""嵌入播放站（线路背后的播放器）：从嵌入页解出直链。SupJav、JavGuru 等多线路站点共用。

播放站常换域名，所以按页面内容认（hint 是站点给的线路名，只在内容认不出时参考）：
  vidhide     EarnVids / VidHide 一系（SupJav 的 EVS、FST）：packer 解包取 hls2（签名 m3u8，s + e 秒后过期，带 asn）
  voe         跳转页 → 混淆的 JSON，取 source（m3u8，约 4 小时，带 IP 前两段和 asn）
  streamtape  robotlink 拼出 get_video（302 到 mp4，约 24 小时；实测播放器能直连）
  vidara      SupJav 的 VAS：POST {嵌入页域名}/api/stream 拿 streaming_url；token 里有取地址的 IP，
              分片伪装成 .woff2（新版 ffmpeg 拒收），只能中转
  lulustream  SupJav 的 LUC：packer 解包取 file；CDN 只认浏览器指纹，只能中转
  turbovip    SupJav 的 TV（turboviplay）：页面里 urlPlay 给出 m3u8；分片放在别的 CDN 上、前面加了假 PNG 头，
              只能中转（剥掉假头）；请求要带 Referer: https://supjav.com/
"""

from __future__ import annotations

import base64
import codecs
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urljoin, urlsplit

from .errors import FetchError, ParseError
from .sites.base import StreamTraits
from .sites.missav import unpack

if TYPE_CHECKING:
    from .fetcher import Fetcher

HOST_TRAITS: dict[str, StreamTraits] = {
    "vidhide": StreamTraits(direct=True, expires=True, ip_bound=True),
    "voe": StreamTraits(direct=True, expires=True, ip_bound=True),
    "streamtape": StreamTraits(direct=True, expires=True),
    "vidara": StreamTraits(direct=False, expires=True, disguised_segments=True),
    "lulustream": StreamTraits(direct=False, expires=True),
    "turbovip": StreamTraits(direct=False, expires=True, fake_header=True, headers={"Referer": "https://supjav.com/"}),
}
HOST_LABELS = {"vidhide": "VidHide", "voe": "VOE", "streamtape": "Streamtape", "vidara": "Vidara",
               "lulustream": "LuluStream", "turbovip": "TurboVip"}

_HLS2_RE = re.compile(r'"?hls2"?\s*:\s*"([^"]+)"')
_FILE_RE = re.compile(r'file\s*:\s*"(https?://[^"]+?\.m3u8[^"]*)"')
_VOE_JUMP_RE = re.compile(r"window\.location\.href\s*=\s*'([^']+)'")
_VOE_JSON_RE = re.compile(r'<script type="application/json">\s*(\[.*?\])\s*</script>', re.S)
_ST_RE = re.compile(
    r"getElementById\(\s*['\"]robotlink['\"]\s*\)\.innerHTML\s*=\s*['\"]([^'\"]*)['\"]\s*\+\s*(?:['\"]{2}\s*\+\s*)?"
    r"\(?\s*['\"]([^'\"]*)['\"]\s*\)?((?:\.substring\(\s*\d+\s*\))+)"
)
_VAS_TOKEN_TS_RE = re.compile(r"token=[0-9a-f]+-(\d{9,11})-")
_URLPLAY_RE = re.compile(r"""urlPlay[\s=:'"]+(https?://[^\s'"\\]+\.m3u8[^\s'"\\]*)""")
_TS_SYNC_PROBE = 8192


def ts_start(data: bytes) -> int:
    """假文件头后面 TS 的起点：第一个之后连续 5 个包（每包 188 字节）都以 0x47 开头的位置；找不到返回 -1。"""
    for i in range(min(len(data), _TS_SYNC_PROBE)):
        if data[i] == 0x47 and all(i + 188 * k < len(data) and data[i + 188 * k] == 0x47 for k in range(1, 5)):
            return i
    return -1


@dataclass
class HostStream:
    url: str
    expires: int
    host: str


def expires_of(url: str, default_ttl: int) -> int:
    """地址里的过期时间：expires=（streamtape）、s + e（vidhide / voe / lulu）、token 里的时间戳（vidara）。"""
    q = parse_qs(urlsplit(url).query)
    if "expires" in q and q["expires"][0].isdigit():
        return int(q["expires"][0])
    if q.get("s", [""])[0].isdigit() and q.get("e", [""])[0].isdigit():
        return int(q["s"][0]) + int(q["e"][0])
    if m := _VAS_TOKEN_TS_RE.search(url):
        return int(m.group(1))
    return int(time.time()) + default_ttl


def decode_voe(html: str) -> dict:
    """VOE 页面里混淆的 JSON：rot13 → 去掉干扰符号 → base64 → 每个字符减 3 → 倒序 → base64。"""
    m = _VOE_JSON_RE.search(html)
    if not m:
        raise ParseError("VOE 页面里没有播放数据")
    s = codecs.decode(json.loads(m.group(1))[0], "rot13")
    for junk in ("@$", "^^", "~@", "%?", "*~", "!!", "#&"):
        s = s.replace(junk, "")
    s = base64.b64decode(s + "=" * (-len(s) % 4)).decode("latin-1")
    s = "".join(chr(ord(c) - 3) for c in s)[::-1]
    return json.loads(base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8"))


def detect(html: str, hint: str = "") -> str:
    """按嵌入页内容认出播放站类型；认不出返回空串。"""
    if "robotlink" in html:
        return "streamtape"
    if _URLPLAY_RE.search(html.replace("\\/", "/")):
        return "turbovip"
    if _VOE_JSON_RE.search(html) or (_VOE_JUMP_RE.search(html) and "voe" in (hint + html[:3000]).lower()):
        return "voe"
    if "api/stream" in html and "jwplayer" in html:
        return "vidara"
    if "eval(function(p,a,c,k,e,d)" in html:
        js = "\n".join(unpack(html))
        if _HLS2_RE.search(js):
            return "vidhide"
        if _FILE_RE.search(js):
            return "lulustream"
    return ""


async def resolve_embed(http: Fetcher, embed_url: str, referer: str, hint: str = "") -> HostStream:
    """打开嵌入页（带站点要求的 Referer），认出播放站并解出直链。"""
    page = await http.fetch(embed_url, headers={"Referer": referer} if referer else None)
    if page.status_code != 200:
        raise FetchError(f"播放站返回 HTTP {page.status_code}")
    html = page.text
    host = detect(html, hint)
    if host == "vidhide":
        for js in unpack(html):
            if m := _HLS2_RE.search(js):
                url = urljoin(embed_url, m.group(1).replace("\\/", "/"))
                return HostStream(url, expires_of(url, 36 * 3600), host)
    elif host == "voe":
        if not _VOE_JSON_RE.search(html) and (m := _VOE_JUMP_RE.search(html)):
            html = (await http.fetch(m.group(1), headers={"Referer": referer} if referer else None)).text
        url = decode_voe(html).get("source") or ""
        if url:
            return HostStream(url, expires_of(url, 4 * 3600), host)
    elif host == "streamtape":
        m = _ST_RE.search(html)
        if m:
            prefix, suffix, ops = m.groups()
            for n in re.findall(r"substring\(\s*(\d+)\s*\)", ops):
                suffix = suffix[int(n):]
            url = "https:" + prefix + suffix + "&stream=1"
            return HostStream(url, expires_of(url, 24 * 3600), host)
    elif host == "vidara":
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(embed_url))
        code = urlsplit(embed_url).path.rstrip("/").rsplit("/", 1)[-1]
        resp = await http.fetch(f"{origin}/api/stream", method="POST", json={"filecode": code, "device": "web"},
                                headers={"Referer": embed_url})
        if resp.status_code != 200:
            raise FetchError(f"Vidara 接口返回 HTTP {resp.status_code}")
        url = (resp.json() or {}).get("streaming_url") or ""
        if url:
            return HostStream(url, expires_of(url, 8 * 3600), host)
    elif host == "turbovip":
        if m := _URLPLAY_RE.search(html.replace("\\/", "/")):
            url = m.group(1)
            return HostStream(url, expires_of(url, 6 * 3600), host)
    elif host == "lulustream":
        for js in unpack(html):
            if m := _FILE_RE.search(js):
                url = m.group(1)
                return HostStream(url, expires_of(url, 8 * 3600), host)
    else:
        raise ParseError(f"认不出的播放站（{urlsplit(embed_url).hostname}）")
    raise ParseError(f"{HOST_LABELS[host]} 页面里没有播放地址")
