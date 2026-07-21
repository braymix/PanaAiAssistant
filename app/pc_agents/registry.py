"""Registro in-memory degli agenti PC.

La dashboard e le route `/agents/*` iterano `all_agents()`: aggiungere un nuovo
agente = implementare `PcAgent` + `register()`, e compare automaticamente.
"""

from __future__ import annotations

from .base import PcAgent

_agents: dict[str, PcAgent] = {}


def register(agent: PcAgent) -> None:
    """Registra (o sostituisce) un agente per nome. Idempotente sul nome."""
    _agents[agent.name] = agent


def get(name: str) -> PcAgent | None:
    return _agents.get(name)


def all_agents() -> list[PcAgent]:
    return list(_agents.values())


def clear() -> None:
    """Per i test: svuota il registro."""
    _agents.clear()
