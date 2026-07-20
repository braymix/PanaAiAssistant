"""Stats live (M5)."""

from __future__ import annotations

from fastapi import APIRouter

from ..executor import get_pool
from ..stats import snapshot

router = APIRouter(prefix="/stats")


@router.get("")
async def stats():
    pool = get_pool()
    return snapshot(pushes_sent=pool.pushes.get("pushes", 0),
                    ollama_queue=pool.queue_depth,
                    scheduler=pool.scheduler)
