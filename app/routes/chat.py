"""Chat + plan mode (M3)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import get_db, utcnow
from ..ids import new_id

router = APIRouter(prefix="/chat")


class NewConversation(BaseModel):
    title: str = "Nuova conversazione"
    repo_path: str = ""
    plan_mode: bool = True         # ON di default (§3.1)
    mode: str = "generic"          # 'generic' | 'research'


class ChatMessage(BaseModel):
    conversation_id: str
    text: str
    repo_path: str = ""
    resume_session: str | None = None


@router.post("/new")
async def new_conversation(body: NewConversation):
    cid = new_id("conv")
    mode = body.mode if body.mode in ("generic", "research") else "generic"
    title = body.title
    if title == "Nuova conversazione" and mode == "research":
        title = "Ricerca online"
    get_db().execute(
        "INSERT INTO conversation(id, title, plan_mode, created_at, mode) "
        "VALUES(?,?,?,?,?)",
        (cid, title, 1 if body.plan_mode else 0, utcnow(), mode),
    )
    return {"conversation_id": cid, "mode": mode}


@router.post("/send")
async def send_message(body: ChatMessage):
    """Avvia un turno di planner in background; il testo arriva via SSE (/events)."""
    from ..planner import chat_stream  # lazy: dipende dall'SDK

    async def _run():
        try:
            await chat_stream(body.conversation_id, body.repo_path or ".",
                              body.text, body.resume_session)
        except Exception as e:  # noqa: BLE001 — mostrare i fallimenti (4.8)
            from ..events import get_bus
            await get_bus().emit(None, "error", {"detail": f"chat: {e}"})

    asyncio.ensure_future(_run())
    return {"status": "accepted"}
