"""启动配置（环境变量）与运行时设置（存数据库，可在 Web 页面修改）。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
        env = os.environ.get
        port = int(env("JABLE_PORT", "8080"))
        return cls(
            data_dir=Path(env("JABLE_DATA_DIR", "data")).resolve(),
            host=env("JABLE_HOST", "0.0.0.0"),
            port=port,
            ui_user=env("JABLE_UI_USER", "admin"),
            ui_password=env("JABLE_UI_PASSWORD", ""),
            log_level=env("JABLE_LOG_LEVEL", "INFO").upper(),
            default_output_dir=env("JABLE_OUTPUT_DIR", ""),
            default_public_base_url=env("JABLE_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}"),
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


class Settings(BaseModel):
    """运行时设置。字段说明会显示在 Web 设置页。"""

    # 抓取
    domains: list[str] = Field(
        default_factory=lambda: ["https://fs1.app", "https://jable.tv"],
        description="站点域名，按顺序优先使用；被拦截的域名进入冷却并切换到下一个",
    )
    proxy: str = Field("", description="抓取代理，如 http://host:port、socks5h://user:pass@host:port")
    impersonate: str = Field("chrome", description="curl_cffi 模拟的浏览器指纹")
    rate_per_sec: float = Field(1.0, gt=0, le=20, description="站点请求速率上限（次/秒）")
    concurrency: int = Field(2, ge=1, le=16, description="并发 worker 数")
    request_timeout: int = Field(30, ge=5, le=300, description="单次请求超时（秒）")
    domain_cooldown: int = Field(300, ge=10, le=86400, description="域名被拦后的首次冷却（秒），连续被拦翻倍，上限 1 小时")
    solver_url: str = Field("", description="Byparr / FlareSolverr 地址，如 http://byparr:8191；留空不启用")
    solver_timeout: int = Field(60, ge=10, le=300, description="解题超时（秒）")
    # 重试
    max_attempts: int = Field(5, ge=1, le=20, description="单个任务最多尝试次数")
    retry_base_delay: int = Field(30, ge=1, le=3600, description="首次重试等待（秒），之后每次 ×4，上限 2 小时")
    # 任务
    fetch_detail: bool = Field(True, description="新建列表任务和订阅时，默认是否抓详情页（女优、标签、上市日期、封面）")
    # 输出
    output_dir: str = Field(
        "", description="输出根目录，各输出库的相对目录都在它下面；留空使用 JABLE_OUTPUT_DIR 或 数据目录/strm"
    )
    path_template: str = Field(
        "{slug}/{slug}",
        description="默认路径模板（不含扩展名，输出库可单独覆盖），必须包含 {slug}；可用变量 {slug} {code} {actor} {year}",
    )
    write_nfo: bool = Field(True, description="写 nfo（Kodi 格式，Emby/Jellyfin 通用）")
    download_cover: bool = Field(True, description="下载封面 fanart")
    poster_crop: bool = Field(True, description="从封面裁出竖版 poster")
    # 播放
    public_base_url: str = Field("", description="Emby/Jellyfin 访问本服务的地址，写进 strm；留空使用 JABLE_PUBLIC_BASE_URL")
    play_mode: Literal["redirect", "proxy", "direct"] = Field(
        "redirect",
        description="redirect：302 到 CDN；proxy：本服务中转视频流；direct：strm 直接写 CDN 地址（约 3 小时失效，仅调试）",
    )
    proxy_user_agents: list[str] = Field(
        default_factory=lambda: ["Lavf", "python-requests"],
        description="redirect 模式下，User-Agent 含这些片段的客户端改走本服务中转（CDN 会拒绝它们，如 ffmpeg 默认的 Lavf）",
    )
    play_token: str = Field("", description="播放地址访问令牌；设置后 strm 地址带 ?t=令牌")
    hls_margin: int = Field(15, ge=0, le=120, description="302 前要求播放地址剩余有效期 ≥ 影片时长 + 该值（分钟）")

    @field_validator("domains")
    @classmethod
    def _check_domains(cls, v: list[str]) -> list[str]:
        out = []
        for d in v:
            d = d.strip().rstrip("/")
            if not d:
                continue
            if not d.startswith(("http://", "https://")):
                d = "https://" + d
            out.append(d)
        if not out:
            raise ValueError("至少需要一个域名")
        return out

    @field_validator("path_template")
    @classmethod
    def _check_template(cls, v: str) -> str:
        return check_path_template(v)

    @field_validator("public_base_url", "solver_url", "proxy")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().rstrip("/")


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
            data = json.loads(raw)
            known = {k: v for k, v in data.items() if k in Settings.model_fields}
            self.current = Settings.model_validate(known)

    async def update(self, patch: dict) -> Settings:
        new = Settings.model_validate({**self.current.model_dump(), **patch})
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
