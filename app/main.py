"""Argo — FastAPI, un solo processo, bind 127.0.0.1 (regola 4.2)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("argo")

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .db import init_db
from .security import IdentityMiddleware
from .routes import chat, plans, runs, approvals as approvals_route, push as push_route, stats as stats_route, ui, projects as projects_route, ollama as ollama_route

HERE = Path(__file__).parent
STATIC = HERE / "static"
TEMPLATES = HERE / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    _sanity_checks(settings)
    yield


def _sanity_checks(settings) -> None:
    if not settings.repo_roots:
        log.warning("ARGO_ROOTS vuoto: ogni Write/Bash sara' NEGATO (regola 4.3). "
                    "Configura le root su cui gli agenti possono operare.")
    if not settings.vapid_keys_path.exists():
        log.warning("vapid_keys.json assente (%s): niente push al telefono "
                    "(regola 4.16). Esegui gates/gate0_push/gen_vapid.py.",
                    settings.vapid_keys_path)
    if settings.dev_allow_no_identity:
        log.warning("ARGO_DEV_ALLOW_NO_IDENTITY=1: auth d'identita' DISATTIVATA. "
                    "Solo per dev locale; MAI con l'app esposta (regola 4.2).")
    if settings.host != "127.0.0.1":
        log.error("host=%s: DEVE essere 127.0.0.1 (regola 4.2).", settings.host)


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
    app.include_router(ollama_route.router)

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
