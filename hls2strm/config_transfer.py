"""可移植的输出库、订阅配置；预览无副作用，确认后在同一事务中导入。"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import now
from .rules import normalize_rule
from .sites import SITES, get_site


class TransferModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LibraryConfig(TransferModel):
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    dir: str = Field(min_length=1)
    external_dir: str = ""
    path_template: str = ""
    versions: Literal["", "emby", "suffix"] = ""
    rule: dict | None = None
    sources: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)

    @field_validator("rule")
    @classmethod
    def check_rule(cls, rule):
        if rule:
            unknown = set(rule) - {"categories", "tags", "models", "quality", "keywords", "match"}
            if unknown:
                raise ValueError(f"未知规则字段：{', '.join(sorted(unknown))}")
            for key, values in rule.items():
                if key != "match" and not (isinstance(values, str) or
                        isinstance(values, list) and all(isinstance(v, str) for v in values)):
                    raise ValueError(f"规则 {key} 必须是字符串或字符串数组")
        return normalize_rule(rule)


class SubscriptionConfig(TransferModel):
    name: str = Field(min_length=1, max_length=200)
    site: str
    source: str
    sort: str = ""
    library: str
    detail: bool = False
    interval: int = Field(120, ge=0)
    stop_after_known: int = Field(48, ge=1)
    max_pages: int = Field(30, ge=1)
    enabled: bool = False
    initial_full: bool = True


class Configuration(TransferModel):
    format: Literal["hls2strm-library-config"] = "hls2strm-library-config"
    version: Literal[1] = 1
    description: str = ""
    libraries: list[LibraryConfig] = Field(max_length=300)
    subscriptions: list[SubscriptionConfig] = Field(default_factory=list, max_length=2000)


class ImportRequest(TransferModel):
    config: Configuration
    activate_subscriptions: bool = False
    preview_token: str = ""


LIB_FIELDS = ("name", "dir", "external_dir", "path_template", "versions", "rule", "sources", "excludes")
SUB_FIELDS = ("name", "site", "source", "sort", "library_id", "detail", "interval", "stop_after_known", "max_pages")


async def export_config(db) -> dict:
    libraries = await db.list_libraries()
    subscriptions = await db.list_subscriptions()
    keys = {lib["id"]: f"library-{lib['id']}" for lib in libraries}
    return Configuration(
        libraries=[LibraryConfig(key=keys[lib["id"]], **{
            **{k: lib[k] for k in LIB_FIELDS},
            "sources": [keys[i] for i in lib["sources"]],
            "excludes": [keys[i] for i in lib["excludes"]],
        }) for lib in libraries],
        subscriptions=[SubscriptionConfig(
            **{k: sub[k] for k in SUB_FIELDS if k != "library_id"},
            library=keys[sub["library_id"]], enabled=bool(sub["enabled"]),
            # 配置不携带影片和抓取历史，新实例必须重新初始化；复用时保留现有进度。
            initial_full=True,
        ) for sub in subscriptions],
    ).model_dump()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def preview_config(engine, request: ImportRequest) -> dict:
    """只操作校验用的引擎副本，不改运行中引擎、数据库、文件或任务。"""
    current = await engine.db.list_libraries()
    subscriptions = await engine.db.list_subscriptions()
    view = copy.copy(engine)
    view.libs = {lib["id"]: dict(lib) for lib in current}
    by_name = {lib["name"]: lib for lib in current}
    config = request.config.model_copy(deep=True)
    keys, names = {}, set()
    next_id = max(view.libs, default=0) + 1
    for lib in config.libraries:
        lib.name = lib.name.strip()
        if lib.key in keys or lib.name in names:
            raise ValueError("配置中的输出库 key 和名称必须分别唯一")
        names.add(lib.name)
        existing = by_name.get(lib.name)
        keys[lib.key] = existing["id"] if existing else next_id
        if not existing:
            next_id += 1

    def refs(values):
        missing = set(values) - keys.keys()
        if missing:
            raise ValueError(f"引用了未定义的输出库 key：{', '.join(sorted(missing))}")
        return sorted({keys[key] for key in values})

    plans = []
    for lib in config.libraries:
        lid = keys[lib.key]
        name, directory, template, external = view._check_library(
            lib.name, lib.dir, lib.path_template, lib.external_dir, lid)
        fields = dict(name=name, dir=directory, path_template=template, external_dir=external,
                      versions=lib.versions, rule=lib.rule, sources=refs(lib.sources), excludes=refs(lib.excludes))
        existing = by_name.get(name)
        if existing and any(existing[k] != fields[k] for k in LIB_FIELDS):
            raise ValueError(f"输出库「{name}」已存在且配置不同；请修改导入内容，或先在输出库页面编辑现有库")
        view.libs[lid] = {"id": lid, **fields}
        plans.append({"key": lib.key, "id": lid, "action": "reuse" if existing else "create", **fields,
                      "root": str(view.writer.library_root(fields)),
                      "external_root": str(view.writer.external_root(fields) or "")})
    # 所有库均已放入副本，允许前向引用，同时检查完整依赖图。
    for lib in plans:
        view._check_links(lib["id"], lib["sources"], lib["excludes"])
        lib["source_names"] = [view.libs[i]["name"] for i in lib["sources"]]
        lib["exclude_names"] = [view.libs[i]["name"] for i in lib["excludes"]]

    sub_plans, seen_names, seen_sources = [], set(), set()
    warnings = ["仅导入输出库和订阅定义；不包含全局设置、密钥、影片、历史任务和抓取进度。",
                "已有库和订阅只复用相同配置，不覆盖、不删除；新订阅默认停用。"]
    for sub in config.subscriptions:
        name = sub.name.strip()
        if not name or name in seen_names:
            raise ValueError("配置中的订阅名称不能为空或重复")
        seen_names.add(name)
        if sub.site not in SITES:
            raise ValueError(f"未知站点：{sub.site}")
        site = get_site(sub.site)
        if sub.sort not in site.sorts:
            raise ValueError(f"{site.label} 不支持排序 {sub.sort}")
        source = site.normalize_source(sub.source)
        if any(ch in source for ch in ("<", ">")) or "%3C" in source.upper():
            raise ValueError(f"订阅「{name}」的列表地址仍有占位符")
        lid = refs([sub.library])[0]
        identity = (site.name, source, sub.sort, lid)
        if identity in seen_sources:
            raise ValueError(f"订阅「{name}」与配置中的另一订阅来源、排序和输出库重复")
        seen_sources.add(identity)
        fields = {**sub.model_dump(exclude={"library", "initial_full"}), "name": name,
                  "site": site.name, "source": source, "library_id": lid}
        matches = [s for s in subscriptions if s["name"] == name]
        # 防止把同一订阅改个名字再次导入。
        matches += [s for s in subscriptions if s["name"] != name and
                    (s["site"], s["source"], s["sort"], s["library_id"]) == identity]
        if matches:
            if len(matches) != 1 or any(matches[0][k] != fields[k] for k in SUB_FIELDS):
                raise ValueError(f"订阅「{name}」与现有订阅名称或来源冲突；请先在订阅页面处理")
            enabled = bool(matches[0]["enabled"])
        else:
            enabled = sub.enabled and request.activate_subscriptions
        fields["enabled"] = enabled
        sub_plans.append({**fields, "library_key": sub.library, "library_name": view.libs[lid]["name"],
                          "action": "reuse" if matches else "create", "initial_full": sub.initial_full})
        if not engine.store.current.site(site.name).enabled:
            warnings.append(f"{site.label} 当前未启用，运行订阅前需在设置页启用站点。")
        if enabled and not matches:
            warnings.append(f"订阅「{name}」确认后启用；" + (
                "需手动执行首轮全量，完成后定时增量。" if sub.initial_full else "调度器可能立即执行增量。"))
    external = [lib for lib in view.libs.values() if lib["external_dir"]]
    if len(external) > 1:
        exclusive = all(a["id"] in b["excludes"] or b["id"] in a["excludes"]
                        for i, a in enumerate(external) for b in external[i + 1:])
        warnings.append("外部整理库已配置两两排除；首次使用前需先完成分类列表，再开放 MDCNG 监控。"
                        if exclusive else "存在未互斥的外部整理库，同一作品进入多个库会重复刮削。")
        warnings.append("后续站点新加分类导致影片换库时仍可能再次刮削；分类订阅停用后不再参与归并等待。")
    if any(lib["action"] == "create" and (lib["rule"] or lib["sources"]) for lib in plans):
        warnings.append("新库的规则与来源关系确认后生效；要补入已有影片，请在库上执行重新归库。来源库归并也会由后台执行。")
    if any(lib["versions"] for lib in plans):
        warnings.append("已开启多画质版本，会产生额外 STRM；MDCNG 不应监控这些版本文件。")
    token = _digest({"config": config.model_dump(), "activate": request.activate_subscriptions,
                     "libraries": [{k: l[k] for k in ("id", *LIB_FIELDS)} for l in current],
                     "subscriptions": [{k: s[k] for k in ("id", *SUB_FIELDS, "enabled", "initialized")}
                                       for s in subscriptions],
                     "settings": engine.store.current.model_dump(), "root": str(engine.store.output_dir)})
    return {"preview_token": token, "libraries": plans, "subscriptions": sub_plans,
            "warnings": list(dict.fromkeys(warnings))}


async def apply_config(engine, request: ImportRequest) -> dict:
    # 再次校验和写入持有同一数据库写锁，避免确认后只导入半份配置。
    async with engine.db._tx():
        plan = await preview_config(engine, request)
        if not request.preview_token or request.preview_token != plan["preview_token"]:
            raise ValueError("配置或当前环境已变化，请重新预览后确认导入")
        conn = engine.db.conn
        ids, created = {}, 0
        for lib in plan["libraries"]:
            if lib["action"] == "reuse":
                ids[lib["id"]] = lib["id"]
                continue
            fields = {k: lib[k] for k in LIB_FIELDS}
            fields.update(rule=json.dumps(fields["rule"], ensure_ascii=False) if fields["rule"] else "",
                          sources="[]", excludes="[]", created_at=now())
            cursor = await conn.execute(
                f"INSERT INTO libraries({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(fields.values()))
            ids[lib["id"]] = cursor.lastrowid
            created += 1
        for lib in plan["libraries"]:
            if lib["action"] == "create":
                await conn.execute("UPDATE libraries SET sources=?, excludes=? WHERE id=?", (
                    json.dumps([ids[i] for i in lib["sources"]]),
                    json.dumps([ids[i] for i in lib["excludes"]]), ids[lib["id"]]))
        sub_count = 0
        for sub in plan["subscriptions"]:
            if sub["action"] == "reuse":
                continue
            fields = {k: sub[k] for k in SUB_FIELDS}
            fields.update(library_id=ids[sub["library_id"]], enabled=int(sub["enabled"]),
                          initialized=int(not sub["initial_full"]), created_at=now())
            await conn.execute(
                f"INSERT INTO subscriptions({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(fields.values()))
            sub_count += 1
    await engine.reload_libraries()
    engine.notify()
    return {"libraries_created": created, "subscriptions_created": sub_count}
