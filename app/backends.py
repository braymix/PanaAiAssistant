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
                          max_turns: int, backend: str):
    """Executor. backend='ollama' -> env verso Ollama; 'subscription' -> escalation."""
    from claude_agent_sdk import ClaudeAgentOptions
    kwargs = dict(
        cwd=cwd,
        permission_mode="default",           # regola 4.5, esplicito
        allowed_tools=list(READONLY_TOOLS),  # §1.8: niente Write/Edit/Bash qui
        can_use_tool=can_use_tool,           # il PolicyGate
        max_turns=max_turns,
    )
    if backend == "ollama":
        kwargs["env"] = ollama_env(settings)  # §1.2
        kwargs["model"] = settings.ollama_model
    elif settings.subscription_model:
        kwargs["model"] = settings.subscription_model
    return ClaudeAgentOptions(**kwargs)
