"""Chat + plan mode (M3)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
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


class RenameConversation(BaseModel):
    title: str


@router.patch("/{conversation_id}")
async def rename_conversation(conversation_id: str, body: RenameConversation):
    """Rinomina il titolo di una chat. Emette `conversation_renamed` cosi' altre
    schede aperte restano coerenti via SSE (§B.6.5)."""
    db = get_db()
    if not db.query_one("SELECT id FROM conversation WHERE id=?", (conversation_id,)):
        raise HTTPException(status_code=404, detail="conversazione inesistente")
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="titolo vuoto")
    title = title[:120]
    db.execute("UPDATE conversation SET title=? WHERE id=?", (title, conversation_id))
    from ..events import get_bus
    await get_bus().emit(None, "conversation_renamed",
                         {"conversation_id": conversation_id, "title": title})
    return {"status": "renamed", "conversation_id": conversation_id, "title": title}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Elimina una chat: conversazione + messaggi. NON tocca `event` (append-only,
    regola 4.4) ne' i piani gia' generati (restano nello storico)."""
    db = get_db()
    if not db.query_one("SELECT id FROM conversation WHERE id=?", (conversation_id,)):
        raise HTTPException(status_code=404, detail="conversazione inesistente")
    db.execute("DELETE FROM message WHERE conversation_id=?", (conversation_id,))
    db.execute("DELETE FROM conversation WHERE id=?", (conversation_id,))
    return {"status": "deleted"}


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
