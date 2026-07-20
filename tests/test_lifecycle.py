"""Ciclo di vita (§B): annulla / pausa / blocca / elimina (soft) / purga (hard).

Invarianti (§B.6): l'annullamento porta a 'cancelled' e non lascia task appesi
(done-event settato, slot scheduler rilasciato); i dipendenti di un task
cancelled non partono; il purge e' irreversibile/confermato/vietato su lavoro in
esecuzione; il soft-delete non intacca `event`.
"""

import asyncio
import json

import pytest

from app.db import utcnow
from app.executor import get_pool
from app.ids import new_id
from app.lifecycle import (
    LifecycleConflict, purge_conversation, purge_plan, soft_delete_conversation,
)

from tests.test_executor import FakeClient, _InitMsg, _insert_plan, _brief


async def _wait_status(db, task_id, status, timeout=5.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
        if row and row["status"] == status:
            return True
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} non ha raggiunto '{status}' "
                         f"(ultimo: {row['status'] if row else None})")


def _task_ids(db, plan_id):
    return [r["id"] for r in db.query(
        "SELECT id FROM task WHERE plan_id=? ORDER BY seq", (plan_id,))]


# =============================================================================
# 1. annulla un piano in corso: cancelled, slot liberato, dipendenti fermi
# =============================================================================
class _BlockingClient(FakeClient):
    """Si blocca nella fase modello finche' non viene annullato."""
    instances = 0

    async def __aenter__(self):
        _BlockingClient.instances += 1
        return self

    async def receive_response(self):
        yield _InitMsg()
        await asyncio.sleep(30)   # hang: verra' cancellato


def test_cancel_running_plan(db, settings, roots):
    settings.max_local_concurrency = 2

    async def scenario():
        repo = roots[0] / "cancelplan"
        repo.mkdir()
        briefs = [
            _brief("t1", "python -c \"raise SystemExit(0)\"", ["a.py"]),
            _brief("t2", "python -c \"raise SystemExit(0)\"", ["b.py"], deps=["t1"]),
        ]
        plan_id = _insert_plan(db, str(repo), briefs)
        t1, t2 = _task_ids(db, plan_id)

        pool = get_pool()
        _BlockingClient.instances = 0
        pool._client_cls = _BlockingClient
        run_task = asyncio.ensure_future(pool.approve_and_run(plan_id))

        await _wait_status(db, t1, "running")
        # t1 ha preso uno slot (fallback cap semplice, niente GPU nei test)
        assert pool.scheduler.local_active == 1

        await pool.cancel_plan(plan_id)
        await asyncio.wait_for(run_task, 5)

        # t1 annullato, slot rilasciato, done-event settato (niente task appeso)
        assert db.query_one("SELECT status FROM task WHERE id=?", (t1,))["status"] \
            == "cancelled"
        assert pool.scheduler.local_active == 0
        assert pool._done_events[t1].is_set()

        # t2 (dipendente) NON e' partito: mai istanziato il client oltre t1
        assert _BlockingClient.instances == 1
        assert db.query_one("SELECT status FROM task WHERE id=?", (t2,))["status"] \
            == "cancelled"

        # piano -> cancelled + evento
        assert db.query_one("SELECT status FROM plan_document WHERE id=?",
                            (plan_id,))["status"] == "cancelled"
        assert db.query_one("SELECT * FROM event WHERE kind='task_cancelled'")

    asyncio.run(scenario())


# =============================================================================
# 2. annulla un task in verify: il sottoprocesso viene terminato (§B.2)
# =============================================================================
def test_cancel_task_kills_verify_subprocess(db, settings, roots):
    async def scenario():
        repo = roots[0] / "killverify"
        repo.mkdir()
        # verify che dorme a lungo: il task resta in 'verifying' finche' non muore
        verify = "python -c \"import time; time.sleep(30)\""
        plan_id = _insert_plan(db, str(repo),
                               [_brief("t1", verify, ["a.py"], )])
        # timeout ampio: e' il cancel a fermarlo, non il timeout
        (t1,) = _task_ids(db, plan_id)
        brief = json.loads(db.query_one(
            "SELECT brief_json FROM task WHERE id=?", (t1,))["brief_json"])
        brief["timeout_s"] = 60
        db.execute("UPDATE task SET brief_json=? WHERE id=?",
                   (json.dumps(brief), t1))

        pool = get_pool()
        pool._client_cls = FakeClient
        run_task = asyncio.ensure_future(pool.approve_and_run(plan_id))

        await _wait_status(db, t1, "verifying")
        # il Popen del verify e' registrato ed e' vivo. La registrazione in
        # _verify_procs avviene subito DOPO il passaggio a 'verifying': sotto
        # carico c'e' un piccolo scarto, quindi attendi la comparsa del Popen.
        proc = None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            proc = pool._verify_procs.get(t1)
            if proc is not None:
                break
            await asyncio.sleep(0.02)
        assert proc is not None and proc.poll() is None

        await pool.cancel_task(t1)
        await asyncio.wait_for(run_task, 10)

        assert db.query_one("SELECT status FROM task WHERE id=?", (t1,))["status"] \
            == "cancelled"
        # il sottoprocesso e' stato terminato
        assert proc.poll() is not None

    asyncio.run(scenario())


