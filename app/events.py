"""Event bus + SSE. La rete cade (regola 4.15): replay da `event` con Last-Event-ID.

`event` e' l'audit log append-only. Ogni evento persiste PRIMA di essere emesso
in SSE, cosi' un client che si riconnette con Last-Event-ID recupera tutto.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from .db import get_db


@dataclass(eq=False)   # identity hash: serve per stare in un set
class _Subscriber:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1000))


class EventBus:
    """Pub/sub in-memory per il live; la persistenza e' su SQLite."""

    def __init__(self) -> None:
        # per-run e un canale globale ("*") per la dashboard/stats
        self._subs: dict[str, set[_Subscriber]] = {}
        self._lock = asyncio.Lock()

    async def _subscribers(self, channel: str) -> set[_Subscriber]:
        return self._subs.setdefault(channel, set())

    async def subscribe(self, channel: str) -> _Subscriber:
        sub = _Subscriber()
        async with self._lock:
            (await self._subscribers(channel)).add(sub)
        return sub

    async def unsubscribe(self, channel: str, sub: _Subscriber) -> None:
        async with self._lock:
            self._subs.get(channel, set()).discard(sub)

    async def publish(self, channel: str, event_id: int, kind: str, payload) -> None:
        msg = {"id": event_id, "kind": kind, "payload": payload}
        async with self._lock:
            targets = list(self._subs.get(channel, set()))
        for sub in targets:
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # il replay da DB coprira' il buco alla riconnessione

    async def emit(self, run_id: str | None, kind: str, payload) -> int:
        """Persiste (append-only) e poi pubblica. Ritorna l'event id."""
        event_id = get_db().append_event(run_id, kind, payload)
        if run_id:
            await self.publish(run_id, event_id, kind, payload)
        await self.publish("*", event_id, kind, payload)
        return event_id


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def sse_format(event_id: int, kind: str, data) -> str:
    body = data if isinstance(data, str) else json.dumps(data)
    return f"id: {event_id}\nevent: {kind}\ndata: {body}\n\n"


async def run_event_stream(run_id: str, last_event_id: int):
    """Generatore SSE per un run: prima il replay da DB, poi il live."""
    bus = get_bus()
    sub = await bus.subscribe(run_id)
    try:
        # replay di quanto perso (regola 4.15)
        for row in get_db().events_after(run_id, last_event_id):
            payload = row["payload"]
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                pass
            yield sse_format(row["id"], row["kind"], payload)
            last_event_id = row["id"]
        # live
        while True:
            try:
                msg = await asyncio.wait_for(sub.queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if msg["id"] <= last_event_id:
                continue
            yield sse_format(msg["id"], msg["kind"], msg["payload"])
            last_event_id = msg["id"]
    finally:
        await bus.unsubscribe(run_id, sub)
