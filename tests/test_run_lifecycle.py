"""Controllo completo: STOP + eliminazione dei run (oltre a piani/chat/task).

L'utente deve poter fermare ed eliminare qualsiasi cosa. Qui: il run singolo,
che non ha `deleted_at` (log-like): delete_run e' hard e vietato su 'running'."""

from __future__ import annotations

from app.db import utcnow
from app.ids import new_id
from app.lifecycle import LifecycleConflict, delete_run

import pytest


def _insert_run(db, run_id, task_id=None, status="done"):
    db.execute(
        "INSERT INTO run(id, task_id, backend, model, status, started_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, task_id, "ollama", "m", status, utcnow()))


def _insert_event(db, run_id, kind="tool_use"):
    db.execute("INSERT INTO event(run_id, ts, kind, payload) VALUES(?,?,?,?)",
               (run_id, utcnow(), kind, "{}"))


# ------------------------------------------------------------- logica

def test_delete_run_removes_events_and_approvals(db):
    rid = new_id("run")
    _insert_run(db, rid, status="done")
    _insert_event(db, rid)
    db.execute(
        "INSERT INTO approval(id, run_id, tool_name, status, pushed_at) "
        "VALUES(?,?,?,?,?)", (new_id("apr"), rid, "Write", "allowed", utcnow()))
    delete_run(db, rid)
    assert db.query_one("SELECT id FROM run WHERE id=?", (rid,)) is None
    assert db.query("SELECT id FROM event WHERE run_id=?", (rid,)) == []
    assert db.query("SELECT id FROM approval WHERE run_id=?", (rid,)) == []


def test_delete_running_run_conflicts(db):
    rid = new_id("run")
    _insert_run(db, rid, status="running")
    with pytest.raises(LifecycleConflict):
        delete_run(db, rid)
    # non eliminato
    assert db.query_one("SELECT id FROM run WHERE id=?", (rid,)) is not None


# ------------------------------------------------------------- HTTP

def test_delete_run_endpoint(client, db):
    rid = new_id("run")
    _insert_run(db, rid, status="done")
    r = client.request("DELETE", f"/runs/{rid}")
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert db.query_one("SELECT id FROM run WHERE id=?", (rid,)) is None


def test_delete_run_404(client):
    assert client.request("DELETE", "/runs/nope").status_code == 404


def test_delete_running_run_409(client, db):
    rid = new_id("run")
    _insert_run(db, rid, status="running")
    assert client.request("DELETE", f"/runs/{rid}").status_code == 409


def test_stop_run_without_task_is_noop(client, db):
    rid = new_id("run")
    _insert_run(db, rid, task_id=None, status="running")
    r = client.post(f"/runs/{rid}/stop")
    assert r.status_code == 200 and r.json()["status"] == "noop"


def test_stop_run_404(client):
    assert client.post("/runs/nope/stop").status_code == 404
