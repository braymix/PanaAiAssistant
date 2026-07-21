"""Gestione del ciclo di vita del processo OpenClaw gateway.

OpenClaw gira come processo SEPARATO (subprocess), NON dentro il loop asyncio di
Argo (invariante §2: il processo asyncio resta uno solo). Lo stato e i log sono
visibili in dashboard via eventi sul bus (SSE, §4.8).

Lifecycle: on-demand. Il processo NON parte all'avvio di Argo; si avvia al primo
`start()` esplicito e si ferma con `stop()` (o al cleanup del lifespan).

Cross-platform (invariante §Test):
  * avvio: `start_new_session=True` (POSIX) / `CREATE_NEW_PROCESS_GROUP` (Windows),
    cosi' il gruppo di processi e' terminabile in blocco;
  * stop: `os.killpg` (POSIX) / `taskkill /F /T` (Windows).

⚠️ OpenClaw NON passa dal PolicyGate (§B.3): nessun `can_use_tool` in questo
percorso. Accesso totale al PC, per scelta esplicita dell'utente.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from .events import get_bus
from .ids import new_id
from .openclaw_setup import _config_path, _pid_file, exec_argv, pid_alive

log = logging.getLogger("argo.openclaw")

_IS_WIN = sys.platform.startswith("win")


class OpenClawProcess:
    """Wrappa il processo `openclaw gateway` gestito da Argo."""

    def __init__(self, settings):
        self.settings = settings
        self.workspace = Path(settings.openclaw_workspace)
        self.gateway_port = settings.openclaw_gateway_port
        self.proc: subprocess.Popen | None = None
        self.pid_file = _pid_file(settings)
        self._log_lines: deque[str] = deque(maxlen=500)
        self._pump_task: asyncio.Task | None = None
        self._started_at: float | None = None
        # override per i test: comando alternativo (un processo finto ma reale).
        self.command_override: list[str] | None = None

    # --- comando -------------------------------------------------------------
    def _build_command(self) -> list[str]:
        if self.command_override is not None:
            return self.command_override
        args = ["gateway", "--port", str(self.gateway_port)]
        cfg = _config_path(self.settings)
        if cfg.exists():
            args += ["--config", str(cfg)]
        # risolve lo shim npm (openclaw.cmd su Windows) -> evita WinError 2.
        argv = exec_argv("openclaw", args)
        return argv if argv is not None else ["openclaw", *args]

    def _popen_kwargs(self) -> dict:
        kwargs: dict = dict(
            cwd=str(self.workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if _IS_WIN:
            # nuovo gruppo di processi -> terminabile in blocco con taskkill /T.
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            # sessione/gruppo nuovo -> os.killpg colpisce anche i figli.
            kwargs["start_new_session"] = True
        return kwargs

    # --- start ---------------------------------------------------------------
    async def start(self) -> bool:
        """Avvia il gateway. Se gia' in esecuzione, ritorna True senza fare nulla."""
        if self.is_running():
            return True
        self.workspace.mkdir(parents=True, exist_ok=True)
        cmd = self._build_command()
        try:
            self.proc = subprocess.Popen(cmd, **self._popen_kwargs())
        except (OSError, ValueError) as e:
            log.error("Avvio OpenClaw fallito (%s): %s", cmd, e)
            await get_bus().emit(None, "openclaw_log", {
                "line": f"[argo] avvio fallito: {e}", "level": "error",
                "timestamp": time.time()})
            return False
        self._started_at = time.monotonic()
        try:
            self.pid_file.write_text(str(self.proc.pid), encoding="utf-8")
        except OSError as e:
            log.warning("Impossibile scrivere pid_file (%s): %s", self.pid_file, e)
        # avvia la cattura dei log su un task di background
        self._pump_task = asyncio.ensure_future(self._pump_logs())
        await get_bus().emit(None, "openclaw_started", {
            "pid": self.proc.pid,
            "model": self.settings.openclaw_primary_model or None,
            "workspace": str(self.workspace),
        })
        await get_bus().emit(None, "openclaw_status_change",
                             {"from": "stopped", "to": "running"})
        return True

    # --- cattura log ---------------------------------------------------------
    async def _pump_logs(self) -> None:
        """Legge stdout riga per riga (in un thread) e le pubblica come eventi."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        bus = get_bus()
        try:
            while True:
                line = await asyncio.to_thread(proc.stdout.readline)
                if line == "":
                    break   # EOF: processo terminato
                line = line.rstrip("\n")
                if not line:
                    continue
                self._log_lines.append(line)
                await bus.emit(None, "openclaw_log", {
                    "line": line, "level": "info", "timestamp": time.time()})
        except Exception as e:  # noqa: BLE001 — il pump non deve mai crashare Argo
            log.debug("pump log OpenClaw terminato: %s", e)

    # --- stop ----------------------------------------------------------------
    async def stop(self, reason: str = "manual") -> bool:
        """Ferma il gateway (SIGTERM/taskkill), timeout 10s poi force. Idempotente."""
        if not self.is_running():
            self._cleanup_pid()
            return True
        pid = self.proc.pid if self.proc else self._pid_from_file()
        uptime = (time.monotonic() - self._started_at) if self._started_at else 0.0
        await asyncio.to_thread(self._terminate, pid)
        if self._pump_task:
            self._pump_task.cancel()
            self._pump_task = None
        self._cleanup_pid()
        self.proc = None
        self._started_at = None
        await get_bus().emit(None, "openclaw_stopped", {
            "pid": pid, "uptime_s": round(uptime, 1), "reason": reason})
        await get_bus().emit(None, "openclaw_status_change",
                             {"from": "running", "to": "stopped"})
        return True

    def _terminate(self, pid: int | None) -> None:
        """Blocca finche' il processo non e' morto (chiamare in un thread)."""
        if pid is None:
            return
        if _IS_WIN:
            # termina l'intero albero del gruppo di processi.
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("taskkill OpenClaw fallito: %s", e)
            return
        # POSIX: SIGTERM al gruppo, attesa 10s, poi SIGKILL.
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.2)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _cleanup_pid(self) -> None:
        try:
            self.pid_file.unlink()
        except OSError:
            pass

    # --- restart -------------------------------------------------------------
    async def restart(self) -> bool:
        await self.stop(reason="restart")
        ok = await self.start()
        if ok:
            await get_bus().emit(None, "openclaw_restarted",
                                 {"pid": self.proc.pid if self.proc else None})
        return ok

    # --- introspezione -------------------------------------------------------
    def _pid_from_file(self) -> int | None:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def is_running(self) -> bool:
        """Check non bloccante: PID valido + processo vivo."""
        if self.proc is not None:
            return self.proc.poll() is None
        pid = self._pid_from_file()
        return pid is not None and pid_alive(pid)

    def recent_logs(self, n: int = 100) -> list[str]:
        """Ultime n righe dal ring buffer."""
        if n <= 0:
            return []
        return list(self._log_lines)[-n:]

    # --- ponte Argo -> OpenClaw ---------------------------------------------
    async def send_task(self, prompt: str) -> str:
        """Invia un task a OpenClaw via la sua API locale. Ritorna il task_id.

        Emette `openclaw_task_sent`. Se l'API non risponde con un id, ne genera uno
        locale, cosi' il ponte Argo->OpenClaw resta osservabile in dashboard."""
        result = await self._post_task(prompt)
        task_id = (result.get("task_id") or result.get("id")
                   or new_id("octask"))
        await get_bus().emit(None, "openclaw_task_sent", {
            "prompt": prompt, "openclaw_task_id": task_id})
        return task_id

    async def _post_task(self, prompt: str) -> dict:
        """POST del task al gateway locale. Isolato per essere mockabile nei test."""
        import httpx
        url = f"http://127.0.0.1:{self.gateway_port}/api/tasks"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json={"prompt": prompt})
                r.raise_for_status()
                data = r.json()
                return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001 — il gateway potrebbe non esporre l'API
            log.debug("send_task al gateway fallito (%s): %s", url, e)
            return {}
