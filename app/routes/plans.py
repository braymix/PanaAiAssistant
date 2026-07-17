"""Tasto VIA + approvazione ed esecuzione del piano (M3->M4)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..briefs import PlanValidationError
from ..db import get_db

router = APIRouter(prefix="/plans")


class ViaRequest(BaseModel):
    conversation_id: str
    repo_path: str
    resume_session: str | None = None


@router.post("/via")
async def via(body: ViaRequest):
    """Tasto VIA: genera e valida il PlanDocument (§3.2). Sincrono: serve l'esito
    subito (accettato/rifiutato) per mostrare la schermata di approvazione."""
    from ..planner import generate_plan  # lazy (SDK)
    try:
        plan_id = await generate_plan(
            body.conversation_id, body.repo_path, body.resume_session)
    except PlanValidationError as e:
        raise HTTPException(status_code=422, detail=f"Piano rifiutato: {e}")
    return {"plan_id": plan_id, "next": f"/plans/{plan_id}"}


@router.get("/{plan_id}/status")
async def plan_status(plan_id: str):
    """Stato live per il monitor mobile: task + approvazioni pendenti."""
    db = get_db()
    plan = db.query_one("SELECT * FROM plan_document WHERE id=?", (plan_id,))
    if not plan:
        raise HTTPException(status_code=404, detail="piano inesistente")
    tasks = db.query(
        "SELECT t.id, t.title, t.status, t.attempts, t.backend, "
        "(SELECT r.id FROM run r WHERE r.task_id=t.id "
        " ORDER BY r.started_at DESC, r.id DESC LIMIT 1) AS run_id "
        "FROM task t WHERE t.plan_id=? ORDER BY t.seq", (plan_id,))
    # approvazioni pendenti per i run dei task di questo piano
    pending = db.query(
        "SELECT a.id, a.tool_name, t.title AS task_title FROM approval a "
        "JOIN run r ON a.run_id=r.id JOIN task t ON r.task_id=t.id "
        "WHERE t.plan_id=? AND a.status='pending' ORDER BY a.pushed_at", (plan_id,))
    return {
        "plan_status": plan["status"],
        "tasks": [dict(t) for t in tasks],
        "pending_approvals": [dict(p) for p in pending],
    }


@router.post("/{plan_id}/approve")
async def approve(plan_id: str):
    """Conferma finale (post-VIA): lancia l'executor pool in background (M4)."""
    from ..executor import get_pool
    plan = get_db().query_one("SELECT * FROM plan_document WHERE id=?", (plan_id,))
    if not plan:
        raise HTTPException(status_code=404, detail="piano inesistente")

    async def _run():
        try:
            await get_pool().approve_and_run(plan_id)
        except Exception as e:  # noqa: BLE001 — mostrare i fallimenti (4.8)
            from ..events import get_bus
            await get_bus().emit(None, "error", {"detail": f"executor: {e}"})

    asyncio.ensure_future(_run())
    return {"status": "executing", "plan_id": plan_id}
