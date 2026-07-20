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
from dataclasses import dataclass
from pathlib import Path

from .autofix import (
    AttemptRecord, FailureClass, build_fix_prompt, classify_failure,
    diff_changed, git_diff_tail, snapshot_changes,
)
from .backends import make_executor_options
from .briefs import TaskBrief
from .config import get_settings
from .db import get_db, utcnow
from .events import get_bus
from .ids import new_id
from .policy import GateContext, make_policy_gate
from .push import send_push
from .security import resolve_within_roots, PathNotAllowed


@dataclass
class _Tier:
    """Un livello della scala di autofix (missione autofix).

    backend: "ollama" | "subscription"; model: override del modello (None = default
    del backend); is_local: True per i tier Ollama; is_escalation: True SOLO per il
    tier abbonamento (mantiene l'evento 'escalation' + push legacy, §4.3).
    """
    backend: str
    model: str | None
    label: str            # "ollama" | "ollama:<model>" | "subscription"
    is_local: bool
    rounds: int
    is_escalation: bool


class ExecutorPool:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.sem = asyncio.Semaphore(self.settings.max_local_concurrency)
        self._done_events: dict[str, asyncio.Event] = {}
        self._task_ok: dict[str, bool] = {}
        self.pushes = {"pushes": 0}  # metrica qualita' piano (§3.2), esposta in stats
        self.queue_depth = 0
        # classe client iniettabile (i test ne passano una finta; a runtime e' l'SDK)
        self._client_cls = None

    def _get_client_cls(self):
        if self._client_cls is None:
            from claude_agent_sdk import ClaudeSDKClient
            self._client_cls = ClaudeSDKClient
        return self._client_cls

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

        # crea la cartella del repo se non esiste (e' dentro le root): evita
        # WinError 267 quando la CLI parte con un cwd inesistente.
        repo_resolved.mkdir(parents=True, exist_ok=True)

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

    def _autofix_tiers(self, brief: TaskBrief) -> list[_Tier]:
        """La scala di autofix (missione autofix): Ollama primario -> eventuali
        modelli locali piu' forti -> abbonamento (l'escalation esistente).

        I round del tier locale sono subordinati a max_local_retries: senza le
        nuove env il comportamento resta quello attuale (Ollama -> retry ->
        escalation), solo informato dall'errore invece che cieco.
        """
        s = self.settings
        local_rounds = max(1, min(s.autofix_max_rounds, s.max_local_retries + 1))
        tiers = [_Tier("ollama", None, "ollama", True, local_rounds, False)]
        for m in s.autofix_local_tiers:
            tiers.append(_Tier("ollama", m, f"ollama:{m}", True, local_rounds, False))
        # tier abbonamento: 1 tentativo, come l'escalation di oggi (§3.5/M4).
        tiers.append(_Tier("subscription", None, "subscription", False, 1, True))
        return tiers

    async def _execute_task(self, task_row, brief: TaskBrief, repo: Path,
                            roots) -> bool:
        db = get_db()
        bus = get_bus()
        task_id = task_row["id"]

        # INVARIANTE 2: verify_cmd immutabile nel loop. Lo catturiamo qui e lo
        # riverifichiamo prima di ogni esecuzione: un modello non deve poter
        # riscrivere il proprio test per farlo passare.
        original_verify_cmd = brief.verify_cmd

        # risolve e valida il perimetro files_allowed (regola 4.3) allo start.
        # INVARIANTE 3: il perimetro resta questo per TUTTI i tentativi.
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

        history: list[AttemptRecord] = []
        attempt = 0                      # contatore globale dei tentativi
        tiers = self._autofix_tiers(brief)
        prev_label: str | None = None

        for tier in tiers:
            if prev_label is not None:
                await bus.emit(None, "autofix_escalate_tier", {
                    "task_id": task_id, "from": prev_label, "to": tier.label,
                })
            if tier.is_escalation:
                # escalation esistente (§3.5/M4): evento + push col testo attuale.
                db.execute("UPDATE task SET status='escalated' WHERE id=?", (task_id,))
                await bus.emit(None, "escalation", {
                    "task_id": task_id,
                    "reason": "MAX_LOCAL_RETRIES esaurito su Ollama",
                })
                send_push("Argo · escalation",
                          f"Task '{brief.title}' passato all'abbonamento", url="/")
            prev_label = tier.label

            for _ in range(tier.rounds):
                attempt += 1

                # reset opzionale tra i tentativi (§6): riparte pulito sui soli
                # files_allowed se il repo e' git.
                if history and self.settings.autofix_reset_between_attempts:
                    self._git_reset_perimeter(repo, brief.files_allowed)

                # INVARIANTE 2 (guardia esplicita): il verify_cmd non e' cambiato.
                self._guard_verify_cmd(brief, original_verify_cmd)

                # tentativo 1: prompt originale (invariato). Poi: fix-brief con la
                # storia completa dei fallimenti (missione autofix).
                if not history:
                    prompt = _executor_prompt(brief)
                else:
                    diff_tail = git_diff_tail(
                        repo, brief.files_allowed,
                        self.settings.autofix_diff_tail_lines)
                    prompt = build_fix_prompt(
                        brief, history, diff_tail,
                        self.settings.autofix_diff_tail_lines)

                status = "escalated" if tier.is_escalation else "running"
                db.execute(
                    "UPDATE task SET status=?, backend=?, attempts=?, "
                    "autofix_round=? WHERE id=?",
                    (status, tier.label, attempt, attempt, task_id),
                )

                pre = snapshot_changes(repo, brief.files_allowed)
                run_id, run_error, policy_events = await self._run_once(
                    task_id, brief, repo, files_allowed, roots,
                    tier.backend, tier.model, prompt, attempt)
                post = snapshot_changes(repo, brief.files_allowed)
                changed_files = diff_changed(pre, post)

                db.execute("UPDATE task SET status='verifying' WHERE id=?", (task_id,))
                # verify_cmd puo' durare minuti: in un thread, altrimenti blocca il
                # solo event loop del processo (§2: un solo processo asyncio).
                # INVARIANTE 1: e' _run_verify (exit code) a decidere, sempre.
                self._guard_verify_cmd(brief, original_verify_cmd)
                passed, output = await asyncio.to_thread(
                    self._run_verify, brief, repo, roots)
                verify_exit = _parse_verify_exit(output)
                db.execute("UPDATE task SET verify_output=? WHERE id=?",
                           (output, task_id))
                await bus.emit(None, "verify_result", {
                    "task_id": task_id, "passed": passed,
                    "output_tail": output[-400:],
                })
                await bus.emit(None, "autofix_attempt", {
                    "task_id": task_id, "attempt": attempt,
                    "backend": tier.label, "model": tier.model, "passed": passed,
                })

                if passed:
                    db.execute("UPDATE task SET status='done' WHERE id=?", (task_id,))
                    return True

                # diagnosi deterministica del fallimento (§3.1). Non ci si fida
                # MAI dell'autovalutazione del modello.
                fc = classify_failure(
                    verify_exit=verify_exit, verify_output=output,
                    run_error=run_error, changed_files=changed_files,
                    policy_events=policy_events)
                db.execute("UPDATE task SET failure_class=? WHERE id=?",
                           (fc.value, task_id))
                db.execute("UPDATE run SET failure_class=? WHERE id=?",
                           (fc.value, run_id))
                await bus.emit(run_id, "autofix_diagnose", {
                    "task_id": task_id, "attempt": attempt,
                    "failure_class": fc.value, "output_tail": output[-400:],
                })
                history.append(AttemptRecord(
                    attempt=attempt, backend=tier.label, failure_class=fc,
                    verify_exit=verify_exit, output_tail=output,
                    changed_files=changed_files))

        # RESA (§4.4 della missione): esaurita la scala, il task fallisce.
        last_fc = history[-1].failure_class.value if history else FailureClass.UNKNOWN.value
        last_tail = history[-1].output_tail[-400:] if history else ""
        db.execute("UPDATE task SET status='failed' WHERE id=?", (task_id,))
        await bus.emit(None, "autofix_gaveup", {
            "task_id": task_id, "rounds": attempt, "last_failure_class": last_fc,
        })
        send_push(
            "Argo · autofix arreso",
            f"Task '{brief.title}' fallito dopo {attempt} tentativi "
            f"({last_fc}). {last_tail[-160:]}",
            url="/")
        return False

    def _guard_verify_cmd(self, brief: TaskBrief, original: str) -> None:
        """INVARIANTE 2: il verify_cmd usato dal loop e' SEMPRE quello del brief,
        mai un output del modello. Se qualcosa lo ha mutato, e' un bug: fermati."""
        if brief.verify_cmd != original:
            raise AssertionError(
                "verify_cmd mutato durante il loop di autofix: invariante violata "
                f"(atteso {original!r}, trovato {brief.verify_cmd!r})")

    def _git_reset_perimeter(self, repo: Path, files_allowed: list[str]) -> None:
        """`git checkout --` sui soli files_allowed (§6). Non-distruttivo altrove."""
        if not (repo / ".git").exists():
            return
        try:
            subprocess.run(
                ["git", "checkout", "--", *files_allowed],
                cwd=str(repo), capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # best-effort: se il reset non riesce, si prosegue sul parziale

    async def _run_once(self, task_id: str, brief: TaskBrief, repo: Path,
                        files_allowed, roots, backend: str, model: str | None,
                        prompt: str, attempt: int) -> tuple[str, str | None, list]:
        """Guida UN tentativo. Ritorna (run_id, error, policy_events) per la
        diagnosi in `_execute_task`. Il `prompt` e il `model` arrivano dal
        chiamante (l'autofix decide se e' il prompt originale o un fix-brief)."""
        db = get_db()
        bus = get_bus()
        run_id = new_id("run")
        resolved_model = model or (
            self.settings.ollama_model if backend == "ollama"
            else self.settings.subscription_model or "default")
        db.execute(
            "INSERT INTO run(id, task_id, backend, model, status, started_at, "
            "attempt) VALUES(?,?,?,?,?,?,?)",
            (run_id, task_id, backend, resolved_model, "running", utcnow(), attempt),
        )

        gate_ctx = GateContext(run_id=run_id, files_allowed_resolved=files_allowed,
                               roots=roots, push_counter=self.pushes)
        can_use_tool = make_policy_gate(gate_ctx)
        options = make_executor_options(
            self.settings, str(repo), can_use_tool, brief.max_turns, backend, model)

        captured = {"cost": 0.0, "turns": 0, "session_id": None}
        error = None
        try:
            # timeout wall-clock (§1.9): un executor locale incastrato non e' "lento".
            await asyncio.wait_for(
                self._drive_client(self._get_client_cls(), options, prompt,
                                   run_id, bus, captured),
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
            ("error" if error else "done", captured["cost"], captured["turns"],
             captured["session_id"], utcnow(), error, run_id),
        )
        return run_id, error, gate_ctx.policy_events

    async def _drive_client(self, ClientCls, options, prompt, run_id, bus, captured):
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
                        captured["session_id"] = sid
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
                    captured["turns"] = getattr(msg, "num_turns", 0) or 0
                    captured["cost"] = getattr(msg, "total_cost_usd", 0.0) or 0.0
                    await bus.emit(run_id, "result", {
                        "subtype": getattr(msg, "subtype", None),
                        "is_error": getattr(msg, "is_error", None),
                        "turns": captured["turns"],
                        "cost_usd": captured["cost"],
                    })

    def _run_verify(self, brief: TaskBrief, repo: Path, roots) -> tuple[bool, str]:
        """Esegue verify_cmd. L'exit code e' il fatto (§5.2). Difesa in profondita':
        ricontrolla il comando e confina verify_cwd dentro le root (4.3)."""
        from .policy import is_dangerous_bash
        if is_dangerous_bash(brief.verify_cmd):
            return False, f"verify_cmd distruttivo rifiutato: {brief.verify_cmd!r}"
        try:
            cwd = resolve_within_roots((repo / brief.verify_cwd), roots)
        except PathNotAllowed as e:
            return False, f"verify_cwd fuori dalle root: {e}"
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


def _parse_verify_exit(output: str) -> int | None:
    """Estrae l'exit code dall'output di `_run_verify` ('(exit N)'). None se il
    verify non e' nemmeno partito (timeout/errore infra: nessun exit code)."""
    import re
    m = re.search(r"\(exit (-?\d+)\)", output or "")
    return int(m.group(1)) if m else None


def _executor_prompt(brief: TaskBrief) -> str:
    return (
        f"# Task: {brief.title}\n\n"
        f"## Contesto\n{brief.context}\n\n"
        f"## Istruzioni (seguile alla lettera)\n{brief.instructions}\n\n"
        f"## File che puoi toccare (SOLO questi)\n"
        + "\n".join(f"- {f}" for f in brief.files_allowed)
        + f"\n\n## Criterio di accettazione\n{brief.acceptance}\n"
    )


_pool: ExecutorPool | None = None


def get_pool() -> ExecutorPool:
    global _pool
    if _pool is None:
        _pool = ExecutorPool()
    return _pool
