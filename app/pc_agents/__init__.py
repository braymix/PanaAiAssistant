"""Pacchetto degli agenti PC gestiti da Argo.

Import di questo pacchetto registra gli agenti attivi nel registry. Aggiungere un
nuovo agente = implementare `PcAgent` + chiamarne il `register()` qui.

Invariante (§Test): questo pacchetto NON importa executor/planner/policy.
"""

from __future__ import annotations

from . import registry
from .base import PcAgent
from .openclaw_agent import OpenClawAgent

__all__ = ["PcAgent", "registry", "register_default_agents"]

_registered = False


def register_default_agents() -> None:
    """Registra gli agenti di default (idempotente). Chiamata all'avvio dell'app."""
    global _registered
    if _registered:
        return
    registry.register(OpenClawAgent())
    _registered = True


# registra subito all'import: la dashboard/route lo trovano senza setup extra.
register_default_agents()
