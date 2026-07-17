"""Registrazione push VAPID (M2)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request

from ..config import get_settings
from ..push import save_subscription

router = APIRouter(prefix="/push")


@router.get("/vapid-public-key")
async def vapid_public_key():
    p: Path = get_settings().vapid_keys_path
    if not p.exists():
        return {"publicKey": None,
                "detail": "vapid_keys.json assente: esegui gen_vapid.py"}
    return {"publicKey": json.loads(p.read_text()).get("public_b64")}


@router.post("/subscribe")
async def subscribe(request: Request):
    sub = await request.json()
    sid = save_subscription(sub)
    return {"id": sid}
