"""Interfaccia astratta per gli agenti PC gestiti da Argo.

Un "agente PC" e' un processo esterno che agisce sulla macchina dell'utente
(OpenClaw e' il primo). Argo lo avvia/ferma/configura e ne mostra lo stato in
dashboard, ma NON gli impone il PolicyGate: la sicurezza e' delegata all'agente
stesso (per OpenClaw: "libero totale", accesso pieno al PC).

Invarianti (§Test):
  * questo modulo NON importa executor/planner/policy: nessun ciclo.
  * ogni implementazione e' un processo SEPARATO; il processo asyncio di Argo
    resta uno solo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # solo per il type hint, evita l'import ciclico a runtime
    from . import registry as _registry


class PcAgent(ABC):
    """Contratto per qualsiasi agente PC gestito da Argo.

    OpenClaw e' il primo; altri (autohotkey, home-assistant, ...) seguono con lo
    stesso contratto: implementare la classe + `registry.register()`, e la UI e
    le route `/agents/*` lo mostrano automaticamente.
    """

    name: str = ""            # "openclaw", "autohotkey", ...  (slug, univoco)
    icon: str = "🤖"          # emoji o path icona
    description: str = ""     # descrizione breve per la dashboard

    # --- ciclo di vita -------------------------------------------------------
    @abstractmethod
    async def check_status(self) -> dict:
        """Stato completo dell'agente (installato, running, modelli, workspace...)."""

    @abstractmethod
    async def start(self) -> bool:
        """Avvia l'agente. Idempotente: se gia' attivo, ritorna True."""

    @abstractmethod
    async def stop(self) -> bool:
        """Ferma l'agente. Idempotente: se gia' fermo, ritorna True."""

    @abstractmethod
    async def restart(self) -> bool:
        """stop() + start()."""

    @abstractmethod
    async def send_task(self, prompt: str) -> str:
        """Invia un task all'agente. Ritorna un task_id (dell'agente)."""

    @abstractmethod
    def is_running(self) -> bool:
        """Check non bloccante: l'agente e' vivo?"""

    @abstractmethod
    def recent_logs(self, n: int = 100) -> list[str]:
        """Ultime n righe di log dal ring buffer dell'agente."""

    # --- ponte inter-agente (predisposto, §C.6) ------------------------------
    async def receive_from(self, sender: str, payload: dict) -> dict:
        """Ricevi un messaggio da un altro agente. Override opzionale.

        Di default non implementato: un agente accetta messaggi inter-agente solo
        se sceglie esplicitamente di farlo."""
        raise NotImplementedError(
            f"L'agente {self.name!r} non riceve messaggi inter-agente.")

    async def send_to(self, target_name: str, payload: dict) -> dict:
        """Invia un payload a un altro agente registrato.

        Import locale del registry per non creare un ciclo a livello di modulo."""
        from . import registry
        target = registry.get(target_name)
        if not target:
            raise ValueError(f"Agente {target_name} non trovato")
        return await target.receive_from(self.name, payload)
