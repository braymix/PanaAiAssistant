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
  id TEXT PRIMARY KEY, title TEXT, plan_mode INTEGER, created_at TEXT,
  mode TEXT DEFAULT 'claudio_codice',  -- unica modalita' chat: "Claudio Codice"
  deleted_at TEXT                  -- soft-delete (ciclo di vita §B.3)
);

CREATE TABLE IF NOT EXISTS app_state(
  key TEXT PRIMARY KEY, value TEXT  -- flag globali persistiti (es. queue paused)
);

CREATE TABLE IF NOT EXISTS message(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT, role TEXT, content TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS plan_document(
  id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT, raw_json TEXT,
  cost_usd REAL, approved_at TEXT, created_at TEXT,
  deleted_at TEXT                  -- soft-delete (ciclo di vita §B.3)
);

CREATE TABLE IF NOT EXISTS task(
  id TEXT PRIMARY KEY, plan_id TEXT, seq INTEGER, title TEXT,
  brief_json TEXT, status TEXT, backend TEXT,
  attempts INTEGER DEFAULT 0, verify_output TEXT, depends_on TEXT,
  autofix_round INTEGER DEFAULT 0, failure_class TEXT, tier TEXT,
  deleted_at TEXT                  -- soft-delete (ciclo di vita §B.3)
);

CREATE TABLE IF NOT EXISTS run(
  id TEXT PRIMARY KEY, task_id TEXT, conversation_id TEXT,
  session_id TEXT, backend TEXT, model TEXT,
  status TEXT, cost_usd REAL, turns INTEGER,
  started_at TEXT, ended_at TEXT, error TEXT,
  attempt INTEGER, failure_class TEXT
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
        self._migrate()

    def _migrate(self) -> None:
        # aggiunte additive di colonne su DB gia' esistenti (CREATE IF NOT EXISTS
        # non le aggiunge). Ignora l'errore se la colonna c'e' gia'.
        cols = [r["name"] for r in self.query("PRAGMA table_info(conversation)")]
        if "mode" not in cols:
            self.execute(
                "ALTER TABLE conversation ADD COLUMN mode TEXT DEFAULT 'claudio_codice'")

        # --- autofix (missione autofix): traccia round e diagnosi sul task e la
        #     classe di fallimento per-tentativo sul run (append-only).
        task_cols = [r["name"] for r in self.query("PRAGMA table_info(task)")]
        if "autofix_round" not in task_cols:
            self.execute(
                "ALTER TABLE task ADD COLUMN autofix_round INTEGER DEFAULT 0")
        if "failure_class" not in task_cols:
            self.execute("ALTER TABLE task ADD COLUMN failure_class TEXT")
        # --- settorializzazione (routing per peso): tier scelto dal router.
        if "tier" not in task_cols:
            self.execute("ALTER TABLE task ADD COLUMN tier TEXT")

        # --- ciclo di vita (§B): soft-delete su conversation/plan/task ----------
        for table in ("conversation", "plan_document", "task"):
            cols = [r["name"] for r in self.query(f"PRAGMA table_info({table})")]
            if "deleted_at" not in cols:
                self.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT")

        run_cols = [r["name"] for r in self.query("PRAGMA table_info(run)")]
        if "attempt" not in run_cols:
            self.execute("ALTER TABLE run ADD COLUMN attempt INTEGER")
        if "failure_class" not in run_cols:
            self.execute("ALTER TABLE run ADD COLUMN failure_class TEXT")

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

    # --- app_state: flag globali persistiti (ciclo di vita §B.1) ---------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM app_state WHERE key=?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

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
