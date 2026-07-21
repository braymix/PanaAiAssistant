"""Implementazione concreta di `PcAgent` per OpenClaw.

Wrappa `OpenClawProcess` (ciclo di vita) e `openclaw_setup` (installazione/config).
Legge le settings live via `get_settings()` a ogni chiamata, cosi' riflette sempre
la configurazione corrente (e i test possono sostituire le settings).

⚠️ OpenClaw e' FUORI dal perimetro di sicurezza di Argo (§B.3): questa classe NON
chiama mai il PolicyGate.
"""

from __future__ import annotations

from .. import openclaw_setup
from ..config import get_settings
from ..openclaw_process import OpenClawProcess
from .base import PcAgent


class OpenClawAgent(PcAgent):
    name = "openclaw"
    icon = "🦞"
    description = "PC Agente — accesso totale al PC (fuori dal perimetro di Argo)"

    def __init__(self) -> None:
        # il processo e' costruito lazy (§B.4: on-demand), cosi' non tocca il
        # workspace all'import ne' all'avvio di Argo.
        self._proc: OpenClawProcess | None = None

    def _process(self) -> OpenClawProcess:
        settings = get_settings()
        # (ri)costruisci se assente o se le settings sono cambiate (test).
        if self._proc is None or self._proc.settings is not settings:
            proc = OpenClawProcess(settings)
            # preserva lo stato del processo eventualmente gia' avviato
            if self._proc is not None:
                proc.proc = self._proc.proc
                proc._log_lines = self._proc._log_lines
                proc._pump_task = self._proc._pump_task
                proc._started_at = self._proc._started_at
                proc.command_override = self._proc.command_override
            self._proc = proc
        return self._proc

    # --- PcAgent -------------------------------------------------------------
    async def check_status(self) -> dict:
        settings = get_settings()
        status = await openclaw_setup.check_status(settings)
        d = status.to_dict()
        # il PID-check del setup guarda il pid_file; integra con lo stato live.
        d["process_running"] = self.is_running()
        d["name"] = self.name
        d["icon"] = self.icon
        d["description"] = self.description
        primary = settings.openclaw_primary_model or None
        d["primary_model"] = primary
        return d

    async def start(self) -> bool:
        # guardia: se OpenClaw non e' installato, `openclaw gateway` fallirebbe con
        # un criptico WinError 2 ("file non trovato"). Meglio un messaggio chiaro.
        settings = get_settings()
        if not await openclaw_setup.ensure_installed(settings):
            import time as _t
            from ..events import get_bus
            await get_bus().emit(None, "openclaw_log", {
                "line": "[argo] OpenClaw non installato. In un terminale lancia: "
                        "npm install -g openclaw  (serve Node.js). Poi premi "
                        "prima 'Setup', poi 'Avvia'.",
                "level": "error", "timestamp": _t.time()})
            await get_bus().emit(None, "openclaw_status_change",
                                 {"from": "stopped", "to": "not_installed"})
            return False
        return await self._process().start()

    async def stop(self) -> bool:
        return await self._process().stop()

    async def restart(self) -> bool:
        return await self._process().restart()

    async def send_task(self, prompt: str) -> str:
        return await self._process().send_task(prompt)

    def is_running(self) -> bool:
        return self._process().is_running()

    def recent_logs(self, n: int = 100) -> list[str]:
        return self._process().recent_logs(n)

    # --- setup / config (specifici di OpenClaw) ------------------------------
    async def setup(self) -> dict:
        """ensure_installed + setup_workspace + generate_config. Idempotente."""
        settings = get_settings()
        installed = await openclaw_setup.ensure_installed(settings)
        workspace = await openclaw_setup.setup_workspace(settings)
        config_path = await openclaw_setup.generate_config(settings)
        return {
            "installed": installed,
            "workspace": str(workspace),
            "config_path": str(config_path),
        }

    async def sync_models(self) -> dict:
        """Rigenera la sola sezione modelli del config.yaml da Ollama."""
        settings = get_settings()
        path = await openclaw_setup.generate_config(settings)
        _connected, models = await openclaw_setup._ollama_tags(settings)
        n = len([m for m in models if (m.get("name") or m.get("model"))])
        from ..events import get_bus
        await get_bus().emit(None, "openclaw_config_synced", {"n_models": n})
        return {"config_path": str(path), "n_models": n}

    async def current_config(self) -> str:
        """config.yaml corrente (read-only), stringa vuota se assente."""
        settings = get_settings()
        path = openclaw_setup._config_path(settings)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""
