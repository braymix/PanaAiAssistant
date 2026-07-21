"""Setup idempotente di OpenClaw (modulo "PC Agente").

OpenClaw e' installato NATIVAMENTE su Windows via `npm install -g openclaw` e gira
come processo separato. Questo modulo prepara la macchina:
  * verifica l'installazione (`openclaw --version`) — NON installa da solo;
  * crea la cartella workspace con la struttura attesa;
  * genera/aggiorna `config.yaml` nel workspace, popolando dinamicamente i modelli
    Ollama installati (interroga GET {ollama_url}/api/tags — niente nomi hardcoded).

⚠️ OpenClaw e' FUORI dal perimetro di sicurezza di Argo (§B.3): nessun PolicyGate,
accesso totale al PC. Decisione deliberata dell'utente ("libero totale").
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("argo.openclaw")

# default della finestra di contesto per ogni modello Ollama esposto a OpenClaw.
DEFAULT_CONTEXT_WINDOW = 65536


@dataclass
class OpenClawStatus:
    installed: bool                 # `openclaw --version` ha successo
    version: str | None
    config_exists: bool             # config.yaml presente nel workspace
    process_running: bool           # il gateway e' attivo (PID check)
    ollama_connected: bool          # Ollama raggiungibile dall'endpoint configurato
    workspace: Path

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "version": self.version,
            "config_exists": self.config_exists,
            "process_running": self.process_running,
            "ollama_connected": self.ollama_connected,
            "workspace": str(self.workspace),
        }


def _config_path(settings) -> Path:
    return Path(settings.openclaw_workspace) / "config.yaml"


def _pid_file(settings) -> Path:
    return Path(settings.openclaw_workspace) / "argo-managed.pid"


def _openclaw_bin() -> str | None:
    """Path dell'eseguibile openclaw se sul PATH (npm global bin), altrimenti None."""
    return shutil.which("openclaw")


