"""TEST OBBLIGATORIO M2 (§7): un profilo con Write FUORI da allowed_tools deve
produrre >=1 approvazione pending. Senza questo test, M2 non e' finita.

Copre due livelli:
  1. il contratto di configurazione (§1.8): make_executor_options NON mette mai
     Write/Edit/Bash in allowed_tools;
  2. il comportamento runtime: quando l'SDK chiama can_use_tool per un Write fuori
     dal perimetro, il broker crea un'approvazione `pending`.
"""

import asyncio

from app.backends import make_executor_options, READONLY_TOOLS
from app.config import get_settings
from app.policy import GateContext, make_policy_gate
from app.approvals import get_broker


def test_write_never_in_allowed_tools(settings):
    opts = make_executor_options(settings, cwd=".", can_use_tool=lambda *a: None,
                                 max_turns=10, backend="ollama")
    for shadowed in ("Write", "Edit", "Bash"):
        assert shadowed not in (opts.allowed_tools or []), (
            f"{shadowed} in allowed_tools -> §1.8: il gate non verrebbe mai chiamato")
    assert set(opts.allowed_tools) == set(READONLY_TOOLS)


def test_write_outside_perimeter_produces_pending_approval(db, settings, roots):
    inside_root = roots[0] / "src" / "x.py"
    inside_root.parent.mkdir(parents=True)
    inside_root.write_text("x")

    ctx = GateContext(run_id="run-shadow", files_allowed_resolved=set(),
                      roots=roots)
    gate = make_policy_gate(ctx)

    async def scenario():
        # l'SDK invoca il gate per un Write NON pre-approvato (non e' in allowed_tools)
        task = asyncio.ensure_future(
            gate("Write", {"file_path": str(inside_root)}, None))
        await asyncio.sleep(0.05)

        pending = db.query("SELECT * FROM approval WHERE status='pending'")
        assert len(pending) >= 1, "nessuna approvazione pending: §1.8 violato"
        assert pending[0]["tool_name"] == "Write"
        assert ctx.push_counter.get("pushes", 0) >= 1  # una push per l'eccezione

        # risolvi per non lasciare il task appeso; verifica che diventi 'allowed'
        assert get_broker().resolve(pending[0]["id"], allow=True)
        result = await asyncio.wait_for(task, timeout=1)
        assert type(result).__name__ == "PermissionResultAllow"

    asyncio.run(scenario())


def test_dangerous_bash_denied_at_runtime_without_asking(db, settings, roots):
    """Il gate runtime nega rm -rf senza creare approvazioni (§8, secondo strato)."""
    ctx = GateContext(run_id="run-danger", files_allowed_resolved=set(), roots=roots)
    gate = make_policy_gate(ctx)

    async def scenario():
        result = await asyncio.wait_for(
            gate("Bash", {"command": "rm -rf /"}, None), timeout=2)
        assert type(result).__name__ == "PermissionResultDeny"
        # nessuna approvazione creata (non e' andato al telefono)
        assert db.query("SELECT * FROM approval") == []
        assert ctx.push_counter.get("pushes", 0) == 0
        # evento policy_deny nel log append-only
        assert db.query_one("SELECT * FROM event WHERE kind='policy_deny'") is not None

    asyncio.run(scenario())


def test_approval_timeout_denies(db, settings, roots):
    """Regola 4.6: il timeout NEGA, non permette."""
    inside_root = roots[0] / "y.py"
    inside_root.write_text("x")
    ctx = GateContext(run_id="run-timeout", files_allowed_resolved=set(), roots=roots)
    gate = make_policy_gate(ctx)

    async def scenario():
        # nessuno risponde: deve scadere (settings.approval_timeout_s=2) e NEGARE
        result = await asyncio.wait_for(
            gate("Write", {"file_path": str(inside_root)}, None), timeout=5)
        assert type(result).__name__ == "PermissionResultDeny"
        row = db.query_one("SELECT status FROM approval WHERE run_id='run-timeout'")
        assert row["status"] == "timeout"

    asyncio.run(scenario())
