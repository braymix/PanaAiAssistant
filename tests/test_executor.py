"""Orchestrazione M4 con un fake ClaudeSDKClient: nessun Ollama, nessuna GPU.

Verifica la logica che il bootstrap tiene per non-negoziabile:
  * l'ordine depends_on (§5.1);
  * chi decide se il task e' finito e' verify_cmd, non il modello (§5.2);
  * retry ed escalation all'abbonamento dopo MAX_LOCAL_RETRIES (§3.5/M4);
  * costo/turni persistiti sul run (per le stats M5).
"""

import asyncio
import json

import pytest

from app.briefs import PlanDocument
from app.db import get_db, utcnow
from app.executor import get_pool
from app.ids import new_id


# --- fake SDK: context manager async con query/receive_response ------------------
class ResultMessage:  # il nome della classe conta: _drive_client fa type().__name__
    subtype = "success"
    is_error = False
    num_turns = 2
    total_cost_usd = 0.02


class _InitMsg:
    subtype = "init"
    data = {"session_id": "sess-fake"}


class FakeClient:
    """Non tocca nulla: nel test e' verify_cmd a decidere l'esito (come in prod)."""
    def __init__(self, options=None):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        yield _InitMsg()
        yield ResultMessage()


def _insert_plan(db, repo_path, briefs) -> str:
    plan = PlanDocument.from_dict({"repo_path": repo_path, "tasks": briefs})
    plan_id = new_id("plan")
    db.execute(
        "INSERT INTO plan_document(id, conversation_id, status, raw_json, created_at) "
        "VALUES(?,?,?,?,?)",
        (plan_id, "c1", "draft", json.dumps(plan.to_dict()), utcnow()),
    )
    for seq, t in enumerate(plan.tasks):
        db.execute(
            "INSERT INTO task(id, plan_id, seq, title, brief_json, status, backend, "
            "attempts, depends_on) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("t"), plan_id, seq, t.title, json.dumps(t.to_dict()),
             "pending", "ollama", 0, json.dumps(t.depends_on)),
        )
    return plan_id


def _brief(tid, verify, files, deps=None):
    return {"id": tid, "title": tid, "instructions": "fai",
            "files_allowed": files, "verify_cmd": verify, "verify_cwd": ".",
            "depends_on": deps or [], "max_turns": 5, "timeout_s": 30}


def test_depends_on_order_and_verify_decides(db, settings, roots):
    repo = roots[0] / "proj"
    repo.mkdir()
    # ogni verify appende il proprio id a order.txt ed esce 0 -> successo
    def vcmd(tid):
        return (f"python -c \"open('order.txt','a').write('{tid}\\n')\"")
    briefs = [
        _brief("t1", vcmd("t1"), ["a.py"]),
        _brief("t2", vcmd("t2"), ["b.py"], deps=["t1"]),
    ]
    plan_id = _insert_plan(db, str(repo), briefs)

    pool = get_pool()
    pool._client_cls = FakeClient
    asyncio.run(pool.approve_and_run(plan_id))

    # depends_on rispettato: t1 completa prima di t2
    order = (repo / "order.txt").read_text().split()
    assert order == ["t1", "t2"]

    # entrambi done, piano done, VIA timbrato (approved_at valorizzato)
    tasks = db.query("SELECT status FROM task WHERE plan_id=?", (plan_id,))
    assert all(t["status"] == "done" for t in tasks)
    plan = db.query_one("SELECT * FROM plan_document WHERE id=?", (plan_id,))
    assert plan["status"] == "done" and plan["approved_at"]

    # costo/turni persistiti (M5): almeno un run con cost>0
    run = db.query_one("SELECT * FROM run WHERE cost_usd>0 LIMIT 1")
    assert run and run["turns"] == 2 and run["session_id"] == "sess-fake"


def test_retry_then_escalation_to_subscription(db, settings, roots):
    """verify fallisce le prime volte e passa solo al 3° tentativo (= l'escalation).

    max_local_retries=1 -> 2 tentativi Ollama, poi 1 su abbonamento. Il counter
    passa a n>=3, cioe' proprio sull'attempt escalato.
    """
    settings.max_local_retries = 1
    repo = roots[0] / "proj2"
    repo.mkdir()
    # counter persistente: exit 0 solo quando n>=3
    verify = (
        "python -c \"import os;p='c.txt';"
        "n=(int(open(p).read()) if os.path.exists(p) else 0)+1;"
        "open(p,'w').write(str(n));"
        "raise SystemExit(0 if n>=3 else 1)\""
    )
    plan_id = _insert_plan(db, str(repo), [_brief("t1", verify, ["a.py"])])

    pool = get_pool()
    pool._client_cls = FakeClient
    asyncio.run(pool.approve_and_run(plan_id))

    task = db.query_one("SELECT * FROM task WHERE plan_id=?", (plan_id,))
    assert task["status"] == "done"           # passato all'escalation
    # il counter dimostra i 3 tentativi effettivi
    assert (repo / "c.txt").read_text().strip() == "3"
    # c'e' un run sul backend 'subscription' (l'escalation)
    runs = db.query("SELECT DISTINCT backend FROM run WHERE task_id=?", (task["id"],))
    backends = {r["backend"] for r in runs}
    assert "subscription" in backends
    # e un evento di escalation nel log append-only
    esc = db.query_one("SELECT * FROM event WHERE kind='escalation'")
    assert esc is not None


def test_all_retries_fail_marks_failed(db, settings, roots):
    settings.max_local_retries = 1
    repo = roots[0] / "proj3"
    repo.mkdir()
    plan_id = _insert_plan(
        db, str(repo),
        [_brief("t1", "python -c \"raise SystemExit(1)\"", ["a.py"])])

    pool = get_pool()
    pool._client_cls = FakeClient
    asyncio.run(pool.approve_and_run(plan_id))

    task = db.query_one("SELECT * FROM task WHERE plan_id=?", (plan_id,))
    assert task["status"] == "failed"
    plan = db.query_one("SELECT status FROM plan_document WHERE id=?", (plan_id,))
    assert plan["status"] == "failed"
