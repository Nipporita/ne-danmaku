"""Standalone FastAPI application that only exposes danmaku services."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .config import AppConfig, load_config


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create a FastAPI instance configured for danmaku services only."""
    if config is None:
        config = load_config()

    app = FastAPI(
        title="Nekocast Danmaku API",
        description="Standalone danmaku gateway extracted from Nekocast",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.config = config

    register_routers(app, config)
    register_event_handlers(app, config)
    setup_static_files(app)

    logger.info("Danmaku-only FastAPI application is ready")
    return app


def setup_static_files(app: FastAPI) -> None:
    """Optionally serve static assets if present."""
    backend_root = Path(__file__).resolve().parents[1]

    public_dir = backend_root / "public"
    if public_dir.exists():
        logger.info("Mounting public assets from {}", public_dir)
        app.mount("/public", StaticFiles(directory=public_dir), name="public")

    frontend_dist = backend_root / "frontend" / "dist"
    if frontend_dist.exists():
        logger.info("Mounting frontend dist at {}", frontend_dist)
        assets_dir = frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str, request: Request):
            if request.url.path.startswith("/api"):
                raise HTTPException(status_code=404, detail="API endpoint not found")
            return FileResponse(frontend_dist / "index.html")


def register_routers(app: FastAPI, config: AppConfig) -> None:
    from .danmaku.routes import create_router as create_danmaku_router

    danmaku_router = create_danmaku_router(config.danmaku)
    app.include_router(danmaku_router, prefix="/api/danmaku/v1", tags=["danmaku"])
    logger.info("Registered danmaku routes at /api/danmaku/v1")


def register_event_handlers(app: FastAPI, config: AppConfig) -> None:
    @app.on_event("startup")
    async def startup_event():
        logger.info("=" * 60)
        logger.info("Starting danmaku service at http://{}:{}", config.host, config.port)
        await startup_danmaku(app, config)
        logger.info("Danmaku service ready")
        logger.info("=" * 60)

    @app.on_event("shutdown")
    async def shutdown_event():
        logger.info("Shutting down danmaku service...")
        await shutdown_danmaku(app)
        logger.info("Danmaku service stopped")


async def startup_danmaku(app: FastAPI, config: AppConfig) -> None:
    from .danmaku.models import ConnectionManager, DanmakuFilter

    danmaku_filter = DanmakuFilter(blacklists=config.danmaku.blacklists)
    connection_manager = ConnectionManager(danmaku_filter=danmaku_filter)
    app.state.danmaku_manager = connection_manager

    if config.danmaku.satori:
        from .danmaku.satori_client import start_satori_client

        await start_satori_client(config.danmaku.satori, connection_manager)
        logger.info("Satori client started")

    if config.danmaku.bilibili:
        from .danmaku.bilibili_client import start_bilibili_client

        await start_bilibili_client(config.danmaku.bilibili, connection_manager)
        logger.info("Bilibili client started")


async def shutdown_danmaku(app: FastAPI) -> None:
    if hasattr(app.state, "danmaku_manager"):
        await app.state.danmaku_manager.disconnect_all()
        logger.info("All danmaku connections closed")
