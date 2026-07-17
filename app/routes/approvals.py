"""Decisione di approvazione (M2). Il timeout gia' nega nel broker (regola 4.6)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..approvals import get_broker
from ..db import get_db

router = APIRouter(prefix="/approvals")


class Decision(BaseModel):
    allow: bool
    reason: str = ""
    updated_input: dict | None = None


@router.get("/{approval_id}/state")
async def approval_state(approval_id: str):
    row = get_db().query_one("SELECT * FROM approval WHERE id=?", (approval_id,))
    if not row:
        raise HTTPException(status_code=404, detail="approval inesistente")
    return dict(row)


@router.post("/{approval_id}/decide")
async def decide(approval_id: str, body: Decision):
    ok = get_broker().resolve(approval_id, body.allow, body.reason,
                              body.updated_input)
    if not ok:
        # gia' risolta o scaduta (il timeout ha gia' negato)
        row = get_db().query_one("SELECT status FROM approval WHERE id=?",
                                 (approval_id,))
        status = row["status"] if row else "unknown"
        raise HTTPException(status_code=409,
                            detail=f"approvazione non piu' in attesa (stato: {status})")
    return {"status": "allowed" if body.allow else "denied"}
