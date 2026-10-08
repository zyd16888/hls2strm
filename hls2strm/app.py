"""应用装配：创建各组件，挂载路由，管理生命周期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api, play
from .config import BootConfig, SettingsStore
from .db import Database
from .engine import Engine
from .fetcher import Fetcher
from .observability import Metrics, ring, setup_logging
from .play import Resolver
from .writer import OutputWriter

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class NoCacheStaticFiles(StaticFiles):
    """前端文件每次都按 ETag 重新校验，升级后不会用到旧的 app.js。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@dataclass
class Context:
    boot: BootConfig
    db: Database
    store: SettingsStore
    metrics: Metrics
    fetcher: Fetcher
    writer: OutputWriter
    resolver: Resolver
    engine: Engine


def database_path(data_dir: Path) -> Path:
    """数据库文件：新装用 hls2strm.db；改名前的数据目录里已经有 jable.db 的接着用它（不改名，换回旧镜像也认得）。"""
    old, new = data_dir / "jable.db", data_dir / "hls2strm.db"
    return old if old.exists() and not new.exists() else new


def create_app(boot: BootConfig | None = None) -> FastAPI:
    boot = boot or BootConfig.from_env()
    boot.data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(boot.data_dir, boot.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ring.bind_loop(asyncio.get_running_loop())
        db = Database(database_path(boot.data_dir))
        await db.open()
        store = SettingsStore(boot, db)
        await store.load()
        metrics = Metrics()
        fetcher = Fetcher(store, metrics)
        writer = OutputWriter(store)
        resolver = Resolver(db, fetcher, store, metrics)
        engine = Engine(db, fetcher, writer, store, metrics, boot)
        app.state.ctx = Context(boot, db, store, metrics, fetcher, writer, resolver, engine)
        log.info("启动：数据目录 %s，输出目录 %s，对外地址 %s", boot.data_dir, store.output_dir, store.public_base_url)
        if not boot.ui_password:
            log.warning("未设置 HLS2STRM_UI_PASSWORD，Web 控制台没有登录保护")
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()
            await fetcher.close()
            await db.close()
            log.info("已退出")

    app = FastAPI(title="hls2strm", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(play.router)
    app.include_router(api.router)
    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", dependencies=[Depends(api.require_auth)], include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    return app
