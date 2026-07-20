"""Ciclo di vita (§B.4): blocca/annulla/pausa + elimina (soft) / purga (hard).

Ogni endpoint mutante emette un evento (cancel/pause/block/delete/purge) cosi' il
telefono resta coerente via SSE (§B.6.5: nulla di silenzioso).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import get_db
from ..events import get_bus
from ..executor import get_pool
from ..lifecycle import (
    LifecycleConflict, purge_conversation, purge_plan, soft_delete_conversation,
    soft_delete_plan, soft_delete_task,
)

router = APIRouter()


# =============================================================================
# annullamento (§B.2)
# =============================================================================
@router.post("/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM plan_document WHERE id=?", (plan_id,)):
        raise HTTPException(status_code=404, detail="piano inesistente")
    await get_pool().cancel_plan(plan_id)
    return {"status": "cancelled", "plan_id": plan_id}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM task WHERE id=?", (task_id,)):
        raise HTTPException(status_code=404, detail="task inesistente")
    acted = await get_pool().cancel_task(task_id)
    return {"status": "cancelled" if acted else "noop", "task_id": task_id}


# =============================================================================
# pausa/ripresa coda (§B.2)
# =============================================================================
@router.post("/queue/pause")
async def pause_queue():
    await get_pool().pause_queue()
    return {"paused": True}


@router.post("/queue/resume")
async def resume_queue():
    await get_pool().resume_queue()
    return {"paused": False}


@router.get("/queue")
async def queue_state():
    pool = get_pool()
    return {"paused": pool.scheduler.paused,
            "queue_depth": pool.queue_depth,
            "vram_reserved_mb": pool.scheduler.reserved_mb,
            "vram_budget_mb": pool.scheduler.budget_mb}


# =============================================================================
# veto pre-esecuzione: block/unblock (§B.1)
# =============================================================================
@router.post("/tasks/{task_id}/block")
async def block_task(task_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM task WHERE id=?", (task_id,)):
        raise HTTPException(status_code=404, detail="task inesistente")
    acted = await get_pool().block_task(task_id)
    if not acted:
        raise HTTPException(status_code=409,
                            detail="task in esecuzione o gia' concluso: annulla invece")
    return {"status": "blocked", "task_id": task_id}


@router.post("/tasks/{task_id}/unblock")
async def unblock_task(task_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM task WHERE id=?", (task_id,)):
        raise HTTPException(status_code=404, detail="task inesistente")
    acted = await get_pool().unblock_task(task_id)
    if not acted:
        raise HTTPException(status_code=409, detail="task non e' 'blocked'")
    return {"status": "pending", "task_id": task_id}


# =============================================================================
# eliminazione soft (default) (§B.3)
# =============================================================================
@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM conversation WHERE id=?", (conversation_id,)):
        raise HTTPException(status_code=404, detail="conversazione inesistente")
    try:
        soft_delete_conversation(db, conversation_id)
    except LifecycleConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    await get_bus().emit(None, "conversation_deleted",
                         {"conversation_id": conversation_id})
    return {"status": "deleted", "conversation_id": conversation_id}


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM plan_document WHERE id=?", (plan_id,)):
        raise HTTPException(status_code=404, detail="piano inesistente")
    try:
        soft_delete_plan(db, plan_id)
    except LifecycleConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    await get_bus().emit(None, "plan_deleted", {"plan_id": plan_id})
    return {"status": "deleted", "plan_id": plan_id}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    db = get_db()
    if not db.query_one("SELECT id FROM task WHERE id=?", (task_id,)):
        raise HTTPException(status_code=404, detail="task inesistente")
    try:
        soft_delete_task(db, task_id)
    except LifecycleConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    await get_bus().emit(None, "task_deleted", {"task_id": task_id})
    return {"status": "deleted", "task_id": task_id}


# =============================================================================
# purge hard (esplicito, irreversibile, doppia conferma) (§B.3)
# =============================================================================
class PurgeConfirm(BaseModel):
    confirm: bool = False


@router.post("/conversations/{conversation_id}/purge")
async def purge_conversation_route(conversation_id: str, body: PurgeConfirm):
    if not body.confirm:
        raise HTTPException(status_code=400,
                            detail="purge irreversibile: richiede confirm=true")
    db = get_db()
    if not db.query_one("SELECT id FROM conversation WHERE id=?", (conversation_id,)):
        raise HTTPException(status_code=404, detail="conversazione inesistente")
    try:
        purge_conversation(db, conversation_id)
    except LifecycleConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    await get_bus().emit(None, "conversation_purged",
                         {"conversation_id": conversation_id})
    return {"status": "purged", "conversation_id": conversation_id}


@router.post("/plans/{plan_id}/purge")
async def purge_plan_route(plan_id: str, body: PurgeConfirm):
    if not body.confirm:
        raise HTTPException(status_code=400,
                            detail="purge irreversibile: richiede confirm=true")
    db = get_db()
    if not db.query_one("SELECT id FROM plan_document WHERE id=?", (plan_id,)):
        raise HTTPException(status_code=404, detail="piano inesistente")
    try:
        purge_plan(db, plan_id)
    except LifecycleConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    await get_bus().emit(None, "plan_purged", {"plan_id": plan_id})
    return {"status": "purged", "plan_id": plan_id}
