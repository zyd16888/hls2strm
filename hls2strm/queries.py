"""影片筛选与排序：参数白名单、索引关联条件和稳定分页。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VIDEO_SORTS = {  # 影片库排序：参数 -> 列
    "created": "id",
    "release": "release_date",
    "duration": "duration",
    "code": "code_key",
    "views": "views",
    "updated": "updated_at",
}
FACET_FIELDS = {"categories": "slug", "tags": "slug", "models": "id"}  # JSON 列 -> 元素里当 id 用的字段


@dataclass
class VideoQuery:
    """影片库的筛选和排序。列表类条件组内任一满足即可，不同条件之间都要满足。"""

    q: str = ""
    status: str = ""  # no_detail / no_output / gone
    library_id: int | None = None
    has_site: list[str] = field(default_factory=list)  # 有其中任一站点的可用源
    lacks_site: list[str] = field(default_factory=list)  # 这些站点都没有源
    sources: str = ""  # single / multi / failing（有源最近取地址失败）/ none（没有可用源）
    subtitle: str = ""  # zh / en / none（按可用源的字幕）
    uncensored: bool | None = None
    models: list[str] = field(default_factory=list)  # 女优 id
    categories: list[str] = field(default_factory=list)  # 分类 slug
    tags: list[str] = field(default_factory=list)
    makers: list[str] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)
    release_from: str = ""  # YYYY-MM-DD
    release_to: str = ""
    added_from: int | None = None  # 入库时间（unix 秒）
    added_to: int | None = None
    duration_min: int | None = None  # 秒
    duration_max: int | None = None
    sort: str = "created"
    desc: bool = True

    def where(self) -> tuple[str, list[Any]]:
        where, params = ["1=1"], []

        def marks(values: list) -> str:
            params.extend(values)
            return ",".join("?" * len(values))

        if self.q.strip():
            like = f"%{self.q.strip()}%"
            where.append("(slug LIKE ? OR code LIKE ? OR title LIKE ? OR models LIKE ? OR tags LIKE ?)")
            params += [like] * 5
        if self.library_id:
            where.append("EXISTS (SELECT 1 FROM outputs o WHERE o.video_id=videos.id AND o.library_id=?)")
            params.append(self.library_id)
        if self.status == "no_detail":
            where.append("detail_at IS NULL AND status='active'")
        elif self.status == "no_output":
            where.append("status='active' AND NOT EXISTS "
                         "(SELECT 1 FROM outputs o WHERE o.video_id=videos.id AND o.strm_path!='')")
        elif self.status == "gone":
            where.append("status='gone'")
        elif self.status == "active":
            where.append("status='active'")
        active = "s.video_id=videos.id AND s.status='active'"
        if self.has_site:
            where.append(f"EXISTS (SELECT 1 FROM sources s WHERE {active} AND s.site IN ({marks(self.has_site)}))")
        for site in self.lacks_site:
            where.append("NOT EXISTS (SELECT 1 FROM sources s WHERE s.video_id=videos.id AND s.site=?)")
            params.append(site)
        n_active = f"(SELECT COUNT(*) FROM sources s WHERE {active})"
        if self.sources == "single":
            where.append(f"{n_active}=1")
        elif self.sources == "multi":
            where.append(f"{n_active}>=2")
        elif self.sources == "none":
            where.append(f"{n_active}=0")
        elif self.sources == "failing":
            where.append(f"EXISTS (SELECT 1 FROM sources s WHERE {active} AND s.fail_streak>0)")
        if self.subtitle in ("zh", "en"):
            where.append(f"EXISTS (SELECT 1 FROM sources s WHERE {active} AND s.subtitle=?)")
            params.append(self.subtitle)
        elif self.subtitle == "none":
            where.append(f"NOT EXISTS (SELECT 1 FROM sources s WHERE {active} AND s.subtitle IN ('zh', 'en'))")
        if self.uncensored is not None:
            where.append("uncensored=?")
            params.append(int(self.uncensored))
        for col, values in (("models", self.models), ("categories", self.categories), ("tags", self.tags)):
            if values:
                where.append(f"id IN (SELECT video_id FROM video_facets WHERE kind='{col}' AND item IN ({marks(values)}))")
        if self.makers:
            where.append(f"maker IN ({marks(self.makers)})")
        if self.quality:
            where.append(f"quality IN ({marks(self.quality)})")
        for cond, value in (("release_date!='' AND release_date>=?", self.release_from),
                            ("release_date!='' AND release_date<=?", self.release_to),
                            ("created_at>=?", self.added_from), ("created_at<=?", self.added_to),
                            ("duration>=?", self.duration_min), ("duration<=?", self.duration_max)):
            if value not in (None, ""):
                where.append(cond)
                params.append(value)
        return " AND ".join(where), params

    def order(self) -> str:
        col = VIDEO_SORTS.get(self.sort, "id")
        d = "DESC" if self.desc else "ASC"
        if col == "id":
            return f"id {d}"
        # 没有值的（没抓详情、没日期）总排在最后
        return f"({col} IS NULL OR {col}='') ASC, {col} {d}, id {d}"
