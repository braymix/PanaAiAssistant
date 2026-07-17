"""SQLite in WAL mode. Schema §5.1, verbatim nei nomi delle colonne.

Un solo processo, utente singolo: una connessione condivisa con un lock per le
scritture e' sufficiente. `event` e' append-only (regola 4.4): niente UPDATE/DELETE.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS project(
  id TEXT PRIMARY KEY, name TEXT, repo_path TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS conversation(
  id TEXT PRIMARY KEY, title TEXT, plan_mode INTEGER, created_at TEXT
);

CREATE TABLE IF NOT EXISTS message(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT, role TEXT, content TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS plan_document(
  id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT, raw_json TEXT,
  cost_usd REAL, approved_at TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS task(
  id TEXT PRIMARY KEY, plan_id TEXT, seq INTEGER, title TEXT,
  brief_json TEXT, status TEXT, backend TEXT,
  attempts INTEGER DEFAULT 0, verify_output TEXT, depends_on TEXT
);

CREATE TABLE IF NOT EXISTS run(
  id TEXT PRIMARY KEY, task_id TEXT, conversation_id TEXT,
  session_id TEXT, backend TEXT, model TEXT,
  status TEXT, cost_usd REAL, turns INTEGER,
  started_at TEXT, ended_at TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, ts TEXT, kind TEXT, payload TEXT
);

CREATE TABLE IF NOT EXISTS approval(
  id TEXT PRIMARY KEY, run_id TEXT, event_id INTEGER,
  tool_name TEXT, tool_input TEXT, status TEXT,
  pushed_at TEXT, decided_at TEXT, reason TEXT, updated_input TEXT
);

CREATE TABLE IF NOT EXISTS push_subscription(
  id TEXT PRIMARY KEY, endpoint TEXT, p256dh TEXT, auth TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_sample(
  ts TEXT, active_runs INTEGER, cost_today REAL,
  tokens_in INTEGER, tokens_out INTEGER, ollama_queue INTEGER,
  pending_approvals INTEGER
);

CREATE INDEX IF NOT EXISTS idx_event_run ON event(run_id, id);
CREATE INDEX IF NOT EXISTS idx_message_conv ON message(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_task_plan ON task(plan_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_task ON run(task_id);
CREATE INDEX IF NOT EXISTS idx_approval_run ON approval(run_id);
"""


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()
        self._init_pragmas()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _init_pragmas(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    # --- primitive --------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        cur = self._conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        cur.close()
        return rows

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- event: SOLO append (regola 4.4) ---------------------------------------
    def append_event(self, run_id: str | None, kind: str, payload: Any) -> int:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        cur = self.execute(
            "INSERT INTO event(run_id, ts, kind, payload) VALUES(?,?,?,?)",
            (run_id, utcnow(), kind, body),
        )
        return int(cur.lastrowid)

    def events_after(self, run_id: str, after_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM event WHERE run_id=? AND id>? ORDER BY id ASC",
            (run_id, after_id),
        )

    def events_all_after(self, after_id: int, limit: int = 500) -> list[sqlite3.Row]:
        # replay globale (dashboard) — regola 4.15 anche sul canale "*"
        return self.query(
            "SELECT * FROM event WHERE id>? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        )

    def close(self) -> None:
        self._conn.close()


_db: Database | None = None


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database non inizializzato: chiama init_db() nello startup")
    return _db


def init_db(path: str | Path) -> Database:
    global _db
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path("") else None
    _db = Database(path)
    return _db


def set_db(db: Database) -> None:
    global _db
    _db = db
