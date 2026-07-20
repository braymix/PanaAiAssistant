"""Stats live (M5). Numeri che in 2 secondi dicono cosa succede e quanto costa."""

from __future__ import annotations

from .config import get_settings
from .db import get_db, utcnow


def snapshot(pushes_sent: int = 0, ollama_queue: int = 0,
             scheduler=None) -> dict:
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

    # --- settorializzazione (§A.8): distribuzione dei task per tier -------------
    tier_rows = db.query(
        "SELECT tier, COUNT(*) c FROM task WHERE tier IS NOT NULL GROUP BY tier")
    tasks_by_tier = {r["tier"]: r["c"] for r in tier_rows}
    # n. escalation a 'frontier' (proxy di costo): task instradati/finiti su Claude.
    frontier_runs = db.query_one(
        "SELECT COUNT(*) c FROM run WHERE backend='subscription' "
        "AND task_id IS NOT NULL")["c"]

    rate = get_settings().usd_to_eur
    cost_usd = cost_today or 0.0
    snap = {
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
        "tasks_cancelled": count("cancelled"),
        "tasks_blocked": count("blocked"),
        "tasks_by_tier": tasks_by_tier,      # distribuzione per tier (§A.8)
        "frontier_escalations": frontier_runs,  # proxy di costo abbonamento
    }
    # VRAM stimata in uso e stato coda dallo scheduler VRAM-aware (§A.6/A.8).
    if scheduler is not None:
        snap["vram_reserved_mb"] = scheduler.reserved_mb
        snap["vram_budget_mb"] = scheduler.budget_mb
        snap["gpu_present"] = scheduler.gpu_present
        snap["queue_paused"] = scheduler.paused
    return snap


def record_sample(snap: dict) -> None:
    get_db().execute(
        "INSERT INTO usage_sample(ts, active_runs, cost_today, tokens_in, "
        "tokens_out, ollama_queue, pending_approvals) VALUES(?,?,?,?,?,?,?)",
        (utcnow(), snap["active_runs"], snap["cost_today"], 0, 0,
         snap["ollama_queue"], snap["pending_approvals"]),
    )
