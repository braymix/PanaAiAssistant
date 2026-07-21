"""Fabbriche di ClaudeAgentOptions per i due backend.

Il perno del progetto (§1.2): `options.env` sceglie il backend per-run, nello
stesso processo Python. Ollama parla Anthropic nativamente (§1.3): niente proxy.

§1.8 (IL GOTCHA): Write/Edit/Bash NON stanno mai in allowed_tools, altrimenti
sono auto-approvati PRIMA di can_use_tool e il gate non gira. Solo tool di sola
lettura in allowed_tools.
"""

from __future__ import annotations

from typing import Callable

from .config import Settings

# tool di sola lettura: sicuri da auto-approvare. Il resto passa dal gate.
READONLY_TOOLS = ["Read", "Glob", "Grep"]

# Il planner non tocca file. In piu' disabilitiamo AskUserQuestion: nella chat non
# c'e' modo di rispondere a quel tool, quindi va in timeout; senza il tool, il
# planner fa le domande come TESTO e l'utente risponde scrivendo in chat.
PLANNER_DISALLOWED = ["Write", "Edit", "Bash", "NotebookEdit", "AskUserQuestion"]

# System prompt dell'EXECUTOR (sub-agente locale). Senza questa spinta esplicita i
# modelli locali "parlano" invece di agire: descrivono il file invece di scriverlo.
# Si APPENDE al preset claude_code, non lo sostituisce.
EXECUTOR_SYSTEM = (
    "Sei un agente di coding AUTONOMO, non un assistente conversazionale. Il tuo "
    "lavoro e' MODIFICARE I FILE con gli strumenti, non parlarne.\n"
    "- USA gli strumenti Write ed Edit per creare/modificare DAVVERO i file del "
    "task. Se ti limiti a stampare il contenuto in un messaggio, sul disco non "
    "cambia NULLA e il task FALLISCE: il risultato conta solo se scritto coi tool.\n"
    "- NON chiedere conferme e NON proporre piani: agisci subito. Tocca solo i "
    "file permessi dal task.\n"
    "- Quando i file sono scritti e il criterio di accettazione e' soddisfatto, "
    "fermati senza aggiungere spiegazioni."
)


def ollama_env(settings: Settings) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": settings.ollama_url,
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "OLLAMA_CONTEXT_LENGTH": settings.ollama_context_length,
    }


def make_planner_options(settings: Settings, cwd: str, can_use_tool: Callable,
                         max_turns: int = 40):
    """Planner su ABBONAMENTO, permission_mode='plan': non tocca niente (§3.1)."""
    from claude_agent_sdk import ClaudeAgentOptions
    kwargs = dict(
        cwd=cwd,
        permission_mode="plan",              # regola 4.5, esplicito
        disallowed_tools=PLANNER_DISALLOWED,  # non tocca file (§3.1)
        can_use_tool=can_use_tool,           # gira comunque (anti-pattern §8)
        max_turns=max_turns,
    )
    if settings.subscription_model:
        kwargs["model"] = settings.subscription_model
    return ClaudeAgentOptions(**kwargs)


def make_via_options(settings: Settings, cwd: str, can_use_tool: Callable,
                     max_turns: int = 40):
    """Generazione del PlanDocument (tasto VIA): NON plan mode (in plan mode la CLI
    propone un piano, non produce JSON). Read-only: niente Write/Edit/Bash, cosi'
    non tocca nulla ma puo' leggere il repo e rispondere in JSON."""
    from claude_agent_sdk import ClaudeAgentOptions
    kwargs = dict(
        cwd=cwd,
        permission_mode="default",
        disallowed_tools=PLANNER_DISALLOWED,   # non tocca file
        allowed_tools=list(READONLY_TOOLS),    # puo' leggere
        can_use_tool=can_use_tool,
        max_turns=max_turns,
    )
    if settings.subscription_model:
        kwargs["model"] = settings.subscription_model
    return ClaudeAgentOptions(**kwargs)


def make_executor_options(settings: Settings, cwd: str, can_use_tool: Callable,
                          max_turns: int, backend: str, model: str | None = None):
    """Executor. backend='ollama' -> env verso Ollama; 'subscription' -> escalation.

    `model` (opzionale) sovrascrive il modello del backend: l'autofix lo usa per i
    tier locali piu' forti (ARGO_AUTOFIX_LOCAL_TIERS) senza cambiare backend.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    kwargs = dict(
        cwd=cwd,
        permission_mode="default",           # regola 4.5, esplicito
        allowed_tools=list(READONLY_TOOLS),  # §1.8: niente Write/Edit/Bash qui
        can_use_tool=can_use_tool,           # il PolicyGate
        max_turns=max_turns,
        # spinge il sub-agente ad AGIRE coi tool invece di "parlare" (soprattutto
        # i modelli locali). APPEND al preset: non sostituisce Claude Code.
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": EXECUTOR_SYSTEM},
    )
    if backend == "ollama":
        kwargs["env"] = ollama_env(settings)  # §1.2
        kwargs["model"] = model or settings.ollama_model
    elif model:
        kwargs["model"] = model
    elif settings.subscription_model:
        kwargs["model"] = settings.subscription_model
    return ClaudeAgentOptions(**kwargs)
