"""Eliminazione: soft di default, purge esplicito (§B.3). Logica DB pura.

Il tavolo `event` e' il log append-only (regola 4.4): l'eliminazione vera dev'essere
DELIBERATA. Percio':
  * soft-delete (default): setta `deleted_at`; le liste filtrano deleted_at IS NULL.
    Reversibile, log intatto.
  * hard purge (esplicito): cascata conversation -> plan -> task -> run -> message
    -> event correlati. Irreversibile. E' l'UNICO percorso che rimuove righe di
    `event` (§B.6.4).

Guardia (§B.3/B.6.3): non si puo' purgare NE' soft-eliminare qualcosa con un task
in esecuzione (running/verifying/escalated) -> il chiamante risponde HTTP 409.
"""

from __future__ import annotations

from .db import Database, utcnow

# stati che indicano "lavoro in esecuzione": vietano delete/purge (§B.3).
ACTIVE_TASK_STATES = ("running", "verifying", "escalated")
# un run "vivo": non si elimina finche' gira (prima STOP, poi delete).
RUN_ACTIVE_STATES = ("running",)


class LifecycleConflict(Exception):
    """Sollevata quando l'operazione tocca lavoro in esecuzione (-> HTTP 409)."""


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


# --- guardie ---------------------------------------------------------------------
def plan_has_active_task(db: Database, plan_id: str) -> bool:
    row = db.query_one(
        f"SELECT COUNT(*) c FROM task WHERE plan_id=? "
        f"AND status IN ({_placeholders(len(ACTIVE_TASK_STATES))})",
        (plan_id, *ACTIVE_TASK_STATES))
    return bool(row and row["c"])


def conversation_has_active_task(db: Database, conversation_id: str) -> bool:
    row = db.query_one(
        f"SELECT COUNT(*) c FROM task t JOIN plan_document p ON t.plan_id=p.id "
        f"WHERE p.conversation_id=? "
        f"AND t.status IN ({_placeholders(len(ACTIVE_TASK_STATES))})",
        (conversation_id, *ACTIVE_TASK_STATES))
    return bool(row and row["c"])


# --- soft-delete (default) -------------------------------------------------------
def soft_delete_task(db: Database, task_id: str) -> None:
    row = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
    if row and row["status"] in ACTIVE_TASK_STATES:
        raise LifecycleConflict("annulla prima l'esecuzione")
    db.execute("UPDATE task SET deleted_at=? WHERE id=?", (utcnow(), task_id))


def soft_delete_plan(db: Database, plan_id: str) -> None:
    if plan_has_active_task(db, plan_id):
        raise LifecycleConflict("annulla prima l'esecuzione")
    now = utcnow()
    db.execute("UPDATE plan_document SET deleted_at=? WHERE id=?", (now, plan_id))
    db.execute("UPDATE task SET deleted_at=? WHERE plan_id=? AND deleted_at IS NULL",
               (now, plan_id))


def soft_delete_conversation(db: Database, conversation_id: str) -> None:
    if conversation_has_active_task(db, conversation_id):
        raise LifecycleConflict("annulla prima l'esecuzione")
    now = utcnow()
    db.execute("UPDATE conversation SET deleted_at=? WHERE id=?",
               (now, conversation_id))
    plans = db.query("SELECT id FROM plan_document WHERE conversation_id=?",
                     (conversation_id,))
    for p in plans:
        db.execute("UPDATE plan_document SET deleted_at=? WHERE id=?", (now, p["id"]))
        db.execute("UPDATE task SET deleted_at=? WHERE plan_id=? "
                   "AND deleted_at IS NULL", (now, p["id"]))


# --- hard purge (esplicito, irreversibile) --------------------------------------
def _purge_runs_and_events(db: Database, task_ids: list[str]) -> None:
    if not task_ids:
        return
    ph = _placeholders(len(task_ids))
    run_rows = db.query(
        f"SELECT id FROM run WHERE task_id IN ({ph})", tuple(task_ids))
    run_ids = [r["id"] for r in run_rows]
    if run_ids:
        rph = _placeholders(len(run_ids))
        # §B.6.4: il purge e' l'UNICO percorso che rimuove righe di `event`.
        db.execute(f"DELETE FROM event WHERE run_id IN ({rph})", tuple(run_ids))
        db.execute(f"DELETE FROM approval WHERE run_id IN ({rph})", tuple(run_ids))
    db.execute(f"DELETE FROM run WHERE task_id IN ({ph})", tuple(task_ids))


def purge_plan(db: Database, plan_id: str) -> None:
    if plan_has_active_task(db, plan_id):
        raise LifecycleConflict("annulla prima l'esecuzione")
    task_rows = db.query("SELECT id FROM task WHERE plan_id=?", (plan_id,))
    _purge_runs_and_events(db, [t["id"] for t in task_rows])
    db.execute("DELETE FROM task WHERE plan_id=?", (plan_id,))
    db.execute("DELETE FROM plan_document WHERE id=?", (plan_id,))


def delete_run(db: Database, run_id: str) -> None:
    """Elimina un singolo run: le sue righe di `event` e `approval`, poi il run.

    Il run non ha `deleted_at` (e' log-like): l'eliminazione e' hard e DELIBERATA
    (§B.6.4, come il purge). Guardia: un run 'running' non si elimina — prima si
    ferma (STOP), poi lo si elimina -> LifecycleConflict (HTTP 409)."""
    row = db.query_one("SELECT status FROM run WHERE id=?", (run_id,))
    if row and row["status"] in RUN_ACTIVE_STATES:
        raise LifecycleConflict("ferma prima il run (STOP), poi eliminalo")
    db.execute("DELETE FROM event WHERE run_id=?", (run_id,))
    db.execute("DELETE FROM approval WHERE run_id=?", (run_id,))
    db.execute("DELETE FROM run WHERE id=?", (run_id,))


def purge_conversation(db: Database, conversation_id: str) -> None:
    if conversation_has_active_task(db, conversation_id):
        raise LifecycleConflict("annulla prima l'esecuzione")
    plans = db.query("SELECT id FROM plan_document WHERE conversation_id=?",
                     (conversation_id,))
    for p in plans:
        task_rows = db.query("SELECT id FROM task WHERE plan_id=?", (p["id"],))
        _purge_runs_and_events(db, [t["id"] for t in task_rows])
        db.execute("DELETE FROM task WHERE plan_id=?", (p["id"],))
    db.execute("DELETE FROM plan_document WHERE conversation_id=?",
               (conversation_id,))
    db.execute("DELETE FROM message WHERE conversation_id=?", (conversation_id,))
    db.execute("DELETE FROM conversation WHERE id=?", (conversation_id,))
