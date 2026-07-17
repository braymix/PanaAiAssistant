"""Executor pool + orchestrazione (M4).

- coda con depends_on e MAX_LOCAL_CONCURRENCY (GPU-bound, §7 M4);
- un ClaudeSDKClient per task, env->Ollama (§1.2);
- timeout wall-clock (§1.9: puo' loopare);
- PolicyGate (§3.2) come can_use_tool: dentro files_allowed -> auto, fuori -> push;
- l'orchestratore esegue verify_cmd LUI STESSO: l'exit code e' il fatto (§5.2),
  MAI l'opinione del modello;
- retry fino a MAX_LOCAL_RETRIES, poi escalation all'abbonamento.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from .backends import make_executor_options
from .briefs import TaskBrief
from .config import get_settings
from .db import get_db, utcnow
from .events import get_bus
from .ids import new_id
from .policy import GateContext, make_policy_gate
from .push import send_push
from .security import resolve_within_roots, PathNotAllowed


class ExecutorPool:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.sem = asyncio.Semaphore(self.settings.max_local_concurrency)
        self._done_events: dict[str, asyncio.Event] = {}
        self._task_ok: dict[str, bool] = {}
        self.pushes = {"pushes": 0}  # metrica qualita' piano (§3.2), esposta in stats
        self.queue_depth = 0

    # --- ingresso: il tasto VIA ha gia' creato il piano; qui parte l'esecuzione --
    async def approve_and_run(self, plan_id: str) -> None:
        db = get_db()
        bus = get_bus()
        plan_row = db.query_one("SELECT * FROM plan_document WHERE id=?", (plan_id,))
        if not plan_row:
            raise ValueError(f"plan {plan_id} inesistente")

        raw = json.loads(plan_row["raw_json"])
        repo_path = raw.get("repo_path", "")
        roots = self.settings.resolved_roots()

        # regola 4.3: valida repo_path e OGNI files_allowed ALLO START, non solo
        # alla creazione. Se qualcosa e' fuori dalle root, il piano non parte.
        try:
            repo_resolved = resolve_within_roots(repo_path, roots)
        except PathNotAllowed as e:
            db.execute("UPDATE plan_document SET status='failed' WHERE id=?", (plan_id,))
            await bus.emit(None, "error", {"plan_id": plan_id, "detail": str(e)})
            raise

        # il tasto VIA E' l'approvazione umana (§3.2): timbrala.
        db.execute(
            "UPDATE plan_document SET status='executing', approved_at=? WHERE id=?",
            (utcnow(), plan_id),
        )

        task_rows = db.query(
            "SELECT * FROM task WHERE plan_id=? ORDER BY seq ASC", (plan_id,)
        )
        # mappa brief_id (dentro il json) -> task db id, per risolvere depends_on
        briefs = {r["id"]: TaskBrief.from_dict(json.loads(r["brief_json"]))
                  for r in task_rows}
        brief_id_to_dbid = {
            TaskBrief.from_dict(json.loads(r["brief_json"])).id: r["id"]
            for r in task_rows
        }
        for r in task_rows:
            self._done_events[r["id"]] = asyncio.Event()

        self.queue_depth = len(task_rows)
        coros = [
            self._run_with_deps(r, briefs[r["id"]], repo_resolved, roots,
                                brief_id_to_dbid)
            for r in task_rows
        ]
        await asyncio.gather(*coros)

        # stato finale del piano
        remaining = db.query(
            "SELECT status FROM task WHERE plan_id=?", (plan_id,))
        statuses = [x["status"] for x in remaining]
        final = "done" if all(s == "done" for s in statuses) else "failed"
        db.execute("UPDATE plan_document SET status=? WHERE id=?", (final, plan_id))
        await bus.emit(None, "plan_done", {"plan_id": plan_id, "status": final})

    async def _run_with_deps(self, task_row, brief: TaskBrief, repo: Path,
                             roots, brief_id_to_dbid) -> None:
        db = get_db()
        # attende le dipendenze
        for dep_brief_id in brief.depends_on:
            dep_dbid = brief_id_to_dbid.get(dep_brief_id)
            if dep_dbid and dep_dbid in self._done_events:
                await self._done_events[dep_dbid].wait()
                if not self._task_ok.get(dep_dbid, False):
                    # dipendenza fallita: questo task non parte
                    db.execute(
                        "UPDATE task SET status='failed', verify_output=? WHERE id=?",
                        (f"dipendenza {dep_brief_id} fallita", task_row["id"]),
                    )
                    self._task_ok[task_row["id"]] = False
                    self._done_events[task_row["id"]].set()
                    self.queue_depth -= 1
                    return

        async with self.sem:
            ok = await self._execute_task(task_row, brief, repo, roots)
        self._task_ok[task_row["id"]] = ok
        self._done_events[task_row["id"]].set()
        self.queue_depth -= 1

    async def _execute_task(self, task_row, brief: TaskBrief, repo: Path,
                            roots) -> bool:
        db = get_db()
        task_id = task_row["id"]

        # risolve e valida il perimetro files_allowed (regola 4.3) allo start.
        try:
            files_allowed = {
                resolve_within_roots((repo / f), roots) for f in brief.files_allowed
            }
        except PathNotAllowed as e:
            db.execute(
                "UPDATE task SET status='failed', verify_output=? WHERE id=?",
                (f"files_allowed fuori dalle root: {e}", task_id),
            )
            return False

        backend = "ollama"
        attempts = 0
        max_attempts = self.settings.max_local_retries + 1

        while attempts < max_attempts:
            attempts += 1
            db.execute(
                "UPDATE task SET status='running', backend=?, attempts=? WHERE id=?",
                (backend, attempts, task_id),
            )
            await self._run_once(task_id, brief, repo, files_allowed, roots, backend)

            db.execute("UPDATE task SET status='verifying' WHERE id=?", (task_id,))
            passed, output = self._run_verify(brief, repo)
            db.execute("UPDATE task SET verify_output=? WHERE id=?", (output, task_id))
            await get_bus().emit(None, "verify_result", {
                "task_id": task_id, "passed": passed,
                "output_tail": output[-400:],
            })

            if passed:
                db.execute("UPDATE task SET status='done' WHERE id=?", (task_id,))
                return True

            if attempts >= max_attempts and backend == "ollama":
                # escalation all'abbonamento (§3.5/M4)
                backend = "subscription"
                attempts = 0
                max_attempts = 1
                db.execute("UPDATE task SET status='escalated' WHERE id=?", (task_id,))
                await get_bus().emit(None, "escalation", {
                    "task_id": task_id,
                    "reason": "MAX_LOCAL_RETRIES esaurito su Ollama",
                })
                send_push("Argo · escalation",
                          f"Task '{brief.title}' passato all'abbonamento", url="/")

        db.execute("UPDATE task SET status='failed' WHERE id=?", (task_id,))
        return False

    async def _run_once(self, task_id: str, brief: TaskBrief, repo: Path,
                        files_allowed, roots, backend: str) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        db = get_db()
        bus = get_bus()
        run_id = new_id("run")
        model = (self.settings.ollama_model if backend == "ollama"
                 else self.settings.subscription_model or "default")
        db.execute(
            "INSERT INTO run(id, task_id, backend, model, status, started_at) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, task_id, backend, model, "running", utcnow()),
        )

        gate_ctx = GateContext(run_id=run_id, files_allowed_resolved=files_allowed,
                               roots=roots, push_counter=self.pushes)
        can_use_tool = make_policy_gate(gate_ctx)
        options = make_executor_options(
            self.settings, str(repo), can_use_tool, brief.max_turns, backend)

        prompt = _executor_prompt(brief)
        cost = 0.0
        turns = 0
        session_id = None
        error = None
        try:
            # timeout wall-clock (§1.9): un executor locale incastrato non e' "lento".
            await asyncio.wait_for(
                self._drive_client(ClaudeSDKClient, options, prompt, run_id, bus,
                                   lambda **k: _capture(k)),
                timeout=brief.timeout_s or self.settings.local_task_timeout_s,
            )
        except asyncio.TimeoutError:
            error = f"timeout wall-clock {brief.timeout_s}s (possibile loop, §1.9)"
            await bus.emit(run_id, "error", {"detail": error})
        except Exception as e:  # noqa: BLE001 — qualsiasi fallimento va MOSTRATO (4.8)
            error = f"{type(e).__name__}: {e}"
            await bus.emit(run_id, "error", {"detail": error})

        db.execute(
            "UPDATE run SET status=?, cost_usd=?, turns=?, session_id=?, ended_at=?, "
            "error=? WHERE id=?",
            ("error" if error else "done", cost, turns, session_id, utcnow(),
             error, run_id),
        )

    async def _drive_client(self, ClientCls, options, prompt, run_id, bus, sink):
        db = get_db()
        async with ClientCls(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                name = type(msg).__name__
                data = getattr(msg, "data", None)
                if getattr(msg, "subtype", None) == "init" and isinstance(data, dict):
                    sid = data.get("session_id")
                    if sid:
                        # §1.7: persisti il session_id SUBITO (senza: niente resume)
                        db.execute("UPDATE run SET session_id=? WHERE id=?",
                                   (sid, run_id))
                if name == "AssistantMessage":
                    for block in getattr(msg, "content", []) or []:
                        bt = type(block).__name__
                        if bt == "TextBlock":
                            await bus.emit(run_id, "assistant_text",
                                           {"text": getattr(block, "text", "")})
                        elif bt == "ToolUseBlock":
                            await bus.emit(run_id, "tool_use", {
                                "name": getattr(block, "name", ""),
                                "input": getattr(block, "input", {}),
                            })
                if name == "ResultMessage":
                    await bus.emit(run_id, "result", {
                        "subtype": getattr(msg, "subtype", None),
                        "is_error": getattr(msg, "is_error", None),
                        "turns": getattr(msg, "num_turns", None),
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                    })

    def _run_verify(self, brief: TaskBrief, repo: Path) -> tuple[bool, str]:
        """Esegue verify_cmd. L'exit code e' il fatto (§5.2)."""
        cwd = (repo / brief.verify_cwd).resolve()
        try:
            proc = subprocess.run(
                brief.verify_cmd, shell=True, cwd=str(cwd),
                capture_output=True, text=True, timeout=brief.timeout_s or 900,
            )
            output = f"$ {brief.verify_cmd}\n(exit {proc.returncode})\n"
            output += (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, f"verify_cmd timeout: {brief.verify_cmd}"
        except Exception as e:  # noqa: BLE001
            return False, f"verify_cmd errore: {type(e).__name__}: {e}"


def _executor_prompt(brief: TaskBrief) -> str:
    return (
        f"# Task: {brief.title}\n\n"
        f"## Contesto\n{brief.context}\n\n"
        f"## Istruzioni (seguile alla lettera)\n{brief.instructions}\n\n"
        f"## File che puoi toccare (SOLO questi)\n"
        + "\n".join(f"- {f}" for f in brief.files_allowed)
        + f"\n\n## Criterio di accettazione\n{brief.acceptance}\n"
    )


def _capture(_k):  # placeholder sink, i valori reali finiscono su DB/eventi
    return None


_pool: ExecutorPool | None = None


def get_pool() -> ExecutorPool:
    global _pool
    if _pool is None:
        _pool = ExecutorPool()
    return _pool
