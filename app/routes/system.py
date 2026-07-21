"""Comandi di sistema (§ sistema): riavvio/spegnimento dell'app, spegnimento del
PC, reset totale (pulizia DB + riavvio dei servizi).

Ogni azione DISTRUTTIVA richiede confirm=true (come il purge, §B.3). Le azioni a
livello di processo/OS (riavvia app, spegni app, spegni PC) sono inoltre dietro
l'interruttore settings.system_controls_enabled (ARGO_SYSTEM_CONTROLS). Ognuna
emette un evento SSE PRIMA di agire (§B.6.5: nulla di silenzioso), cosi' il
telefono sa cosa succede anche se poi la connessione cade.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..db import get_db
from ..events import get_bus
from ..preflight import check_backends
from ..system import (
    factory_reset, get_effects, poweroff_pc, restart_app, restart_services,
    shutdown_app,
)

router = APIRouter(prefix="/system")


@router.get("/health")
async def system_health():
    """Preflight dei backend: la Claude Code CLI sull'abbonamento e la STESSA CLI
    puntata a Ollama locale (§1.2). Sola lettura, non tocca nulla. Mostra anche
    lo stato dell'auto-approvazione (ARGO_AUTO_APPROVE)."""
    return await check_backends(get_settings())


class Confirm(BaseModel):
    confirm: bool = False


class ResetBody(BaseModel):
    confirm: bool = False
    keep_push: bool = True         # non perdere l'iscrizione push del telefono
    keep_projects: bool = True     # non perdere i progetti (config utente)


def _require_confirm(confirm: bool) -> None:
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="operazione irreversibile: richiede confirm=true")


def _require_enabled() -> None:
    if not get_settings().system_controls_enabled:
        raise HTTPException(
            status_code=403,
            detail="comandi di sistema disattivati (ARGO_SYSTEM_CONTROLS=0)")


# =============================================================================
# app: riavvio / spegnimento (a livello di processo)
# =============================================================================
@router.post("/app/restart")
async def app_restart(body: Confirm):
    _require_confirm(body.confirm)
    _require_enabled()
    settings = get_settings()
    await get_bus().emit(None, "system_app_restart",
                         {"delay_s": settings.system_action_delay_s})
    restart_app(settings, get_effects())
    return {"status": "restarting", "delay_s": settings.system_action_delay_s}


@router.post("/app/shutdown")
async def app_shutdown(body: Confirm):
    _require_confirm(body.confirm)
    _require_enabled()
    settings = get_settings()
    await get_bus().emit(None, "system_app_shutdown",
                         {"delay_s": settings.system_action_delay_s})
    shutdown_app(settings, get_effects())
    return {"status": "shutting_down", "delay_s": settings.system_action_delay_s}


# =============================================================================
# PC: spegnimento (a livello di OS)
# =============================================================================
@router.post("/pc/shutdown")
async def pc_shutdown(body: Confirm):
    _require_confirm(body.confirm)
    _require_enabled()
    settings = get_settings()
    await get_bus().emit(None, "system_pc_shutdown",
                         {"delay_s": settings.system_action_delay_s})
    poweroff_pc(settings, get_effects())
    return {"status": "powering_off", "delay_s": settings.system_action_delay_s}


# =============================================================================
# servizi in-process: riavvio a caldo + reset totale (pulizia DB + riavvio)
# =============================================================================
@router.post("/services/restart")
async def services_restart(body: Confirm):
    _require_confirm(body.confirm)
    result = await restart_services()
    await get_bus().emit(None, "system_services_restarted", result)
    return {"status": "services_restarted", **result}


@router.post("/reset")
async def system_reset(body: ResetBody):
    """Pulizia totale del DB + riavvio totale dei servizi. Non spegne il processo
    ne' il PC: e' un 'factory reset' a caldo."""
    _require_confirm(body.confirm)
    result = await factory_reset(get_db(), get_settings(),
                                 keep_push=body.keep_push,
                                 keep_projects=body.keep_projects)
    await get_bus().emit(None, "system_reset", result)
    return {"status": "reset_done", **result}
