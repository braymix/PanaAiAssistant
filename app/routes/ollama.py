"""Log live del server Ollama + stato dei modelli caricati (per la UI)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..config import get_settings

router = APIRouter(prefix="/ollama")

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/ps")
async def ollama_ps():
    """Modelli attualmente caricati (da /api/ps): quanto sta in GPU vs CPU."""
    url = get_settings().ollama_url.rstrip("/") + "/api/ps"
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(url)
            return r.json()
    except Exception as e:  # noqa: BLE001 — Ollama potrebbe essere spento
        return {"error": f"Ollama non raggiungibile: {type(e).__name__}: {e}",
                "models": []}


@router.get("/logs/stream")
async def logs_stream(request: Request):
    path = Path(get_settings().ollama_log)
    # parti dagli ultimi ~4 KB, non da tutto il file
    start = max(0, path.stat().st_size - 4000) if path.exists() else 0

    async def gen():
        # prima manda solo la coda recente
        if path.exists():
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(start)
                tail = f.read()
            for line in tail.splitlines():
                if line.strip():
                    yield f"event: logline\ndata: {line}\n\n"
        else:
            yield f"event: logline\ndata: [log non trovato: {path} — avvia Ollama o imposta ARGO_OLLAMA_LOG]\n\n"
        # poi segue il file (tail -f)
        last_size = path.stat().st_size if path.exists() else 0
        while True:
            if await request.is_disconnected():
                break
            try:
                if path.exists():
                    size = path.stat().st_size
                    if size < last_size:
                        last_size = 0
                    if size > last_size:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            chunk = f.read()
                        last_size = size
                        for line in chunk.splitlines():
                            if line.strip():
                                yield f"event: logline\ndata: {line}\n\n"
                    else:
                        yield ": keep-alive\n\n"
                else:
                    yield f"event: logline\ndata: [in attesa del log: {path}]\n\n"
            except Exception as e:  # noqa: BLE001
                yield f"event: logline\ndata: [errore: {e}]\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)
