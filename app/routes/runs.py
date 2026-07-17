"""SSE. Replay con Last-Event-ID (regola 4.15)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..events import global_event_stream, run_event_stream

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str):
    stream = run_event_stream(run_id, _last_event_id(request))
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@router.get("/events")
async def global_events(request: Request):
    """Canale globale per dashboard/chat/stats, con replay Last-Event-ID (4.15)."""
    stream = global_event_stream(_last_event_id(request))
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers=_SSE_HEADERS)
