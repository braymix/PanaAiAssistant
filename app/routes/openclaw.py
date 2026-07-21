"""Route degli agenti PC.

Due livelli:
  * GENERICHE `/agents/*` — parametrizzate sul nome agente (§C.4). Iterano il
    registry: aggiungere un agente = implementare `PcAgent` + `register()`, e le
    route lo servono senza modifiche.
  * ALIAS `/openclaw/*` — compatibilita' con la missione OpenClaw (§B.5). Le route
    generiche sono alias/redirect verso `/agents/openclaw/*`; le route specifiche
    di OpenClaw (setup, config, config/sync) restano qui.

⚠️ Nessun agente PC passa dal PolicyGate (§B.3): nessun `can_use_tool` qui.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from ..config import get_settings
from ..events import get_bus
from ..pc_agents import registry

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class TaskBody(BaseModel):
    prompt: str


def _agent_or_404(name: str):
    agent = registry.get(name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agente {name!r} non trovato")
    return agent


# =============================================================================
# Route GENERICHE /agents/* (§C.4)
# =============================================================================
@router.get("/agents")
async def list_agents():
    """Tutti gli agenti registrati + stato di base (running)."""
    out = []
    for a in registry.all_agents():
        try:
            running = a.is_running()
        except Exception:  # noqa: BLE001 — uno stato illeggibile non fa 500
            running = False
        out.append({"name": a.name, "icon": a.icon,
                    "description": a.description, "running": running})
    return {"agents": out}


@router.get("/agents/{name}/status")
async def agent_status(name: str):
    return await _agent_or_404(name).check_status()


@router.post("/agents/{name}/start")
async def agent_start(name: str):
    return {"status": "started", "ok": await _agent_or_404(name).start()}


@router.post("/agents/{name}/stop")
async def agent_stop(name: str):
    return {"status": "stopped", "ok": await _agent_or_404(name).stop()}


@router.post("/agents/{name}/restart")
async def agent_restart(name: str):
    return {"status": "restarted", "ok": await _agent_or_404(name).restart()}


@router.get("/agents/{name}/logs")
async def agent_logs(name: str, n: int = 100):
    return {"lines": _agent_or_404(name).recent_logs(n)}


@router.get("/agents/{name}/logs/stream")
async def agent_logs_stream(name: str):
    agent = _agent_or_404(name)
    kind = f"{agent.name}_log"

    async def gen():
        # prima la coda recente dal ring buffer
        for line in agent.recent_logs(100):
            yield f"event: logline\ndata: {line}\n\n"
        # poi il live: eventi {name}_log dal bus globale
        bus = get_bus()
        sub = await bus.subscribe("*")
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if msg.get("kind") != kind:
                    continue
                payload = msg.get("payload") or {}
                line = payload.get("line", "") if isinstance(payload, dict) else str(payload)
                yield f"event: logline\ndata: {line}\n\n"
        finally:
            await bus.unsubscribe("*", sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@router.post("/agents/{name}/task")
async def agent_task(name: str, body: TaskBody):
    task_id = await _agent_or_404(name).send_task(body.prompt)
    return {"status": "sent", "task_id": task_id}


# =============================================================================
# ALIAS /openclaw/* -> /agents/openclaw/* (§B.5, §C.4)
# =============================================================================
_OC = "openclaw"


@router.get("/openclaw/status")
async def openclaw_status():
    return RedirectResponse(f"/agents/{_OC}/status", status_code=307)


@router.post("/openclaw/start")
async def openclaw_start():
    return RedirectResponse(f"/agents/{_OC}/start", status_code=307)


@router.post("/openclaw/stop")
async def openclaw_stop():
    return RedirectResponse(f"/agents/{_OC}/stop", status_code=307)


@router.post("/openclaw/restart")
async def openclaw_restart():
    return RedirectResponse(f"/agents/{_OC}/restart", status_code=307)


@router.get("/openclaw/logs")
async def openclaw_logs(n: int = 100):
    return RedirectResponse(f"/agents/{_OC}/logs?n={n}", status_code=307)


@router.get("/openclaw/logs/stream")
async def openclaw_logs_stream():
    return RedirectResponse(f"/agents/{_OC}/logs/stream", status_code=307)


@router.post("/openclaw/task")
async def openclaw_task():
    return RedirectResponse(f"/agents/{_OC}/task", status_code=307)


# --- route SPECIFICHE di OpenClaw (nessun equivalente generico) --------------
@router.post("/openclaw/setup")
async def openclaw_setup_endpoint():
    """ensure_installed + setup_workspace + generate_config."""
    agent = _agent_or_404(_OC)
    return await agent.setup()


@router.get("/openclaw/config")
async def openclaw_config():
    """config.yaml corrente (read-only, per debug)."""
    agent = _agent_or_404(_OC)
    return {"config": await agent.current_config(),
            "workspace": str(get_settings().openclaw_workspace)}


@router.post("/openclaw/config/sync")
async def openclaw_config_sync():
    """Rigenera la sezione modelli da Ollama."""
    agent = _agent_or_404(_OC)
    return await agent.sync_models()
