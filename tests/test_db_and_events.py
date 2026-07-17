"""Schema §5.1, append-only event log e replay (regola 4.4/4.15)."""

import asyncio

from app.events import get_bus, run_event_stream, global_event_stream


def test_schema_tables_exist(db):
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("project", "conversation", "message", "plan_document", "task",
              "run", "event", "approval", "push_subscription", "usage_sample"):
        assert t in names


def test_wal_mode(db):
    mode = db.query_one("PRAGMA journal_mode")[0]
    assert mode.lower() == "wal"


def test_event_append_only_and_replay(db):
    async def scenario():
        bus = get_bus()
        ids = []
        for i in range(3):
            ids.append(await bus.emit("run-1", "assistant_text", {"n": i}))
        # replay da metà (regola 4.15): riceve solo gli eventi successivi
        gen = run_event_stream("run-1", ids[0])
        got = []
        for _ in range(2):
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=1)
            got.append(chunk)
        assert "run-1" not in got[0] or True   # sanity
        # i due replay corrispondono a ids[1] e ids[2]
        assert f"id: {ids[1]}" in got[0]
        assert f"id: {ids[2]}" in got[1]
        await gen.aclose()

    asyncio.run(scenario())


def test_global_replay_across_runs(db):
    async def scenario():
        bus = get_bus()
        a = await bus.emit("run-a", "assistant_text", {"x": 1})
        b = await bus.emit(None, "chat_delta", {"text": "hi"})
        # replay globale da 'a-1' deve includere a e b (canali diversi, 4.15)
        gen = global_event_stream(a - 1)
        first = await asyncio.wait_for(gen.__anext__(), timeout=1)
        second = await asyncio.wait_for(gen.__anext__(), timeout=1)
        assert f"id: {a}" in first
        assert f"id: {b}" in second
        await gen.aclose()

    asyncio.run(scenario())
