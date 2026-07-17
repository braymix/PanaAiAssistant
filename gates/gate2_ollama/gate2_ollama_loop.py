"""
GATE 2 — Ollama regge il loop agentico.

Esegui sul PC con la GPU. Prima:

  1. Controlla la VRAM:            nvidia-smi
  2. Scegli il modello con tool-use forte in base alla VRAM (default qwen3-coder).
  3. Avvia Ollama con context ALTO (§1.9, la manopola che fa o rompe tutto):
         setx OLLAMA_CONTEXT_LENGTH 65536   (Windows, poi riapri il terminale)
         ollama serve
     e in un altro terminale:      ollama pull qwen3-coder
  4. pip install "claude-agent-sdk==0.2.120" anyio
  5. python gate2_ollama_loop.py

Cosa fa: crea un repo-giocattolo con un README, chiede a un ClaudeSDKClient
puntato su Ollama (via env, §1.2/1.3) un task banale — "leggi README.md e crea
SUMMARY.md" — con max_turns=15 e timeout wall-clock 5 min (§1.9: puo' loopare).

Misura e RIPORTA i numeri veri (non nasconderli dietro un retry):
  * ha completato? (SUMMARY.md esiste e result subtype == success)
  * quanti turni?
  * ha loopato? (turni vicini a max_turns senza aver scritto il file = sintomo)
  * durata.

Se loopa su un task banale non reggera' 40 task reali: si rivede il modello o
il dettaglio del TaskBrief. Questo e' il senso del gate.
"""

import anyio
import os
import time
import tempfile
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

# --- configurazione -------------------------------------------------------------
MODEL = os.environ.get("GATE2_MODEL", "qwen3-coder")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MAX_TURNS = 15
TIMEOUT_S = 300  # 5 minuti wall-clock

# §1.3: Ollama parla Anthropic nativamente da v0.14.0. Niente LiteLLM/proxy.
# §1.2: options.env finisce nell'environment del processo `claude` spawnato.
OLLAMA_ENV = {
    "ANTHROPIC_BASE_URL": OLLAMA_URL,
    "ANTHROPIC_AUTH_TOKEN": "ollama",
    "OLLAMA_CONTEXT_LENGTH": "65536",  # §1.9
}

TASK = (
    "Leggi il file README.md nella working directory. Poi crea un file "
    "SUMMARY.md che contenga un riassunto di 2-3 righe del README. "
    "Quando SUMMARY.md e' scritto, hai finito: fermati."
)


async def main():
    workdir = Path(tempfile.mkdtemp(prefix="gate2_"))
    (workdir / "README.md").write_text(
        "# Toy Project\n\n"
        "Questo e' un progetto giocattolo per il GATE 2.\n"
        "Serve solo a verificare che l'executor locale sappia leggere un file "
        "e scriverne un altro senza incastrarsi in un loop.\n",
        encoding="utf-8",
    )
    summary = workdir / "SUMMARY.md"
    print(f"[workdir] {workdir}")
    print(f"[model]   {MODEL} via {OLLAMA_URL}")

    options = ClaudeAgentOptions(
        cwd=str(workdir),
        model=MODEL,
        env=OLLAMA_ENV,                       # §1.2 -> backend per-run
        permission_mode="acceptEdits",        # esplicito (regola 4.5)
        allowed_tools=["Read", "Write"],      # gate di CAPACITA', non di sicurezza:
                                              # qui vogliamo solo misurare il loop.
        max_turns=MAX_TURNS,                  # esplicito (regola 4.5)
    )

    turns = 0
    result_subtype = None
    completed = False
    looped = False

    start = time.monotonic()
    with anyio.move_on_after(TIMEOUT_S) as scope:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(TASK)
            async for msg in client.receive_response():
                name = type(msg).__name__
                if name == "AssistantMessage":
                    for block in getattr(msg, "content", []) or []:
                        if type(block).__name__ == "ToolUseBlock":
                            print(f"  turn -> tool {getattr(block, 'name', '?')} "
                                  f"input={getattr(block, 'input', {})!r}")
                if name == "ResultMessage":
                    turns = getattr(msg, "num_turns", 0) or 0
                    result_subtype = getattr(msg, "subtype", None)

    duration = time.monotonic() - start
    if scope.cancelled_caught:
        looped = True
        print(f"\n[!] TIMEOUT wall-clock a {TIMEOUT_S}s — sintomo di loop (§1.9).")

    completed = summary.exists() and result_subtype == "success"
    if turns >= MAX_TURNS and not completed:
        looped = True

    print("\n" + "#" * 60)
    print("VERDETTO GATE 2 (numeri veri)")
    print(f"  completato:      {completed}  (SUMMARY.md esiste={summary.exists()}, "
          f"result={result_subtype})")
    print(f"  turni:           {turns} / {MAX_TURNS}")
    print(f"  ha loopato:      {looped}")
    print(f"  durata:          {duration:.1f}s")
    if summary.exists():
        print(f"\n--- SUMMARY.md ---\n{summary.read_text(encoding='utf-8')}")
    print("#" * 60)
    print("Riporta questi numeri cosi' come sono.")


if __name__ == "__main__":
    anyio.run(main)
