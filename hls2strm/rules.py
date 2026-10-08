"""规则库：按分类、标签、女优、画质、关键词自动归库。

规则格式：{"categories": [], "tags": [], "models": [], "quality": [], "keywords": [], "match": "any|all"}
分类、标签可以写站点上的名称（如「中文字幕」）或 slug（如 chinese-subtitle）；女优写名字或 id。
各组条件之间按 match 组合：any = 满足任一组，all = 每组都满足；组内是「任一」。
"""

from __future__ import annotations

RULE_LISTS = ("categories", "tags", "models", "quality", "keywords")


def normalize_rule(raw: dict | None) -> dict | None:
    """清洗规则；没有任何条件时返回 None（不是规则库）。"""
    if not raw:
        return None
    rule: dict = {}
    for key in RULE_LISTS:
        values = raw.get(key) or []
        if isinstance(values, str):
            values = values.replace("，", ",").split(",")
        rule[key] = list(dict.fromkeys(v.strip() for v in values if str(v).strip()))
    match = raw.get("match", "any")
    if match not in ("any", "all"):
        raise ValueError("match 只能是 any 或 all")
    rule["match"] = match
    if not any(rule[k] for k in RULE_LISTS):
        return None
    return rule


def _keys(items: list[dict], *fields: str) -> set[str]:
    return {str(item.get(f, "")).strip().lower() for item in items for f in fields if item.get(f)}


def match_rule(rule: dict, v: dict) -> bool:
    checks = []
    if rule["categories"]:
        have = _keys(v.get("categories") or [], "slug", "name")
        checks.append(any(c.lower() in have for c in rule["categories"]))
    if rule["tags"]:
        have = _keys(v.get("tags") or [], "slug", "name")
        checks.append(any(t.lower() in have for t in rule["tags"]))
    if rule["models"]:
        have = _keys(v.get("models") or [], "id", "name")
        checks.append(any(m.lower() in have for m in rule["models"]))
    if rule["quality"]:
        quality = (v.get("quality") or "").lower()
        checks.append(any(q.lower() in quality for q in rule["quality"]))
    if rule["keywords"]:
        text = f"{v.get('code', '')} {v.get('title', '')}".lower()
        checks.append(any(k.lower() in text for k in rule["keywords"]))
    if not checks:
        return False
    return any(checks) if rule["match"] == "any" else all(checks)


def describe_rule(rule: dict | None) -> str:
    if not rule:
        return ""
    names = {"categories": "分类", "tags": "标签", "models": "女优", "quality": "画质", "keywords": "关键词"}
    parts = [f"{names[k]}：{'、'.join(rule[k])}" for k in RULE_LISTS if rule[k]]
    return (" 或 " if rule["match"] == "any" else " 且 ").join(parts)
