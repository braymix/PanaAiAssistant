"""
GATE 1 — L'approvazione blocca davvero.

Esegui QUESTO sul PC dove gira Claude Code con abbonamento (NON in un sandbox).

    pip install "claude-agent-sdk==0.2.120" anyio
    python spike_approval.py

Deve, tutti e quattro:
  1. bloccarsi finche' NON rispondi (digiti a/d + invio nel terminale);
  2. mostrare i secondi di blocco mentre aspetta;
  3. NEGARE automaticamente al timeout (default 30s), non permettere (regola 4.6);
  4. catturare e stampare il session_id dal SystemMessage(init).

Verdetto:
  - Se alla fine  state["approvals"] == 0  => QUALCOSA PRE-APPROVA "Write"
    (vedi §1.8: un tool in allowed_tools e' auto-approvato PRIMA di can_use_tool).
    In quel caso il gate FALLISCE: fermati e riporta, non aggirarlo.
  - Se  state["approvals"] >= 1  e il blocco/timeout si comportano come sopra,
    il gate PASSA.

Punti chiave verificati contro l'SDK 0.2.120:
  * §1.1  can_use_tool richiede STREAMING mode -> usiamo ClaudeSDKClient come
          context manager (NON query() con prompt-stringa: solleverebbe ValueError).
  * §1.8  "Write" NON e' in allowed_tools: se ci fosse, il callback non verrebbe
          mai chiamato e questo spike "sembrerebbe" funzionare pur essendo rotto.
  * §4.6  il timeout NEGA.
"""

import anyio
import sys
import time

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
)

# --- configurazione dello spike -------------------------------------------------
APPROVAL_TIMEOUT_S = 30
PROMPT = (
    "Crea un file chiamato hello_gate1.txt nella working directory corrente "
    "con dentro la riga 'gate 1 ok'. Usa lo strumento Write."
)

# Stato osservabile del gate. Se 'approvals' resta 0 => il gate e' rotto (§1.8).
state = {
    "approvals": 0,       # quante volte can_use_tool e' stato REALMENTE invocato
    "allowed": 0,
    "denied": 0,
    "timed_out": 0,
    "session_id": None,
}


async def prompt_human_decision(tool_name: str, tool_input: dict) -> str:
    """Chiede a te, nel terminale, allow/deny — con countdown e timeout->deny."""
    print("\n" + "=" * 60)
    print(f"[APPROVAZIONE RICHIESTA]  tool = {tool_name}")
    # §4.7: tool_input si mostra GREZZO, mai riassunto.
    print(f"tool_input (grezzo): {tool_input!r}")
    print("Rispondi:  a = allow   d = deny   (invio)")
    print(f"Timeout tra {APPROVAL_TIMEOUT_S}s -> DENY automatico.")
    print("=" * 60)

    start = time.monotonic()
    answer = {"value": None}

    async def read_stdin():
        # anyio.to_thread cosi' l'input bloccante non ferma il countdown.
        line = await anyio.to_thread.run_sync(sys.stdin.readline)
        answer["value"] = (line or "").strip().lower()

    async def tick():
        while answer["value"] is None:
            elapsed = time.monotonic() - start
            print(f"  ...bloccato da {elapsed:5.1f}s", flush=True)
            await anyio.sleep(1.0)

    with anyio.move_on_after(APPROVAL_TIMEOUT_S) as scope:
        async with anyio.create_task_group() as tg:
            tg.start_soon(tick)
            await read_stdin()
            tg.cancel_scope.cancel()

    if scope.cancelled_caught or answer["value"] is None:
        return "timeout"
    return "allow" if answer["value"].startswith("a") else "deny"


async def can_use_tool(tool_name, input_data, context):
    """Il cuore del gate. Se non viene mai chiamato per Write => §1.8 violato."""
    state["approvals"] += 1
    decision = await prompt_human_decision(tool_name, input_data)

    if decision == "allow":
        state["allowed"] += 1
        print(">>> ALLOW")
        return PermissionResultAllow()

    if decision == "timeout":
        state["timed_out"] += 1
        print(">>> TIMEOUT -> DENY (regola 4.6)")
        return PermissionResultDeny(message="Approval timed out; denied by policy.")

    state["denied"] += 1
    print(">>> DENY")
    return PermissionResultDeny(message="Denied by human.")


async def main():
    options = ClaudeAgentOptions(
        permission_mode="default",     # esplicito (regola 4.5)
        can_use_tool=can_use_tool,
        # §1.8: "Write" DELIBERATAMENTE assente da allowed_tools.
        allowed_tools=["Read"],
        disallowed_tools=[],
        max_turns=6,                   # esplicito (regola 4.5)
    )

    print(f"Avvio spike. Timeout approvazione = {APPROVAL_TIMEOUT_S}s.\n")

    # §1.1: streaming mode via context manager, poi client.query(prompt).
    async with ClaudeSDKClient(options=options) as client:
        await client.query(PROMPT)
        async for msg in client.receive_response():
            data = getattr(msg, "data", None)
            # §1.7: SystemMessage(init) -> session_id, da persistere SUBITO.
            if getattr(msg, "subtype", None) == "init" and isinstance(data, dict):
                sid = data.get("session_id")
                if sid:
                    state["session_id"] = sid
                    print(f"[session_id] {sid}")
            # ResultMessage finale
            if type(msg).__name__ == "ResultMessage":
                print(f"\n[result] subtype={getattr(msg, 'subtype', None)} "
                      f"is_error={getattr(msg, 'is_error', None)} "
                      f"turns={getattr(msg, 'num_turns', None)} "
                      f"cost=${getattr(msg, 'total_cost_usd', 0) or 0:.4f}")

    print("\n" + "#" * 60)
    print("VERDETTO GATE 1")
    print(f"  approvals (can_use_tool invocato): {state['approvals']}")
    print(f"  allowed={state['allowed']} denied={state['denied']} "
          f"timed_out={state['timed_out']}")
    print(f"  session_id catturato: {state['session_id']}")
    if state["approvals"] == 0:
        print("  ESITO: ❌ FALLITO — qualcosa pre-approva Write (§1.8). "
              "Fermati e riporta.")
        sys.exit(1)
    print("  ESITO: ✅ can_use_tool ha bloccato almeno una volta.")
    print("#" * 60)


if __name__ == "__main__":
    anyio.run(main)