# =============================================================================
# 3. pausa/riprendi: in pausa non ammette; resume drena
# =============================================================================
def test_pause_blocks_then_resume_drains(db, settings, roots):
    async def scenario():
        repo = roots[0] / "pause"
        repo.mkdir()
        plan_id = _insert_plan(
            db, str(repo), [_brief("t1", "python -c \"raise SystemExit(0)\"", ["a.py"])])
        (t1,) = _task_ids(db, plan_id)

        pool = get_pool()
        pool._client_cls = FakeClient
        await pool.pause_queue()
        assert pool.scheduler.paused

        run_task = asyncio.ensure_future(pool.approve_and_run(plan_id))
        await asyncio.sleep(0.3)   # in pausa: il task NON deve avanzare
        assert db.query_one("SELECT status FROM task WHERE id=?",
                            (t1,))["status"] == "pending"

        await pool.resume_queue()
        await asyncio.wait_for(run_task, 5)
        assert db.query_one("SELECT status FROM task WHERE id=?",
                            (t1,))["status"] == "done"
        # persistito
        assert db.get_state("queue_paused") == "0"

    asyncio.run(scenario())


# =============================================================================
# 4. block impedisce il pickup
# =============================================================================
class _CountingClient(FakeClient):
    instances = 0

    async def __aenter__(self):
        _CountingClient.instances += 1
        return self


def test_block_prevents_pickup(db, settings, roots):
    async def scenario():
        repo = roots[0] / "block"
        repo.mkdir()
        plan_id = _insert_plan(
            db, str(repo), [_brief("t1", "python -c \"raise SystemExit(0)\"", ["a.py"])])
        (t1,) = _task_ids(db, plan_id)

        pool = get_pool()
        _CountingClient.instances = 0
        pool._client_cls = _CountingClient
        assert await pool.block_task(t1)
        assert db.query_one("SELECT status FROM task WHERE id=?",
                            (t1,))["status"] == "blocked"

        await asyncio.wait_for(pool.approve_and_run(plan_id), 5)
        # il task bloccato NON e' stato eseguito
        assert _CountingClient.instances == 0
        assert db.query_one("SELECT status FROM task WHERE id=?",
                            (t1,))["status"] == "blocked"

    asyncio.run(scenario())


# =============================================================================
# 5. soft-delete nasconde ma tiene le righe; purge cascata rimuove davvero
# =============================================================================
def _seed_conversation(db):
    cid = new_id("conv")
    db.execute("INSERT INTO conversation(id, title, plan_mode, created_at) "
               "VALUES(?,?,?,?)", (cid, "C", 1, utcnow()))
    db.execute("INSERT INTO message(conversation_id, role, content, ts) "
               "VALUES(?,?,?,?)", (cid, "user", "ciao", utcnow()))
    pid = new_id("plan")
    db.execute("INSERT INTO plan_document(id, conversation_id, status, raw_json, "
               "created_at) VALUES(?,?,?,?,?)",
               (pid, cid, "done", json.dumps({"tasks": []}), utcnow()))
    tid = new_id("t")
    db.execute("INSERT INTO task(id, plan_id, seq, title, brief_json, status, "
               "backend) VALUES(?,?,?,?,?,?,?)",
               (tid, pid, 0, "T", "{}", "done", "ollama"))
    rid = new_id("run")
    db.execute("INSERT INTO run(id, task_id, backend, model, status, started_at) "
               "VALUES(?,?,?,?,?,?)", (rid, tid, "ollama", "m", "done", utcnow()))
    db.append_event(rid, "assistant_text", {"text": "x"})
    return cid, pid, tid, rid


