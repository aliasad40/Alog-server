"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..auth.passwords import hash_password
from ..config import Config, get_config
from ..database.clickhouse import ClickHouseService
from ..database.meta import MetadataStore
from ..logging_setup import setup_logging
from ..queue.redis_queue import build_client as build_redis
from . import auth_api, routers_api, search_api, settings_api, system_api

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or get_config()
    setup_logging(cfg, "api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.cfg = cfg
        app.state.meta = MetadataStore(cfg.paths.metadata_db)
        app.state.clickhouse = ClickHouseService(cfg)
        app.state.redis = build_redis(cfg)
        # Guards against username enumeration: a login for an unknown user
        # still pays for one Argon2 verification, so the response time does
        # not reveal which usernames exist.
        app.state.dummy_hash = hash_password("not-a-real-password")
        await routers_api.sync_allow_list_startup(app)
        log.info("api ready on %s:%s", cfg.server.bind, cfg.server.port)

        yield

        app.state.clickhouse.close()
        try:
            await app.state.redis.aclose()
        except Exception:
            pass

    app = FastAPI(
        title="Network Log Server",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_api.router)
    app.include_router(routers_api.router)
    app.include_router(search_api.router)
    app.include_router(settings_api.router)
    app.include_router(system_api.router)

    @app.exception_handler(500)
    async def internal_error(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong on the server. Check the service logs."},
        )

    # In production nginx serves these directly; this keeps `nls-admin serve`
    # and the Docker image usable on their own.
    if FRONTEND_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = None  # populated by uvicorn factory


def get_app() -> FastAPI:
    return create_app()
