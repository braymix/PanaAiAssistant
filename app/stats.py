"""Stats live (M5). Numeri che in 2 secondi dicono cosa succede e quanto costa."""

from __future__ import annotations

from .config import get_settings
from .db import get_db, utcnow


def snapshot(pushes_sent: int = 0, ollama_queue: int = 0) -> dict:
    db = get_db()
    today = utcnow()[:10]

    active_runs = db.query_one(
        "SELECT COUNT(*) c FROM run WHERE status='running'")["c"]
    cost_today = db.query_one(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM run WHERE started_at LIKE ?",
        (today + "%",))["s"]
    pending = db.query_one(
        "SELECT COUNT(*) c FROM approval WHERE status='pending'")["c"]

    def count(status: str) -> int:
        return db.query_one(
            "SELECT COUNT(*) c FROM task WHERE status=?", (status,))["c"]

    rate = get_settings().usd_to_eur
    cost_usd = cost_today or 0.0
    return {
        "active_runs": active_runs,
        "cost_today": round(cost_usd, 4),                 # USD, stimato
        "cost_today_eur": round(cost_usd * rate, 2),      # EUR, stimato
        "pending_approvals": pending,
        "ollama_queue": ollama_queue,
        "pushes_sent": pushes_sent,          # = qualita' del piano (§3.2)
        "tasks_done": count("done"),
        "tasks_failed": count("failed"),
        "tasks_escalated": count("escalated"),
        "tasks_running": count("running"),
    }


def record_sample(snap: dict) -> None:
    get_db().execute(
        "INSERT INTO usage_sample(ts, active_runs, cost_today, tokens_in, "
        "tokens_out, ollama_queue, pending_approvals) VALUES(?,?,?,?,?,?,?)",
        (utcnow(), snap["active_runs"], snap["cost_today"], 0, 0,
         snap["ollama_queue"], snap["pending_approvals"]),
    )
