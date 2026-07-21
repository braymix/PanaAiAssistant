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
import os
import signal
import subprocess
from pathlib import Path

from .autofix import (
    AttemptRecord, FailureClass, build_fix_prompt, classify_failure,
    diff_changed, git_diff_tail, snapshot_changes,
)
from .backends import make_executor_options
from .briefs import TaskBrief
from .config import get_settings, patience_policy
from .db import get_db, utcnow
from .events import get_bus
from .hardware import get_profile
from .ids import new_id
from .policy import GateContext, make_policy_gate
from .push import send_push
from .router import (
    RouteDecision, estimate_complexity, next_tier, route, tier_warnings,
)
from .scheduler import VramScheduler
from .security import resolve_within_roots, PathNotAllowed


class ExecutorPool:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._done_events: dict[str, asyncio.Event] = {}
        self._task_ok: dict[str, bool] = {}
        self.pushes = {"pushes": 0}  # metrica qualita' piano (§3.2), esposta in stats
        self.queue_depth = 0
        # classe client iniettabile (i test ne passano una finta; a runtime e' l'SDK)
        self._client_cls = None
        # settorializzazione (§A.6): scheduler VRAM-aware, profilo e pazienza lazy.
        self._scheduler: VramScheduler | None = None
        self._profile_cache = None
        self._config_warned = False
        # --- ciclo di vita (§B.2): registro dei task vivi per annullarli --------
        self._plan_tasks: dict[str, list[asyncio.Task]] = {}
        self._task_coros: dict[str, asyncio.Task] = {}
        self._verify_procs: dict[str, subprocess.Popen] = {}
        self._paused_restored = False

    def _get_client_cls(self):
        if self._client_cls is None:
            from claude_agent_sdk import ClaudeSDKClient
            self._client_cls = ClaudeSDKClient
        return self._client_cls

    # --- settorializzazione: profilo hardware, scheduler, pazienza --------------
    def _profile(self):
        if self._profile_cache is None:
            self._profile_cache = get_profile(self.settings)
        return self._profile_cache

    @property
    def scheduler(self) -> VramScheduler:
        if self._scheduler is None:
            self._scheduler = VramScheduler.from_profile(self._profile(), self.settings)
        return self._scheduler

    def _policy(self):
        return patience_policy(self.settings.patience)

    async def _warn_config_once(self) -> None:
        """Emette i config_warning (§A.8) una sola volta: modelli del registro non
        installati in Ollama."""
        if self._config_warned:
            return
        self._config_warned = True
        for detail in tier_warnings(self._profile(), self.settings.model_tiers):
            await get_bus().emit(None, "config_warning", {"detail": detail})

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
        await self._warn_config_once()  # §A.8: modelli del registro non installati

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

        # ripristina lo stato di pausa persistito PRIMA di ammettere task (§B.2).
        await self._restore_paused_once()

        self.queue_depth = len(task_rows)
        # ciclo di vita (§B.2): ogni task e' un asyncio.Task REGISTRATO, cosi' e'
        # annullabile singolarmente; il piano tiene la lista per cancel_plan.
        tasks: list[asyncio.Task] = []
        for r in task_rows:
            t = asyncio.ensure_future(
                self._run_with_deps(r, briefs[r["id"]], repo_resolved, roots,
                                    brief_id_to_dbid))
            self._task_coros[r["id"]] = t
            tasks.append(t)
        self._plan_tasks[plan_id] = tasks
        try:
            # return_exceptions: un annullamento (CancelledError) di un task NON
            # deve far esplodere l'intero gather e lasciare gli altri appesi.
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._plan_tasks.pop(plan_id, None)
            for r in task_rows:
                self._task_coros.pop(r["id"], None)

        # stato finale del piano. Se e' stato annullato, resta 'cancelled'.
        plan_now = db.query_one(
            "SELECT status FROM plan_document WHERE id=?", (plan_id,))
        if plan_now and plan_now["status"] == "cancelled":
            await bus.emit(None, "plan_done",
                           {"plan_id": plan_id, "status": "cancelled"})
            return
        remaining = db.query(
            "SELECT status FROM task WHERE plan_id=?", (plan_id,))
        statuses = [x["status"] for x in remaining]
        if any(s == "cancelled" for s in statuses):
            final = "cancelled"
        elif all(s == "done" for s in statuses):
            final = "done"
        else:
            final = "failed"
        db.execute("UPDATE plan_document SET status=? WHERE id=?", (final, plan_id))
        await bus.emit(None, "plan_done", {"plan_id": plan_id, "status": final})

    async def _run_with_deps(self, task_row, brief: TaskBrief, repo: Path,
                             roots, brief_id_to_dbid) -> None:
        db = get_db()
        bus = get_bus()
        task_id = task_row["id"]
        try:
            # veto pre-esecuzione (§B.1): un task 'blocked' non viene preso in carico.
            if self._db_status(task_id) == "blocked":
                await bus.emit(None, "task_blocked_skip", {"task_id": task_id})
                self._finish(task_id, ok=False)
                return

            # attende le dipendenze
            for dep_brief_id in brief.depends_on:
                dep_dbid = brief_id_to_dbid.get(dep_brief_id)
                if dep_dbid and dep_dbid in self._done_events:
                    await self._done_events[dep_dbid].wait()
                    if not self._task_ok.get(dep_dbid, False):
                        # INVARIANTE §B.6.2: i dipendenti di un task
                        # cancelled/failed/blocked NON partono. Erediti lo stato:
                        # 'cancelled' se la dipendenza e' stata annullata/bloccata.
                        dep_status = self._db_status(dep_dbid)
                        inherit = ("cancelled"
                                   if dep_status in ("cancelled", "blocked")
                                   else "failed")
                        db.execute(
                            "UPDATE task SET status=?, verify_output=? WHERE id=?",
                            (inherit,
                             f"dipendenza {dep_brief_id} non riuscita ({dep_status})",
                             task_id),
                        )
                        self._finish(task_id, ok=False)
                        return

            # ri-controlla il veto DOPO le dipendenze (potrebbe essere stato
            # bloccato mentre attendeva): sempre pre-esecuzione.
            if self._db_status(task_id) == "blocked":
                await bus.emit(None, "task_blocked_skip", {"task_id": task_id})
                self._finish(task_id, ok=False)
                return

            # NB: l'ammissione (scheduler VRAM-aware, §A.6) e' DENTRO _execute_task,
            # per-tier: il peso dipende dalla RouteDecision del tier corrente.
            ok = await self._execute_task(task_row, brief, repo, roots)
            self._finish(task_id, ok=ok)
        except asyncio.CancelledError:
            # INVARIANTE §B.6.1: annullamento -> 'cancelled', mai appeso. Lo slot
            # dello scheduler e' gia' rilasciato dai finally interni; qui si
            # garantisce stato, done-event e queue_depth.
            db.execute(
                "UPDATE task SET status='cancelled' WHERE id=? "
                "AND status NOT IN ('done','failed')", (task_id,))
            self._kill_verify(task_id)
            await bus.emit(None, "task_cancelled", {"task_id": task_id})
            self._finish(task_id, ok=False)
            # non ri-sollevo: l'annullamento e' cooperativo e gia' gestito.

    def _finish(self, task_id: str, *, ok: bool) -> None:
        """Chiusura idempotente di un task: done-event + queue_depth una volta."""
        if self._done_events.get(task_id) and self._done_events[task_id].is_set():
            return  # gia' chiuso (es. cancel arrivato mentre finiva)
        self._task_ok[task_id] = ok
        if task_id in self._done_events:
            self._done_events[task_id].set()
        self.queue_depth = max(0, self.queue_depth - 1)

    def _db_status(self, task_id: str) -> str | None:
        row = get_db().query_one("SELECT status FROM task WHERE id=?", (task_id,))
        return row["status"] if row else None

    # --- ciclo di vita: annullamento cooperativo (§B.2) ------------------------
    async def cancel_task(self, task_id: str) -> bool:
        """Annulla un task: uccide il verify se in corso, cancella la coroutine
        (-> CancelledError gestito in _run_with_deps: stato 'cancelled', slot
        rilasciato, done-event settato). Ritorna True se ha agito."""
        db = get_db()
        row = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
        if not row:
            return False
        if row["status"] in ("done", "failed", "cancelled"):
            return False
        # termina un eventuale sottoprocesso verify (§B.2).
        self._kill_verify(task_id)
        t = self._task_coros.get(task_id)
        if t and not t.done():
            t.cancel()  # -> asyncio.CancelledError in _run_with_deps
            return True
        # non in volo (pending/blocked, oppure gia' finito): marca e sblocca i deps.
        db.execute(
            "UPDATE task SET status='cancelled' WHERE id=? "
            "AND status NOT IN ('done','failed')", (task_id,))
        await get_bus().emit(None, "task_cancelled", {"task_id": task_id})
        self._finish(task_id, ok=False)
        return True

    async def cancel_plan(self, plan_id: str) -> None:
        """Annulla un intero piano: tutti i suoi task + il gather. Piano ->
        'cancelled' (§B.2)."""
        db = get_db()
        db.execute(
            "UPDATE plan_document SET status='cancelled' WHERE id=? "
            "AND status NOT IN ('done','failed')", (plan_id,))
        task_rows = db.query("SELECT id FROM task WHERE plan_id=?", (plan_id,))
        for r in task_rows:
            await self.cancel_task(r["id"])
        await get_bus().emit(None, "plan_cancelled", {"plan_id": plan_id})

    async def cancel_all(self) -> int:
        """Annulla TUTTI i piani in volo (riavvio servizi / reset, § sistema).
        Ritorna il numero di piani annullati."""
        plan_ids = list(self._plan_tasks.keys())
        for pid in plan_ids:
            await self.cancel_plan(pid)
        return len(plan_ids)

    # --- pausa/ripresa della coda (§B.2) ---------------------------------------
    async def pause_queue(self) -> None:
        await self.scheduler.pause()
        get_db().set_state("queue_paused", "1")
        await get_bus().emit(None, "queue_paused", {"paused": True})

    async def resume_queue(self) -> None:
        await self.scheduler.resume()
        get_db().set_state("queue_paused", "0")
        await get_bus().emit(None, "queue_resumed", {"paused": False})

    async def _restore_paused_once(self) -> None:
        """Ripristina lo stato di pausa persistito, una sola volta per processo."""
        if self._paused_restored:
            return
        self._paused_restored = True
        if get_db().get_state("queue_paused", "0") == "1":
            self.scheduler.set_paused(True)

    # --- veto pre-esecuzione: block/unblock (§B.1) -----------------------------
    async def block_task(self, task_id: str) -> bool:
        """Mette un veto: il task non verra' preso in carico. Vietato su un task
        gia' in esecuzione (running/verifying) -> il chiamante restituisce 409."""
        db = get_db()
        row = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
        if not row or row["status"] in ("running", "verifying", "done", "escalated"):
            return False
        db.execute("UPDATE task SET status='blocked' WHERE id=?", (task_id,))
        await get_bus().emit(None, "task_blocked", {"task_id": task_id})
        return True

    async def unblock_task(self, task_id: str) -> bool:
        db = get_db()
        row = db.query_one("SELECT status FROM task WHERE id=?", (task_id,))
        if not row or row["status"] != "blocked":
            return False
        db.execute("UPDATE task SET status='pending' WHERE id=?", (task_id,))
        await get_bus().emit(None, "task_unblocked", {"task_id": task_id})
        return True

    # --- verify come sottoprocesso uccidibile (§B.2) ---------------------------
    def _kill_verify(self, task_id: str) -> None:
        proc = self._verify_procs.get(task_id)
        if proc is not None and proc.poll() is None:
            _terminate_process_group(proc)

    def _route_ladder(self, brief: TaskBrief) -> tuple[RouteDecision, list[RouteDecision]]:
        """La scala di autofix E' la scala di escalation del router (§A.5/A.7).

        Parte dal tier ROUTATO (settorializzazione per peso/hardware/pazienza) e
        risale via `next_tier` fino a 'frontier' (subscription). I modelli locali
        che non entrano in VRAM sono gia' esclusi da `available_tiers`.
        """
        profile = self._profile()
        tiers = self.settings.model_tiers
        policy = self._policy()
        headroom = self.settings.vram_headroom_mb

        first = route(brief, profile, tiers, policy, headroom)
        ladder = [first]
        seen = {first.tier}
        cur = first.tier
        while True:
            nxt = next_tier(cur, brief, profile, tiers, policy, headroom)
            if nxt is None or nxt.tier in seen:
                break
            ladder.append(nxt)
            seen.add(nxt.tier)
            cur = nxt.tier
        return first, ladder

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
        first, ladder = self._route_ladder(brief)

        # settorializzazione: registra la decisione di routing (§A.8).
        await bus.emit(None, "route_decision", {
            "task_id": task_id,
            "complexity": brief.complexity or estimate_complexity(brief),
            "criticality": brief.criticality,
            "tier": first.tier, "model": first.model,
            "weight": first.concurrency_weight, "reason": first.reason,
        })
        db.execute("UPDATE task SET tier=? WHERE id=?", (first.tier, task_id))

        # budget di tentativi LOCALI: preserva la semantica autofix esistente
        # (backward compat), condiviso fra i tier locali della scala. Esaurito il
        # budget locale si salta a 'frontier' (subscription).
        local_budget = max(1, min(self.settings.autofix_max_rounds,
                                  self.settings.max_local_retries + 1))
        remaining_local = local_budget
        prev_label: str | None = None

        for decision in ladder:
            is_local = decision.backend == "ollama"
            if is_local and remaining_local <= 0:
                continue  # budget locale esaurito: prova gli eventuali tier sup.
            rounds = decision.autofix_local_rounds if is_local else 1
            if is_local:
                rounds = min(rounds, remaining_local)
            if rounds <= 0:
                continue

            label = decision.tier if is_local else "subscription"
            if prev_label is not None:
                await bus.emit(None, "autofix_escalate_tier", {
                    "task_id": task_id, "from": prev_label, "to": label,
                })
            if not is_local:
                # escalation a 'frontier' (subscription): evento + push (§3.5/M4).
                db.execute("UPDATE task SET status='escalated' WHERE id=?", (task_id,))
                await bus.emit(None, "escalation", {
                    "task_id": task_id,
                    "reason": f"scala locale esaurita -> {decision.tier}",
                })
                send_push("Argo · escalation",
                          f"Task '{brief.title}' passato all'abbonamento", url="/")
            prev_label = label

            # ammissione VRAM-aware per QUESTO tier (§A.6): il peso e' quello del
            # tier corrente; il rilascio e' garantito (anche su cancel, Parte B).
            await self.scheduler.acquire(
                decision.concurrency_weight, is_local=is_local)
            try:
                passed = False
                for _ in range(rounds):
                    attempt += 1
                    if is_local:
                        remaining_local -= 1

                    # reset opzionale tra i tentativi (§6): riparte pulito sui soli
                    # files_allowed se il repo e' git.
                    if history and self.settings.autofix_reset_between_attempts:
                        self._git_reset_perimeter(repo, brief.files_allowed)

                    # INVARIANTE 2 (guardia): il verify_cmd non e' cambiato.
                    self._guard_verify_cmd(brief, original_verify_cmd)

                    # tentativo 1: prompt originale. Poi: fix-brief con la storia.
                    if not history:
                        prompt = _executor_prompt(brief)
                    else:
                        diff_tail = git_diff_tail(
                            repo, brief.files_allowed,
                            self.settings.autofix_diff_tail_lines)
                        prompt = build_fix_prompt(
                            brief, history, diff_tail,
                            self.settings.autofix_diff_tail_lines)

                    status = "escalated" if not is_local else "running"
                    db.execute(
                        "UPDATE task SET status=?, backend=?, tier=?, attempts=?, "
                        "autofix_round=? WHERE id=?",
                        (status, decision.backend, decision.tier, attempt,
                         attempt, task_id),
                    )

                    pre = snapshot_changes(repo, brief.files_allowed)
                    run_id, run_error, policy_events = await self._run_once(
                        task_id, brief, repo, files_allowed, roots,
                        decision.backend, decision.model, prompt, attempt)
                    post = snapshot_changes(repo, brief.files_allowed)
                    changed_files = diff_changed(pre, post)

                    db.execute("UPDATE task SET status='verifying' WHERE id=?",
                               (task_id,))
                    # verify_cmd puo' durare minuti: in un thread, altrimenti blocca
                    # il solo event loop del processo (§2). INVARIANTE 1: decide
                    # _run_verify (exit code), sempre.
                    self._guard_verify_cmd(brief, original_verify_cmd)
                    passed, output = await asyncio.to_thread(
                        self._run_verify, brief, repo, roots, task_id)
                    verify_exit = _parse_verify_exit(output)
                    db.execute("UPDATE task SET verify_output=? WHERE id=?",
                               (output, task_id))
                    await bus.emit(None, "verify_result", {
                        "task_id": task_id, "passed": passed,
                        "output_tail": output[-400:],
                    })
                    await bus.emit(None, "autofix_attempt", {
                        "task_id": task_id, "attempt": attempt,
                        "backend": decision.backend, "model": decision.model,
                        "tier": decision.tier, "passed": passed,
                    })

                    if passed:
                        db.execute("UPDATE task SET status='done' WHERE id=?",
                                   (task_id,))
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
                        attempt=attempt, backend=decision.tier, failure_class=fc,
                        verify_exit=verify_exit, output_tail=output,
                        changed_files=changed_files))
            finally:
                await self.scheduler.release(
                    decision.concurrency_weight, is_local=is_local)

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
        except asyncio.CancelledError:
            # annullamento cooperativo (§B.2): finalizza il run come 'cancelled' e
            # RI-SOLLEVA, cosi' _run_with_deps porta il task a 'cancelled'.
            db.execute(
                "UPDATE run SET status='cancelled', ended_at=?, error=? WHERE id=?",
                (utcnow(), "annullato", run_id))
            raise
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

    def _run_verify(self, brief: TaskBrief, repo: Path, roots,
                    task_id: str | None = None) -> tuple[bool, str]:
        """Esegue verify_cmd. L'exit code e' il fatto (§5.2). Difesa in profondita':
        ricontrolla il comando e confina verify_cwd dentro le root (4.3).

        Ciclo di vita (§B.2): gira come `Popen` in un GRUPPO DI PROCESSO dedicato e
        si registra in `_verify_procs[task_id]`, cosi' `cancel_task` puo' terminare
        l'intero albero (POSIX killpg / Windows taskkill /T). Il timeout wall-clock
        resta invariato."""
        from .policy import is_dangerous_bash
        if is_dangerous_bash(brief.verify_cmd):
            return False, f"verify_cmd distruttivo rifiutato: {brief.verify_cmd!r}"
        try:
            cwd = resolve_within_roots((repo / brief.verify_cwd), roots)
        except PathNotAllowed as e:
            return False, f"verify_cwd fuori dalle root: {e}"

        popen_kwargs: dict = dict(
            cwd=str(cwd), shell=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # gruppo di processo dedicato: permette di uccidere anche i figli (§B.2).
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True  # setsid -> os.killpg

        try:
            proc = subprocess.Popen(brief.verify_cmd, **popen_kwargs)  # noqa: S602
        except Exception as e:  # noqa: BLE001
            return False, f"verify_cmd errore: {type(e).__name__}: {e}"

        if task_id:
            self._verify_procs[task_id] = proc
        try:
            out, err = proc.communicate(timeout=brief.timeout_s or 900)
            output = f"$ {brief.verify_cmd}\n(exit {proc.returncode})\n"
            output += (out or "") + (err or "")
            return proc.returncode == 0, output
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
            return False, f"verify_cmd timeout: {brief.verify_cmd}"
        except Exception as e:  # noqa: BLE001
            return False, f"verify_cmd errore: {type(e).__name__}: {e}"
        finally:
            if task_id:
                self._verify_procs.pop(task_id, None)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Termina l'intero albero del verify (§B.2). POSIX: SIGTERM al process group
    (start_new_session). Windows: `taskkill /T /F` (CREATE_NEW_PROCESS_GROUP).
    Best-effort: un verify gia' morto non e' un errore."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        try:
            proc.kill()  # fallback: almeno il processo diretto
        except (OSError, subprocess.SubprocessError):
            pass


def _parse_verify_exit(output: str) -> int | None:
    """Estrae l'exit code dall'output di `_run_verify` ('(exit N)'). None se il
    verify non e' nemmeno partito (timeout/errore infra: nessun exit code)."""
    import re
    m = re.search(r"\(exit (-?\d+)\)", output or "")
    return int(m.group(1)) if m else None


def _executor_prompt(brief: TaskBrief) -> str:
    return (
        f"# Task: {brief.title}\n\n"
        f"AGISCI ORA usando gli strumenti Write/Edit per scrivere i file qui "
        f"sotto. NON limitarti a descrivere o incollare il contenuto in un "
        f"messaggio: senza usare i tool il task fallisce.\n\n"
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
