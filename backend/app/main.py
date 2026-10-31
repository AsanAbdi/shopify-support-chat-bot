import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import select, text

from app.api import admin, chat
from app.config import get_settings
from app.db import dispose_engine, session_factory
from app.deps import build_container
from app.logging_conf import setup_logging
from app.models import User
from app.observability import init_sentry
from app.security import hash_password

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


async def seed_admin() -> None:
    s = get_settings()
    async with session_factory()() as session:
        exists = (await session.execute(select(User.id).where(User.username == s.admin_username))).first()
        if not exists:
            session.add(User(username=s.admin_username, password_hash=hash_password(s.admin_password.get_secret_value()), role="admin"))
            await session.commit()
            log.info("seeded initial admin user")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging()
    init_sentry(s)
    if s.app_env == "prod" and (s.jwt_secret.get_secret_value() == "change-me-in-env" or s.admin_password.get_secret_value() == "admin12345"):
        raise RuntimeError("Refusing to start in prod with default JWT_SECRET/ADMIN_PASSWORD")
    mocks = "mock-services" in s.shopify_base_url or "mock-services" in s.freshdesk_base_url
    if s.app_env == "prod" and mocks and not s.allow_mock_integrations:
        raise RuntimeError("Refusing to start in prod with Shopify/Freshdesk pointing at mock-services (ALLOW_MOCK_INTEGRATIONS=true for a demo)")
    if s.app_env == "prod" and mocks:
        log.warning("production mode with MOCK Shopify/Freshdesk: demo deployment, no real orders or tickets")
    app.state.container = await build_container(s)
    await seed_admin()
    yield
    await app.state.container.aclose()
    await dispose_engine()


class RevalidatedStaticFiles(StaticFiles):
    """The admin is one small file that changes with every release: let the browser revalidate it (ETag) instead of
    keeping an old copy that no longer matches the API (that produced a broken page after an update)."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Shop Support Assistant", version="0.1.0", lifespan=lifespan,
                  docs_url="/docs" if s.app_env != "prod" else None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"], allow_credentials=False,
    )
    app.include_router(chat.router)
    app.include_router(admin.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request):
        try:
            async with session_factory()() as session:
                await session.execute(text("SELECT 1"))
            await request.app.state.container.redis.ping()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.get("/widget.js", include_in_schema=False)
    async def widget_js() -> FileResponse:
        return FileResponse(STATIC / "widget.js", media_type="application/javascript", headers={"Cache-Control": "public, max-age=300" if get_settings().app_env == "prod" else "no-cache"})

    @app.get("/demo", include_in_schema=False)
    async def demo() -> FileResponse:
        return FileResponse(STATIC / "demo.html")

    app.mount("/admin", RevalidatedStaticFiles(directory=STATIC / "admin", html=True), name="admin")
    Instrumentator(excluded_handlers=["/healthz", "/readyz", "/metrics"]).instrument(app).expose(app, include_in_schema=False)
    return app


app = create_app()
