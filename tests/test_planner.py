"""Continuita' della conversazione del planner (§1.7): il session_id viene
persistito e riusato come resume al turno successivo. Fake client, niente SDK."""

import asyncio

from app.db import utcnow
from app.ids import new_id
from app.planner import chat_stream, set_client_cls


# cattura gli options.resume visti dal client, in ordine
seen_resume: list = []
_counter = {"n": 0}


class TextBlock:
    def __init__(self, text):
        self.text = text


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class _Init:
    subtype = "init"

    def __init__(self, session_id):
        self.data = {"session_id": session_id}


class FakePlannerClient:
    def __init__(self, options=None):
        self.options = options
        seen_resume.append(getattr(options, "resume", None))
        _counter["n"] += 1
        self._sid = f"sess-{_counter['n']}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        yield _Init(self._sid)
        yield AssistantMessage([TextBlock("risposta del planner")])


class _PlanJSONClient:
    """Finge un planner che restituisce un PlanDocument con path ASSOLUTI FUORI
    dalle root (come il bug reale del caso Garlasco)."""
    PAYLOAD = (
        '{"repo_path": "C:\\\\Users\\\\x\\\\Documents\\\\Garlasco", '
        '"tasks": [{"id": "t1", "title": "Sez 1", "instructions": "scrivi", '
        '"files_allowed": ["C:\\\\Users\\\\x\\\\Documents\\\\Garlasco\\\\intro.md"], '
        '"verify_cmd": "python -c \\"pass\\"", "verify_cwd": "C:\\\\altrove"}]}'
    )

    def __init__(self, options=None):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        yield AssistantMessage([TextBlock(self.PAYLOAD)])


def test_generate_plan_forces_repo_and_reroots_files(db, settings, roots):
    import asyncio
    import json
    from app.planner import generate_plan, set_client_cls

    project = str(roots[0] / "proj")
    set_client_cls(_PlanJSONClient)
    try:
        cid = new_id("conv")
        db.execute(
            "INSERT INTO conversation(id, title, plan_mode, created_at) VALUES(?,?,?,?)",
            (cid, "t", 1, utcnow()))
        plan_id = asyncio.run(generate_plan(cid, project))
    finally:
        set_client_cls(None)

    raw = json.loads(db.query_one(
        "SELECT raw_json FROM plan_document WHERE id=?", (plan_id,))["raw_json"])
    # repo_path forzato al progetto, non quello inventato dal planner
    assert raw["repo_path"] == project
    # files_allowed ri-radicati (solo nome file) e verify_cwd assoluto -> "."
    assert raw["tasks"][0]["files_allowed"] == ["intro.md"]
    assert raw["tasks"][0]["verify_cwd"] == "."


def test_planner_session_persisted_and_resumed(db):
    seen_resume.clear()
    _counter["n"] = 0
    set_client_cls(FakePlannerClient)
    try:
        cid = new_id("conv")
        db.execute(
            "INSERT INTO conversation(id, title, plan_mode, created_at) VALUES(?,?,?,?)",
            (cid, "t", 1, utcnow()))

        # turno 1: nessuna sessione precedente
        asyncio.run(chat_stream(cid, ".", "ciao"))
        assert seen_resume[0] is None
        run1 = db.query_one(
            "SELECT session_id FROM run WHERE conversation_id=?", (cid,))
        assert run1["session_id"] == "sess-1"
        # messaggi persistiti (user + assistant)
        msgs = db.query("SELECT role FROM message WHERE conversation_id=?", (cid,))
        assert [m["role"] for m in msgs] == ["user", "assistant"]

        # turno 2: deve riprendere dalla sessione del turno 1
        asyncio.run(chat_stream(cid, ".", "aggiungi X"))
        assert seen_resume[1] == "sess-1"
    finally:
        set_client_cls(None)  # ripristina l'import pigro dell'SDK