async def _run_version() -> tuple[bool, str | None]:
    """`openclaw --version`: (successo, versione). Non solleva."""
    if _openclaw_bin() is None:
        return False, None
    try:
        proc = await asyncio.create_subprocess_exec(
            "openclaw", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return True, out.decode("utf-8", "replace").strip() or None
        return False, None
    except (OSError, asyncio.TimeoutError, ValueError) as e:  # noqa: BLE001
        log.debug("openclaw --version fallito: %s", e)
        return False, None


def _pid_running(settings) -> bool:
    """Il gateway gestito da Argo e' vivo? Legge il pid_file e verifica il PID."""
    pf = _pid_file(settings)
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


def pid_alive(pid: int) -> bool:
    """Check cross-platform, non bloccante, se un PID e' vivo (senza psutil)."""
    if pid <= 0:
        return False
    import os
    import sys
    if sys.platform.startswith("win"):
        # su Windows: OpenProcess via ctypes; se apre -> vivo.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            # verifica che non sia gia' terminato (exit code STILL_ACTIVE=259)
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and exit_code.value == 259
        return False
    # POSIX: kill(pid, 0) — nessun segnale, solo il check di esistenza/permessi.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # esiste, ma non e' nostro: comunque vivo
    return True


async def _ollama_tags(settings) -> tuple[bool, list[dict]]:
    """(raggiungibile, lista modelli) da GET {ollama_url}/api/tags. Non solleva."""
    url = settings.ollama_url.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        return True, list(data.get("models", []) or [])
    except Exception as e:  # noqa: BLE001 — Ollama potrebbe essere spento
        log.debug("Ollama /api/tags non raggiungibile (%s): %s", url, e)
        return False, []


async def check_status(settings) -> OpenClawStatus:
    """Stato completo di OpenClaw. Tollerante: nessun crash se non installato o se
    il workspace non esiste (es. path Windows su Linux/CI)."""
    installed, version = await _run_version()
    workspace = Path(settings.openclaw_workspace)
    try:
        config_exists = _config_path(settings).exists()
    except OSError:
        config_exists = False
    try:
        process_running = _pid_running(settings)
    except OSError:
        process_running = False
    ollama_connected, _models = await _ollama_tags(settings)
    return OpenClawStatus(
        installed=installed, version=version, config_exists=config_exists,
        process_running=process_running, ollama_connected=ollama_connected,
        workspace=workspace,
    )


async def _log_line(line: str, level: str = "info") -> None:
    """Pubblica una riga sul bus come `openclaw_log`, cosi' compare nel log live
    della dashboard esattamente come l'output del gateway."""
    from .events import get_bus
    await get_bus().emit(None, "openclaw_log",
                         {"line": line, "level": level, "timestamp": time.time()})


async def _npm_install(settings) -> tuple[bool, str | None]:
    """SCARICA e installa OpenClaw (`npm install -g openclaw` per default).

    Streamma l'output riga per riga come eventi `openclaw_log`. Non solleva:
    ritorna (successo, errore). Il comando e' configurabile
    (ARGO_OPENCLAW_INSTALL_CMD) e ha un timeout (ARGO_OPENCLAW_INSTALL_TIMEOUT_S).
    """
    cmd = (settings.openclaw_install_cmd or "").strip() or "npm install -g openclaw"
    timeout = max(30, int(settings.openclaw_install_timeout_s or 600))
    await _log_line(f"[argo] scarico e installo OpenClaw: {cmd}")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as e:
        return False, str(e)

    async def _pump() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line:
                await _log_line(line)

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
        rc = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False, f"timeout dopo {timeout}s"
    except OSError as e:  # noqa: BLE001
        return False, str(e)
    if rc == 0:
        return True, None
    return False, f"exit code {rc}"


async def ensure_installed(settings) -> bool:
    """Assicura che openclaw sia installato globalmente via npm.

    Se manca e l'auto-download e' attivo (default), lo SCARICA con
    `npm install -g openclaw` e ri-verifica. Con auto-download spento, si limita a
    loggare le istruzioni per l'installazione manuale. Idempotente."""
    installed, version = await _run_version()
    if installed:
        log.info("OpenClaw gia' installato (%s).", version or "versione ignota")
        return True

    if not getattr(settings, "openclaw_auto_install", True):
        log.warning(
            "OpenClaw NON installato (auto-download disattivato). Installalo con:\n"
            "    npm install -g openclaw\n"
            "poi ripremi 'Setup' dalla dashboard.")
        await _log_line(
            "[argo] OpenClaw non installato. Auto-download OFF: "
            "installa a mano con `npm install -g openclaw`.", level="error")
        return False

    log.info("OpenClaw non installato: avvio auto-download.")
    ok, err = await _npm_install(settings)
    if not ok:
        log.warning(
            "Auto-download OpenClaw fallito (%s). Installa a mano: "
            "npm install -g openclaw", err)
        await _log_line(
            f"[argo] auto-download fallito: {err}. "
            "Prova a mano: `npm install -g openclaw`.", level="error")
        return False

    installed, version = await _run_version()
    if installed:
        log.info("OpenClaw installato (%s).", version or "ok")
        await _log_line(f"[argo] OpenClaw installato ({version or 'ok'}).")
    else:
        await _log_line(
            "[argo] installazione terminata ma `openclaw --version` non risponde: "
            "controlla il PATH di npm (bin globale).", level="error")
    return installed


async def setup_workspace(settings) -> Path:
    """Crea la cartella workspace con la struttura attesa, se non esiste. On-demand
    (non all'avvio di Argo). Idempotente."""
    ws = Path(settings.openclaw_workspace)
    ws.mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "sessions"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def _model_entry(tag: str, context_window: int = DEFAULT_CONTEXT_WINDOW) -> dict:
    """Costruisce l'entry di un modello Ollama per config.yaml di OpenClaw."""
    return {
        "id": f"ollama/{tag}",
        "name": tag,
        "contextWindow": context_window,
        "reasoning": False,
        "cost": {"input": 0, "output": 0},
    }


def _default_config(settings, model_entries: list[dict], primary: str) -> dict:
    """Il documento config.yaml completo, dalle decisioni architetturali (§B.2)."""
    ws = str(Path(settings.openclaw_workspace))
    return {
        "models": {
            "providers": {
                "ollama": {
                    "baseUrl": settings.ollama_url.rstrip("/") + "/v1",
                    "apiKey": "ollama-local",
                    "api": "openai-completions",
                    "models": model_entries,
                },
            },
        },
        "agents": {
            "defaults": {
                "model": {"primary": primary},
                "workspace": ws,
                "maxConcurrent": settings.openclaw_max_concurrent,
                "subagents": {"maxConcurrent": 4},
            },
            "heartbeat": {"enabled": False},   # on-demand only — niente daemon
        },
        "gateway": {"port": settings.openclaw_gateway_port},
        "tools": {
            "shell": {"enabled": True, "allowedCommands": "all"},  # accesso totale
            "browser": {"enabled": True},
            "files": {"enabled": True, "allowedPaths": "all"},     # nessun perimetro
            "web": {
                "search": {"enabled": True},  # OpenClaw puo' cercare (Argo no)
                "fetch": {"enabled": True},
            },
        },
        # integrazioni messaging: PREDISPOSTE, non attive.
        "integrations": {
            "whatsapp": {"enabled": False},
            "telegram": {"enabled": False},
            "slack": {"enabled": False},
            "discord": {"enabled": False},
        },
    }


def _choose_primary(settings, model_entries: list[dict]) -> str:
    """Modello primario: ARGO_OPENCLAW_PRIMARY_MODEL se valido, altrimenti il primo
    disponibile; "" se nessun modello."""
    ids = [m["id"] for m in model_entries]
    pref = (settings.openclaw_primary_model or "").strip()
    if pref:
        cand = pref if pref.startswith("ollama/") else f"ollama/{pref}"
        if cand in ids:
            return cand
        # preferito indicato ma non installato: usalo comunque (l'utente sa cosa fa)
        return cand
    return ids[0] if ids else ""


async def generate_config(settings) -> Path:
    """Genera/aggiorna config.yaml nel workspace di OpenClaw.

    1. interroga GET {ollama_url}/api/tags per i modelli installati;
    2. costruisce un'entry per ciascuno (id, name, contextWindow, reasoning, costo 0);
    3. sceglie il primary (ARGO_OPENCLAW_PRIMARY_MODEL o il primo);
    4. scrive il YAML. Se il file esiste gia', AGGIORNA solo la sezione modelli e il
       primary, preservando il resto della config utente.
    """
    ws = await setup_workspace(settings)
    _connected, models = await _ollama_tags(settings)
    tags = [m.get("name") or m.get("model") for m in models]
    tags = [t for t in tags if t]
    model_entries = [_model_entry(t) for t in tags]
    primary = _choose_primary(settings, model_entries)

    cfg_path = _config_path(settings)
    if cfg_path.exists():
        # aggiorna SOLO la sezione modelli + primary, senza sovrascrivere il resto.
        try:
            existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
        existing.setdefault("models", {}).setdefault("providers", {})
        existing["models"]["providers"].setdefault("ollama", {})
        ollama_cfg = existing["models"]["providers"]["ollama"]
        ollama_cfg["baseUrl"] = settings.ollama_url.rstrip("/") + "/v1"
        ollama_cfg.setdefault("apiKey", "ollama-local")
        ollama_cfg.setdefault("api", "openai-completions")
        ollama_cfg["models"] = model_entries
        # aggiorna il primary solo se e' vuoto o non piu' tra i modelli installati
        defaults = existing.setdefault("agents", {}).setdefault("defaults", {})
        model_sec = defaults.setdefault("model", {})
        if primary and (not model_sec.get("primary")
                        or model_sec.get("primary") not in [m["id"] for m in model_entries]):
            model_sec["primary"] = primary
        doc = existing
    else:
        doc = _default_config(settings, model_entries, primary)

    cfg_path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("OpenClaw config.yaml scritto (%s) con %d modelli.",
             cfg_path, len(model_entries))
    return cfg_path