def test_soft_delete_hides_but_keeps_rows(db, settings):
    cid, pid, tid, rid = _seed_conversation(db)
    soft_delete_conversation(db, cid)
    # righe ancora presenti (log intatto)
    assert db.query_one("SELECT deleted_at FROM conversation WHERE id=?",
                        (cid,))["deleted_at"] is not None
    assert db.query_one("SELECT deleted_at FROM plan_document WHERE id=?",
                        (pid,))["deleted_at"] is not None
    assert db.query_one("SELECT deleted_at FROM task WHERE id=?",
                        (tid,))["deleted_at"] is not None
    # l'evento append-only NON e' toccato dal soft-delete (§B.6.4)
    assert db.query_one("SELECT * FROM event WHERE run_id=?", (rid,)) is not None


def test_soft_delete_hidden_from_dashboard(client, db):
    cid, pid, tid, rid = _seed_conversation(db)
    soft_delete_conversation(db, cid)
    r = client.get("/")
    assert r.status_code == 200
    assert cid not in r.text          # nascosta dalla lista


def test_purge_cascade_removes_rows(db, settings):
    cid, pid, tid, rid = _seed_conversation(db)
    purge_conversation(db, cid)
    assert db.query_one("SELECT * FROM conversation WHERE id=?", (cid,)) is None
    assert db.query_one("SELECT * FROM plan_document WHERE id=?", (pid,)) is None
    assert db.query_one("SELECT * FROM task WHERE id=?", (tid,)) is None
    assert db.query_one("SELECT * FROM run WHERE id=?", (rid,)) is None
    assert db.query_one("SELECT * FROM message WHERE conversation_id=?",
                        (cid,)) is None
    # purge = UNICO percorso che rimuove righe di event (§B.6.4)
    assert db.query_one("SELECT * FROM event WHERE run_id=?", (rid,)) is None


# =============================================================================
# 6. delete/purge di lavoro in esecuzione -> conflitto (409)
# =============================================================================
def test_delete_running_plan_conflicts(db, settings):
    pid = new_id("plan")
    db.execute("INSERT INTO plan_document(id, conversation_id, status, raw_json, "
               "created_at) VALUES(?,?,?,?,?)",
               (pid, "c1", "executing", json.dumps({"tasks": []}), utcnow()))
    db.execute("INSERT INTO task(id, plan_id, seq, title, brief_json, status, "
               "backend) VALUES(?,?,?,?,?,?,?)",
               (new_id("t"), pid, 0, "T", "{}", "running", "ollama"))
    with pytest.raises(LifecycleConflict):
        purge_plan(db, pid)


def test_http_delete_and_purge_running_returns_409(client, db):
    pid = "plan-run"
    db.execute("INSERT INTO plan_document(id, conversation_id, status, raw_json, "
               "created_at) VALUES(?,?,?,?,?)",
               (pid, "c1", "executing", json.dumps({"tasks": []}), utcnow()))
    db.execute("INSERT INTO task(id, plan_id, seq, title, brief_json, status, "
               "backend) VALUES(?,?,?,?,?,?,?)",
               ("tk-run", pid, 0, "T", "{}", "verifying", "ollama"))
    assert client.request("DELETE", f"/plans/{pid}").status_code == 409
    assert client.post(f"/plans/{pid}/purge", json={"confirm": True}).status_code == 409


def test_http_purge_requires_confirm(client, db):
    pid = "plan-x"
    db.execute("INSERT INTO plan_document(id, conversation_id, status, raw_json, "
               "created_at) VALUES(?,?,?,?,?)",
               (pid, "c1", "done", json.dumps({"tasks": []}), utcnow()))
    assert client.post(f"/plans/{pid}/purge", json={"confirm": False}).status_code == 400
    r = client.post(f"/plans/{pid}/purge", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "purged"


def test_http_queue_pause_resume(client, db):
    assert client.post("/queue/pause").json()["paused"] is True
    assert client.get("/queue").json()["paused"] is True
    assert client.post("/queue/resume").json()["paused"] is False


def test_http_block_running_task_conflicts(client, db):
    db.execute("INSERT INTO task(id, plan_id, seq, title, brief_json, status, "
               "backend) VALUES(?,?,?,?,?,?,?)",
               ("tk-r", "p", 0, "T", "{}", "running", "ollama"))
    assert client.post("/tasks/tk-r/block").status_code == 409
