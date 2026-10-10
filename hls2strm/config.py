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
        if d not in out:
            out.append(d)
    return out


class LineConfig(BaseModel):
    """多线路站点上一条线路的设置。"""

    enabled: bool = True
    proxy: bool = False  # 强制由本服务中转（比如直链在外网播放器上打不开时）
    direct_mode: Literal["auto", "allow", "proxy"] = "auto"


class SiteConfig(BaseModel):
    """单个站点的抓取设置；每个站点各自限速、冷却，一个站被拦不影响其他站。"""

    enabled: bool = True
    domains: list[str] = Field(default_factory=list)
    domain_mode: Literal["priority", "round_robin", "balanced"] = "priority"
    domain_error_cooldown: int = Field(15, ge=0)
    rate_per_sec: float = Field(1.0, gt=0)
    concurrency: int = Field(2, ge=1)
    play_concurrency: int = Field(2, ge=1)
    solver: bool = True  # 域名被拦时是否当场调用解题服务（没过才冷却、换下一个域名）
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
        description="各站点的启用、镜像域名与选择策略、限速、后台抓取和播放页面并发；域名策略不分摊 CDN 视频流量",
    )
    site_priority: list[str] = Field(
        default_factory=lambda: list(SITES),
        description="同一部影片有多个源时，站点的优先顺序（播放择优、元数据取用都按它）",
    )
    # 抓取
    proxy: str = Field("", description="上游代理，页面解析与原样中转都会经过该出口，如 http://host:port、socks5h://user:pass@host:port")
    impersonate: str = Field("chrome", description="curl_cffi 模拟的浏览器指纹")
    request_timeout: int = Field(30, ge=5, le=300, description="单次请求超时（秒）")
    domain_cooldown: int = Field(300, ge=0, description="域名被拦后的首次冷却（秒），连续被拦翻倍；0 不冷却")
    domain_cooldown_max: int = Field(3600, ge=0, description="域名拦截冷却上限（秒），0 不限制上限")
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
    trash_days: int = Field(
        7, ge=0, le=365,
        description="外部整理库回收区保留天数：影片移出外部整理库（归并、规则、手动移出）时删掉 strm，它独占的影片目录"
                    "（外部工具生成的 nfo、图片）移进整理目录下的 .hls2strm-trash，放满这么多天后自动删除；0 不进回收区、直接删",
    )
    orphan_cleanup: bool = Field(
        True,
        description="每天清理一次外部整理库的残留：整理目录里已经没有 strm、只剩 nfo 和图片的影片目录移进回收区"
                    "（一天内有改动的不动，可能正在整理）",
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
        description="redirect：可以直连时 302，否则原样中转；proxy：全部原样中转；direct：strm 写 CDN 地址，仅调试。本服务不转码",
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
    quality_first: bool = Field(
        True, description="挑源时画质优先：先比画质（源里最清楚的一档），再看站点优先顺序；关掉则站点优先，画质只用来分先后"
    )
    quality_max: int = Field(0, ge=0, le=4320, description="选源画质偏好上限：超过的源排到后面，不限制实际播放档位、不转码；0 不限")
    quality_unknown: int = Field(
        720, ge=0, le=4320, description="还不知道画质的源按多少算（分辨率的高）；Jable 实测多为 720"
    )
    prefer_direct: bool = Field(
        False,
        description="直连优先：能 302 的源排在要本服务中转的前面（省本服务带宽），画质其次；"
                    "关掉则画质优先，更清楚的源要中转也用它",
    )
    variant_mode: Literal["highest", "all"] = Field(
        "highest",
        description="多码率的源（MissAV、VidHide 等的主播放列表里有好几档）给播放器哪些档：highest 只给最高一档"
                    "（画质优先；网络差时播放器没法自己降档）；all 整个交给播放器按网速自适应",
    )
    version_min_height: int = Field(
        480, ge=0, le=4320,
        description="多画质版本（输出库里开了才写）：低于这一档的不单独写版本文件；一部片至少有两档才写",
    )
    quality_capture: bool = Field(
        True,
        description="顺手探测画质：抓详情、播放拿到播放地址时读一次播放列表认出分辨率；只请求 CDN，不多访问源站",
    )
    quality_concurrency: int = Field(2, ge=1, description="后台画质探测并发，不进行转码")
    quality_pending: int = Field(32, ge=0, description="后台顺手探测画质的任务额度，0 不限制；达到额度时仍可稍后通过显式画质探测任务补全")
    health_rank: bool = Field(
        True,
        description="按播放连通性挑源：本服务测到不通、不稳、慢的播放站排到后面，同一档里再比画质、站点优先顺序。"
                    "测的是本服务到 CDN 的网络，302 给外网客户端时只能参考",
    )
    health_interval: int = Field(
        60, ge=0, le=1440,
        description="定时检测播放连通性的间隔（分钟），0 关闭。每轮每个播放站抽几部最近播过的片，"
                    "有没过期的现成地址就不访问源站，下载一个分片的开头一段测速",
    )
    health_samples: int = Field(1, ge=1, le=5, description="每轮每个播放站抽几部片检测（每部最多访问一次源站）")
    health_bytes: int = Field(512, ge=64, le=8192, description="每次检测下载多少（KB）来测速")
    health_slow_kbps: int = Field(1500, ge=0, le=100000, description="检测速度低于它（kbps）算慢；0 不按速度分档")
    play_remote: bool = Field(True, description="普通播放按外部客户端处理：受出口限制的直链自动原样中转；关闭仅适合同出口播放器。网关始终按外部客户端处理")
    play_connections: int = Field(32, ge=1, description="播放解析连接额度，与后台任务独立；不限制用户数，保存后新请求生效")
    background_connections: int = Field(32, ge=1, description="后台抓取连接额度")
    hls_connections: int = Field(64, ge=1, description="HLS 分片、子清单和密钥中转连接额度；每个请求结束后释放")
    file_connections: int = Field(64, ge=1, description="MP4 等文件原样中转连接额度，与 HLS 独立；传输结束后释放")
    preflight_connections: int = Field(16, ge=1, description="起播预检连接额度，与视频传输独立")
    speed_connections: int = Field(16, ge=1, description="影片线路测速的独立连接额度；同时覆盖线路解析、清单与媒体采样")
    pool_wait_timeout: float = Field(30, ge=0, description="连接额度排队时限（秒），0 不单独限制；仍受请求总时限约束，拥塞不记为源故障")
    relay_buffer_kb: int = Field(512, ge=64, description="每条媒体连接的缓冲上限（KiB），满时暂停上游；不限制用户下载速度")
    playlist_cache_entries: int = Field(128, ge=0, description="播放清单缓存条数，0 关闭缓存；并发请求仍合并")
    playlist_cache_seconds: float = Field(15, ge=0, description="播放清单缓存有效期（秒），0 不保留结果")
    preflight_bytes: int = Field(32, ge=1, description="起播媒体预检采样大小（KiB）")
    preflight_cache_entries: int = Field(512, ge=0, description="起播预检缓存条数，0 关闭缓存；同一地址的并发预检合并")
    preflight_cache_seconds: float = Field(30, ge=0, description="成功预检缓存有效期（秒）")
    preflight_failure_cooldown: float = Field(300, ge=0, description="媒体预检失败候选的冷却（秒），0 不启用；本服务排队拥塞不进入冷却")
    source_failure_cooldown: int = Field(300, ge=0, description="源或线路连续失败的首次排序冷却（秒），连续失败翻倍；0 关闭，失败记录仍保留")
    source_failure_cooldown_max: int = Field(21600, ge=0, description="源或线路排序冷却上限（秒），0 不限制上限")
    play_session_entries: int = Field(2048, ge=0, description="播放会话内存缓存条数，0 不限制；被移出内存的有效会话仍可从数据库恢复")
    play_session_hours: float = Field(8, gt=0, description="播放会话最低有效期（小时），实际至少覆盖影片时长再加一小时")
    speed_test_bytes: int = Field(2048, ge=1, description="单线路测速媒体采样上限（KiB）；只读取有限媒体数据，不转码")
    speed_test_timeout: float = Field(20, gt=0, description="每条线路测速的总时限（秒），包含解析、清单与下载；各线路并行执行")
    resolve_mode: Literal["auto", "redirect", "strict_redirect", "proxy"] = Field(
        "auto",
        description="网关 resolve 默认给什么地址（网关请求里带 mode 时以它为准）：auto 按上面的偏好挑源，"
                    "能直连给 CDN 地址、要中转给中转地址；redirect 优先直连、失败可回退；strict_redirect 只直连且禁止中转回退；proxy 一律原样中转",
    )
    resolve_relay_ttl: int = Field(21600, ge=0, description="网关中转地址缓存时长（秒），0 不缓存；绑定会话的地址还会按会话有效期缩短")
    resolve_timeout: int = Field(20, ge=5, le=120, description="播放解析与首份清单返回的总时限（秒），包含找源、排队和 CDN 请求；媒体传输另按停滞超时判断")
    resolve_attempt_timeout: int = Field(6, ge=1, le=120, description="每个源或线路单次尝试最多等待几秒；仍受播放总时限约束，给备用源保留机会")
    play_discover: bool = Field(
        True, description="现场找源：影片已知的源都不能用（或库里没有这部影片）时，按番号到其他启用的站点找一次"
    )
    resolve_proxy_url: str = Field(
        "",
        description="网关 resolve 用的公网中转地址：挑中的源要中转（如 MissAV）、或直连的源都失败时，返回这个地址下的中转链接，"
                    "须能从外网访问（本服务的公网地址，或网关转发到本服务的地址）；留空则只给能直连的源，没有时返回 409",
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy_play_mode(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for key in ("play_mode", "resolve_mode"):
                if data.get(key) == "continuous":
                    data[key] = "proxy"
        return data

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
