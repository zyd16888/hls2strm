"""番号规范化与跨站匹配键。

各站对同一部片的写法不同：Jable 写 `fc2ppv-1066192`，MissAV 写 `fc2-ppv-1066192`；有的站编号补零到 5 位（`ssis00001`）。
匹配键把这些统一：前缀大写、去掉分隔符，编号去掉前导零，FC2 统一成 FC2PPV。
日期型番号（无码厂牌的 `010120-001`）前面没有字母，原样比较；`_` 和 `-` 不能混为一谈：
Caribbeancom 写 `100826-001`，一本道、Caribbeancompr、pacopacomama、10musume 写 `100826_001`，是不同的片。
"""

from __future__ import annotations

import re

_FC2_RE = re.compile(r"^FC2[\s_-]*(?:PPV[\s_-]*)?(\d{5,9})$")
_SPLIT_RE = re.compile(r"[\s_-]+")
_GLUED_RE = re.compile(r"([A-Z]+)(\d+)")
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9._-]+")
_DATE_SEP_RE = re.compile(r"[\s-]+")


def code_key(code: str) -> str:
    """'SSIS-001' / 'ssis00001' -> 'SSIS-1'；'FC2-491887' / 'fc2ppv-491887' -> 'FC2PPV-491887'。"""
    s = code.strip().upper()
    if not s:
        return ""
    if m := _FC2_RE.match(s):
        return f"FC2PPV-{int(m.group(1))}"
    if not any(c.isalpha() for c in s):
        return _DATE_SEP_RE.sub("-", s)  # 日期型番号：只统一空白和横线，下划线原样保留
    tokens = [t for t in _SPLIT_RE.split(s) if t]
    if len(tokens) == 1:
        m = _GLUED_RE.fullmatch(tokens[0])
        if not m:
            return tokens[0]
        tokens = [m.group(1), m.group(2)]
    # 第一个纯数字、且前面有字母的段是编号，后面的段（分段 -2 等）原样保留
    for i in range(1, len(tokens)):
        prefix = "".join(tokens[:i])
        if tokens[i].isdigit() and any(c.isalpha() for c in prefix):
            return "-".join([prefix, str(int(tokens[i])), *tokens[i + 1:]])
    return "-".join(tokens)


def work_slug(code: str, uncensored: bool = False) -> str:
    """新作品的 slug（/play/{slug}.m3u8 和默认文件名用）：番号小写，无码流出版加 -u。"""
    base = _SLUG_UNSAFE_RE.sub("-", code.strip().lower()).strip("-._") or "x"
    return (base[:78] + "-u") if uncensored else base[:80]
