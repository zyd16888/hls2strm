"""启动配置（环境变量）与运行时设置（存数据库，可在 Web 页面修改）。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .sites import SITES


def env(name: str, default: str = "") -> str:
    """环境变量 HLS2STRM_{name}；改名前的 JABLE_{name} 也认，而且优先：
    镜像里用新名字设了默认值，老部署在 compose 里显式写的旧名字不能被默认值盖掉。"""
    old = os.environ.get(f"JABLE_{name}")
    return old if old is not None else os.environ.get(f"HLS2STRM_{name}", default)


class BootConfig(BaseModel):
    """进程启动时就要确定、运行中不能改的配置。"""

    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8080
    ui_user: str = "admin"
    ui_password: str = ""
    log_level: str = "INFO"
    # 运行时设置里对应项留空时使用的默认值
    default_output_dir: str = ""
    default_public_base_url: str = ""

    @classmethod
    def from_env(cls) -> BootConfig:
        port = int(env("PORT", "8080"))
        return cls(
            data_dir=Path(env("DATA_DIR", "data")).resolve(),
            host=env("HOST", "0.0.0.0"),
            port=port,
            ui_user=env("UI_USER", "admin"),
            ui_password=env("UI_PASSWORD", ""),
            log_level=env("LOG_LEVEL", "INFO").upper(),
            default_output_dir=env("OUTPUT_DIR", ""),
            default_public_base_url=env("PUBLIC_BASE_URL", f"http://127.0.0.1:{port}"),
        )


def check_path_template(v: str) -> str:
    v = v.strip().strip("/")
    if "{slug}" not in v:
        raise ValueError("路径模板必须包含 {slug}，否则不同影片会互相覆盖")
    try:
        v.format(slug="x", code="x", actor="x", year="x")
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"路径模板无效：{e}") from e
    return v


def normalize_domains(v: list[str]) -> list[str]:
    out = []
    for d in v:
        d = d.strip().rstrip("/")
        if not d:
            continue
        if not d.startswith(("http://", "https://")):
            d = "https://" + d
        out.append(d)
    return out


class LineConfig(BaseModel):
    """多线路站点上一条线路的设置。"""

    enabled: bool = True
    proxy: bool = False  # 强制由本服务中转（比如直链在外网播放器上打不开时）


class SiteConfig(BaseModel):
    """单个站点的抓取设置；每个站点各自限速、冷却，一个站被拦不影响其他站。"""

    enabled: bool = True
    domains: list[str] = Field(default_factory=list)
    rate_per_sec: float = Field(1.0, gt=0, le=20)
    concurrency: int = Field(2, ge=1, le=16)
    solver: bool = True  # 全部域名被拦时是否调用解题服务
    lines: dict[str, LineConfig] = Field(default_factory=dict)  # 多线路站点：每条线路的设置
    line_order: list[str] = Field(default_factory=list)  # 线路的优先顺序，前面的先试

    def line(self, name: str) -> LineConfig:
        return self.lines.get(name) or LineConfig()

    def line_rank(self, name: str) -> int:
        return self.line_order.index(name) if name in self.line_order else len(self.line_order)

    @field_validator("domains")
    @classmethod
    def _check_domains(cls, v: list[str]) -> list[str]:
        return normalize_domains(v)


def default_sites() -> dict[str, SiteConfig]:
    return {name: SiteConfig(enabled=site.default_enabled, domains=list(site.default_domains))
            for name, site in SITES.items()}


class Settings(BaseModel):
    """运行时设置。字段说明会显示在 Web 设置页。"""

    # 站点
    sites: dict[str, SiteConfig] = Field(
        default_factory=default_sites,
        description="各站点的启用、域名（按顺序优先使用，被拦的进入冷却并切到下一个）、限速、并发",
    )
    site_priority: list[str] = Field(
        default_factory=lambda: list(SITES),
        description="同一部影片有多个源时，站点的优先顺序（播放择优、元数据取用都按它）",
    )
    # 抓取
    proxy: str = Field("", description="抓取代理，如 http://host:port、socks5h://user:pass@host:port")
    impersonate: str = Field("chrome", description="curl_cffi 模拟的浏览器指纹")
    request_timeout: int = Field(30, ge=5, le=300, description="单次请求超时（秒）")
    domain_cooldown: int = Field(300, ge=10, le=86400, description="域名被拦后的首次冷却（秒），连续被拦翻倍，上限 1 小时")
    solver_url: str = Field("", description="Byparr / FlareSolverr 地址，如 http://byparr:8191；留空不启用")
    solver_timeout: int = Field(60, ge=10, le=300, description="解题超时（秒）")
    # 重试
    max_attempts: int = Field(5, ge=1, le=20, description="单个任务最多尝试次数")
    retry_base_delay: int = Field(30, ge=1, le=3600, description="首次重试等待（秒），之后每次 ×4，上限 2 小时")
    # 任务
    fetch_detail: bool = Field(True, description="新建列表任务和订阅时，默认是否抓详情页（女优、标签、上市日期、封面）")
    auto_probe_sites: list[str] = Field(
        default_factory=list,
        description="新片自动补源：列表任务、订阅收进新影片时，到这些站点按番号找备用源（填站点名，如 missav）；留空不自动找",
    )
    probe_recheck_days: int = Field(30, ge=1, le=3650, description="补源时某个站没有这部影片，多少天内不再去查")
    external_restore: bool = Field(
        True,
        description="外部整理库的 strm 在收件目录和外部整理目录里都找不到时（按内容找，外部工具改名、加后缀也认得出），"
                    "重新写进收件目录交给外部工具再整理；外部整理目录不存在或是空的时候不补，多半是挂载出了问题",
    )
    # 输出
    output_dir: str = Field(
        "", description="输出根目录，各输出库的相对目录都在它下面；留空使用 HLS2STRM_OUTPUT_DIR 或 数据目录/strm"
    )
    path_template: str = Field(
        "{slug}/{slug}",
        description="默认路径模板（不含扩展名，输出库可单独覆盖），必须包含 {slug}；可用变量 {slug} {code} {actor} {year}",
    )
    write_nfo: bool = Field(True, description="写 nfo（Kodi 格式，Emby/Jellyfin 通用）")
    download_cover: bool = Field(True, description="下载封面 fanart")
    poster_crop: bool = Field(True, description="从封面裁出竖版 poster")
    # 播放
    public_base_url: str = Field("", description="Emby/Jellyfin 访问本服务的地址，写进 strm；留空使用 HLS2STRM_PUBLIC_BASE_URL")
    play_mode: Literal["redirect", "proxy", "direct"] = Field(
        "redirect",
        description="redirect：302 到 CDN；proxy：本服务中转视频流；direct：strm 直接写 CDN 地址（约 3 小时失效，仅调试）",
    )
    proxy_user_agents: list[str] = Field(
        default_factory=lambda: ["Lavf", "python-requests"],
        description="redirect 模式下，User-Agent 含这些片段的客户端改走本服务中转（CDN 会拒绝它们，如 ffmpeg 默认的 Lavf）",
    )
    play_token: str = Field("", description="播放地址访问令牌；设置后 strm 地址带 ?t=令牌")
    resolve_token: str = Field(
        "", description="供 embyGateway 等调用 /api/resolve 的令牌（Bearer）；留空则不开放该接口"
    )
    hls_margin: int = Field(15, ge=0, le=120, description="302 前要求播放地址剩余有效期 ≥ 影片时长 + 该值（分钟）")
    subtitle_priority: list[Literal["zh", "none", "en"]] = Field(
        default_factory=lambda: ["zh", "none", "en"],
        description="同一部影片有多个源时的字幕偏好：zh 中文字幕、none 无字幕、en 英文字幕，排前面的优先",
    )
    subtitle_fallback: bool = Field(
        True, description="首选字幕的源都不能用时，是否用其他字幕的源顶上（比如中字源失效时播无字幕版）"
    )
    resolve_timeout: int = Field(20, ge=5, le=120, description="一次播放请求挑源、取地址的总时限（秒），超时不再试后面的源")
    play_discover: bool = Field(
        True, description="现场找源：影片已知的源都不能用（或库里没有这部影片）时，按番号到其他启用的站点找一次"
    )
    resolve_proxy_url: str = Field(
        "", description="网关 resolve 用：作品只有要中转的源（如 MissAV）时返回这个地址下的中转链接，须能从公网访问；留空返回 409"
    )

    @model_validator(mode="after")
    def _fill_sites(self) -> Settings:
        """补齐新加的站点；域名留空用站点默认；优先顺序去掉未知站点、补上漏掉的。"""
        for name, site in SITES.items():
            cfg = self.sites.get(name) or SiteConfig(enabled=site.default_enabled)
            if not cfg.domains:
                cfg.domains = list(site.default_domains)
            for line, spec in site.line_specs.items():
                cfg.lines.setdefault(line, LineConfig(enabled=spec.default_enabled))
            order = [n for n in dict.fromkeys(cfg.line_order) if n in cfg.lines]
            cfg.line_order = order + [n for n in cfg.lines if n not in order]
            self.sites[name] = cfg
        self.sites = {k: v for k, v in self.sites.items() if k in SITES}
        order = [n for n in dict.fromkeys(self.site_priority) if n in SITES]
        self.site_priority = order + [n for n in SITES if n not in order]
        return self

    def site(self, name: str) -> SiteConfig:
        return self.sites[name]

    def site_rank(self, name: str) -> int:
        """站点优先级，越小越优先。"""
        return self.site_priority.index(name) if name in self.site_priority else len(self.site_priority)

    @field_validator("path_template")
    @classmethod
    def _check_template(cls, v: str) -> str:
        return check_path_template(v)

    @field_validator("public_base_url", "solver_url", "proxy", "resolve_proxy_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().rstrip("/")


def migrate_settings(data: dict) -> dict:
    """单站点时代的 domains、rate_per_sec、concurrency 搬到 sites.jable 下。"""
    if "sites" in data:
        return data
    jable = {k: data.pop(k) for k in ("domains", "rate_per_sec", "concurrency") if k in data}
    if jable:
        data["sites"] = {"jable": jable}
    return data


class SettingsStore:
    """持有当前设置，负责持久化和变更通知。"""

    def __init__(self, boot: BootConfig, db) -> None:
        self.boot = boot
        self._db = db
        self.current = Settings()
        self._listeners: list[Callable[[Settings, Settings], None]] = []

    async def load(self) -> None:
        raw = await self._db.get_setting("settings")
        if raw:
            data = migrate_settings(json.loads(raw))
            known = {k: v for k, v in data.items() if k in Settings.model_fields}
            self.current = Settings.model_validate(known)

    async def update(self, patch: dict) -> Settings:
        """改设置；sites 按站点、按字段合并，只传改动的部分就行。"""
        data = self.current.model_dump()
        patch = dict(patch)
        for name, cfg in (patch.pop("sites", None) or {}).items():
            data["sites"][name] = {**data["sites"].get(name, {}), **cfg}
        new = Settings.model_validate({**data, **patch})
        old, self.current = self.current, new
        await self._db.set_setting("settings", new.model_dump_json())
        for fn in self._listeners:
            fn(old, new)
        return new

    def on_change(self, fn: Callable[[Settings, Settings], None]) -> None:
        self._listeners.append(fn)

    @property
    def output_dir(self) -> Path:
        d = self.current.output_dir or self.boot.default_output_dir
        return Path(d).resolve() if d else self.boot.data_dir / "strm"

    @property
    def public_base_url(self) -> str:
        return (self.current.public_base_url or self.boot.default_public_base_url).rstrip("/")
