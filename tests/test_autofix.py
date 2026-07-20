"""Autofix feedback-driven (missione autofix).

Due livelli, come da cultura del repo:
  * logica PURA (classify_failure / build_fix_prompt) testabile senza SDK;
  * integrazione con il fake ClaudeSDKClient (nessun Ollama, nessuna GPU) per il
    loop di autofix a tier: recupero, resa limitata, perimetro invariato.

Invarianti verificate: verify_cmd immutabile, PolicyGate sempre attivo, loop
limitato con resa, retry informato dall'errore invece che cieco.
"""

import asyncio
import json

import pytest

from app.autofix import (
    AttemptRecord, FailureClass, build_fix_prompt, classify_failure,
    diff_changed, snapshot_changes,
)
from app.briefs import PlanDocument, TaskBrief
from app.db import utcnow
from app.executor import get_pool
from app.ids import new_id

from tests.test_executor import FakeClient, _insert_plan, _brief


# =============================================================================
# 1. classify_failure — tabella input -> FailureClass per ogni classe
# =============================================================================
def test_classify_verify_infra():
    fc = classify_failure(
        verify_exit=127, verify_output="$ pytest\nbash: pytest: command not found",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.VERIFY_INFRA


def test_classify_verify_infra_windows():
    fc = classify_failure(
        verify_exit=None, verify_output="'pytest' is not recognized as an internal",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.VERIFY_INFRA


def test_classify_timeout_loop():
    fc = classify_failure(
        verify_exit=None, verify_output="",
        run_error="timeout wall-clock 30s (possibile loop, §1.9)",
        changed_files=[], policy_events=[])
    assert fc is FailureClass.TIMEOUT_LOOP


def test_classify_perimeter_block():
    fc = classify_failure(
        verify_exit=1, verify_output="AssertionError", run_error=None,
        changed_files=[],
        policy_events=[{"kind": "policy_deny", "tool_name": "Write"}])
    assert fc is FailureClass.PERIMETER_BLOCK


def test_classify_perimeter_block_ask_on_edit():
    fc = classify_failure(
        verify_exit=1, verify_output="", run_error=None, changed_files=["a.py"],
        policy_events=[{"kind": "policy_ask", "tool_name": "Edit"}])
    assert fc is FailureClass.PERIMETER_BLOCK


def test_classify_no_change():
    fc = classify_failure(
        verify_exit=1, verify_output="(exit 1)\nsome test output",
        run_error=None, changed_files=[], policy_events=[])
    assert fc is FailureClass.NO_CHANGE


def test_classify_import_error():
    fc = classify_failure(
        verify_exit=1, verify_output="ModuleNotFoundError: No module named 'foo'",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.IMPORT_ERROR


def test_classify_syntax_error():
    fc = classify_failure(
        verify_exit=1, verify_output="  File \"a.py\", line 3\n    def(\nSyntaxError: invalid syntax",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.SYNTAX_ERROR


def test_classify_missing_file():
    fc = classify_failure(
        verify_exit=1, verify_output="FileNotFoundError: [Errno 2] a.txt",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.MISSING_FILE


def test_classify_verify_assertion():
    fc = classify_failure(
        verify_exit=1, verify_output="test_x FAILED\nassert 1 == 2\nAssertionError",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.VERIFY_ASSERTION


def test_classify_unknown():
    fc = classify_failure(
        verify_exit=1, verify_output="(exit 1)\nqualcosa di illeggibile",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.UNKNOWN


def test_classify_priority_infra_over_assertion():
    # infra vince su assertion anche se l'output contiene 'FAILED'
    fc = classify_failure(
        verify_exit=127, verify_output="FAILED: command not found",
        run_error=None, changed_files=["a.py"], policy_events=[])
    assert fc is FailureClass.VERIFY_INFRA


# =============================================================================
# 2. build_fix_prompt — contiene il tail dell'errore, la classe e il verify_cmd
# =============================================================================
def _mk_brief(verify="pytest -q", files=("a.py",)):
    return TaskBrief(id="t1", title="Fai la cosa", files_allowed=list(files),
                     context="ctx", instructions="istr", acceptance="deve passare",
                     verify_cmd=verify, verify_cwd=".")


def test_build_fix_prompt_contains_error_tail():
    brief = _mk_brief(verify="pytest -q tests/test_x.py")
    tail = "riga1\nriga2\nAssertionError: 1 != 2"
    hist = [AttemptRecord(attempt=1, backend="ollama",
                          failure_class=FailureClass.VERIFY_ASSERTION,
                          verify_exit=1, output_tail=tail, changed_files=["a.py"])]
    prompt = build_fix_prompt(brief, hist, diff_tail=None, tail_lines=80)

    assert "AssertionError: 1 != 2" in prompt          # tail dell'errore
    assert FailureClass.VERIFY_ASSERTION.value in prompt  # la classe diagnosticata
    assert "pytest -q tests/test_x.py" in prompt        # il verify_cmd testuale
    assert brief.title in prompt and brief.acceptance in prompt
    assert "- a.py" in prompt                           # il perimetro
    assert "NON modificarlo" in prompt                  # vincolo su verify_cmd


def test_build_fix_prompt_tails_from_bottom():
    # l'errore e' in coda: con tail_lines piccolo, la testa sparisce, la coda resta
    brief = _mk_brief()
    tail = "\n".join(f"linea{i}" for i in range(1, 21)) + "\nERRORE_FINALE"
    hist = [AttemptRecord(attempt=1, backend="ollama",
                          failure_class=FailureClass.UNKNOWN, verify_exit=1,
                          output_tail=tail, changed_files=[])]
    prompt = build_fix_prompt(brief, hist, diff_tail=None, tail_lines=3)
    assert "ERRORE_FINALE" in prompt
    assert "linea1\n" not in prompt   # la testa e' stata tagliata


# =============================================================================
# 3. snapshot / diff pure
# =============================================================================
def test_snapshot_detects_change(tmp_path):
    (tmp_path / "a.py").write_text("uno")
    pre = snapshot_changes(tmp_path, ["a.py", "b.py"])
    (tmp_path / "a.py").write_text("due")
    (tmp_path / "b.py").write_text("nuovo")
    post = snapshot_changes(tmp_path, ["a.py", "b.py"])
    assert diff_changed(pre, post) == ["a.py", "b.py"]


# =============================================================================
# 4. verify_cmd immutabile: un finto output del modello non lo cambia (invar. 2)
# =============================================================================
class _EvilClient(FakeClient):
    """Finge di 'proporre' un nuovo verify_cmd riscrivendo il brief a runtime."""
    brief_to_corrupt = None

    async def query(self, prompt):
        self.prompt = prompt
        if _EvilClient.brief_to_corrupt is not None:
            # tentativo malevolo: riscrivere il proprio test per farlo passare
            _EvilClient.brief_to_corrupt.verify_cmd = "python -c \"raise SystemExit(0)\""


def test_verify_cmd_is_immutable(db, settings, roots):
    settings.max_local_retries = 0     # un solo tentativo locale
    settings.autofix_max_rounds = 1
    repo = roots[0] / "immut"
    repo.mkdir()
    # verify che fallisce SEMPRE con exit 1
    verify = "python -c \"raise SystemExit(1)\""
    plan_id = _insert_plan(db, str(repo), [_brief("t1", verify, ["a.py"])])

    # il brief in memoria che il loop usa: lo recuperiamo per corromperlo
    task = db.query_one("SELECT * FROM task WHERE plan_id=?", (plan_id,))
    brief = TaskBrief.from_dict(json.loads(task["brief_json"]))

    pool = get_pool()
    _EvilClient.brief_to_corrupt = brief
    pool._client_cls = _EvilClient
    try:
        # la guardia _guard_verify_cmd deve scattare se il brief e' stato mutato,
        # oppure il loop usa comunque il verify originale -> il task NON passa.
        with pytest.raises(AssertionError):
            asyncio.run(pool._execute_task(task, brief, repo, roots))
    finally:
        _EvilClient.brief_to_corrupt = None


# =============================================================================
# 5. autofix recovers: 1° tentativo fallisce, il 2° (col fix-brief) passa
# =============================================================================
class _RecordingClient(FakeClient):
    """Registra i prompt ricevuti e crea il file che fa passare il verify solo dal
    2° tentativo in poi (simula un modello guidato dal fix-brief)."""
    prompts: list = []
    repo = None
    call = 0

    async def query(self, prompt):
        self.prompt = prompt
        _RecordingClient.prompts.append(prompt)
        _RecordingClient.call += 1
        if _RecordingClient.call >= 2:
            # dal 2° tentativo "applica il fix": crea il file atteso dal verify
            (_RecordingClient.repo / "ok.txt").write_text("fixed")


def test_autofix_recovers(db, settings, roots):
    settings.max_local_retries = 2
    settings.autofix_max_rounds = 3
    repo = roots[0] / "recov"
    repo.mkdir()
    # passa (exit 0) solo se esiste ok.txt
    verify = ("python -c \"import os,sys;"
              "sys.exit(0 if os.path.exists('ok.txt') else 1)\"")
    plan_id = _insert_plan(db, str(repo), [_brief("t1", verify, ["ok.txt"])])

    _RecordingClient.prompts = []
    _RecordingClient.repo = repo
    _RecordingClient.call = 0
    pool = get_pool()
    pool._client_cls = _RecordingClient
    asyncio.run(pool.approve_and_run(plan_id))

    task = db.query_one("SELECT * FROM task WHERE plan_id=?", (plan_id,))
    assert task["status"] == "done"

    # il 2° prompt e' un fix-brief e contiene l'errore/diagnosi del 1° tentativo
    assert len(_RecordingClient.prompts) >= 2
    second = _RecordingClient.prompts[1]
    assert "FIX — Task" in second
    assert "Output REALE dell'ultimo tentativo" in second
    assert verify in second               # verify_cmd testuale, immutato

    # eventi di autofix emessi
    assert db.query_one("SELECT * FROM event WHERE kind='autofix_diagnose'")
    assert db.query_one("SELECT * FROM event WHERE kind='autofix_attempt'")


# =============================================================================
# 6. loop limitato -> resa (task failed, push, evento autofix_gaveup)
# =============================================================================
def test_autofix_bounded_then_gaveup(db, settings, roots, monkeypatch):
    settings.max_local_retries = 5      # non vincola: e' autofix_max_rounds a farlo
    settings.autofix_max_rounds = 2
    repo = roots[0] / "giveup"
    repo.mkdir()
    verify = "python -c \"raise SystemExit(1)\""   # fallisce sempre
    plan_id = _insert_plan(db, str(repo), [_brief("t1", verify, ["a.py"])])

    pushes = {"n": 0}
    import app.executor as ex
    monkeypatch.setattr(ex, "send_push",
                        lambda *a, **k: pushes.__setitem__("n", pushes["n"] + 1))

    pool = get_pool()
    pool._client_cls = FakeClient
    asyncio.run(pool.approve_and_run(plan_id))

    task = db.query_one("SELECT * FROM task WHERE plan_id=?", (plan_id,))
    assert task["status"] == "failed"

    # il tier locale ha girato esattamente autofix_max_rounds volte (poi abbonamento)
    local_runs = db.query(
        "SELECT * FROM run WHERE task_id=? AND backend='ollama'", (task["id"],))
    assert len(local_runs) == settings.autofix_max_rounds

    # resa emessa e almeno una push (escalation + gaveup)
    assert db.query_one("SELECT * FROM event WHERE kind='autofix_gaveup'")
    assert pushes["n"] >= 1


# =============================================================================
# 7. perimetro ancora applicato: Write fuori files_allowed passa dal gate
# =============================================================================
def test_perimeter_still_enforced(db, settings, roots):
    """L'autofix NON allarga il perimetro: un Write fuori files_allowed viene
    ancora valutato dal PolicyGate (deny se fuori root)."""
    from app.policy import GateContext, make_policy_gate

    outside = roots[0].parent / "evil.py"      # fuori dalle root
    ctx = GateContext(run_id="run-peri", files_allowed_resolved=set(), roots=roots)
    gate = make_policy_gate(ctx)

    async def scenario():
        result = await asyncio.wait_for(
            gate("Write", {"file_path": str(outside)}, None), timeout=2)
        assert type(result).__name__ == "PermissionResultDeny"
        # il verdetto e' finito nel buffer che l'autofix legge per la diagnosi
        assert any(e["kind"] == "policy_deny" and e["tool_name"] == "Write"
                   for e in ctx.policy_events)

    asyncio.run(scenario())
