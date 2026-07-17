"""Argo — FastAPI, un solo processo, bind 127.0.0.1 (regola 4.2)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .db import init_db
from .security import IdentityMiddleware
from .routes import chat, plans, runs, approvals as approvals_route, push as push_route, stats as stats_route, ui, projects as projects_route

HERE = Path(__file__).parent
STATIC = HERE / "static"
TEMPLATES = HERE / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Argo", lifespan=lifespan)
    app.add_middleware(IdentityMiddleware)

    app.include_router(ui.router)
    app.include_router(projects_route.router)
    app.include_router(chat.router)
    app.include_router(plans.router)
    app.include_router(runs.router)
    app.include_router(approvals_route.router)
    app.include_router(push_route.router)
    app.include_router(stats_route.router)

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # asset PWA serviti a scope root (devono essere pubblici: prefissi in security.py)
    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()


def main() -> None:
    import uvicorn
    settings = get_settings()
    # regola 4.2: SEMPRE loopback, mai 0.0.0.0.
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
