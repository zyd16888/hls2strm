"""应用装配：创建各组件，挂载路由，管理生命周期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import api, play
from .auth import Auth, load_secret
from .config import BootConfig, SettingsStore
from .db import Database
from .engine import Engine
from .fetcher import Fetcher
from .observability import Metrics, ring, setup_logging
from .play import Resolver
from .writer import OutputWriter

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class FrontendFiles(StaticFiles):
    """前端构建产物（web/ 构建到 static/）：assets/ 下的文件名带内容哈希，长期缓存；其他文件每次按 ETag 重新校验。"""

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        hashed = path.replace("\\", "/").startswith("assets/")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if hashed else "no-cache"
        return resp


@dataclass
class Context:
    boot: BootConfig
    auth: Auth
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
        engine = Engine(db, fetcher, writer, store, metrics, boot)
        resolver = engine.resolver
        auth = Auth(boot, await load_secret(db))
        app.state.ctx = Context(boot, auth, db, store, metrics, fetcher, writer, resolver, engine)
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
    app.include_router(api.public)
    app.include_router(api.router)
    app.mount("/static", FrontendFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        """页面本身不用登录（没有数据）；接口要登录，没登录时页面显示登录页。"""
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return PlainTextResponse("前端还没构建：在 web 目录执行 npm ci && npm run build（Docker 镜像里已经构建好）",
                                     status_code=503)
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    return app
