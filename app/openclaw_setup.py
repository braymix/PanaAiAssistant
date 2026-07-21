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
import os
import shutil
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


def exec_argv(name: str, args: list[str],
              is_windows: bool | None = None) -> list[str] | None:
    """Argv per lanciare un eseguibile risolvendone lo shim.

    Su Windows npm installa i comandi come shim `.cmd`/`.bat`: `subprocess` NON
    li risolve da solo (fallisce con WinError 2), anche se il comando funziona nel
    terminale. Qui risolviamo il percorso REALE con `shutil.which` (che applica il
    PATHEXT) e, se e' uno shim `.cmd`/`.bat`, lo lanciamo via `cmd /c` (CreateProcess
    non esegue i .cmd direttamente). None se il comando non e' sul PATH.

    `is_windows` e' iniettabile per i test; di default deriva da os.name."""
    if is_windows is None:
        is_windows = os.name == "nt"
    exe = shutil.which(name)
    if exe is None and is_windows:
        # PATH del processo Argo "stale" (terminale aperto prima di installare
        # Node): cerca lo shim nella cartella globale npm standard.
        exe = _find_npm_shim(name)
    if exe is None:
        return None
    if is_windows and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


def _find_npm_shim(name: str) -> str | None:
    """Percorso dello shim `<name>.cmd` nella dir globale npm tipica di Windows."""
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm")           # default installer ufficiale
    prefix = os.environ.get("npm_config_prefix")
    if prefix:
        roots.append(Path(prefix))
    for root in roots:
        for ext in (".cmd", ".bat", ".exe", ""):
            cand = root / f"{name}{ext}"
            try:
                if cand.exists():
                    return str(cand)
            except OSError:
                continue
    return None


async def _run_version() -> tuple[bool, str | None]:
    """`openclaw --version`: (successo, versione). Non solleva."""
    argv = exec_argv("openclaw", ["--version"])
    if argv is None:
        return False, None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
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


async def ensure_installed(settings) -> bool:
    """Verifica che openclaw sia installato globalmente via npm.

    Se NON lo e', non lo installa: ritorna False e logga le istruzioni (l'utente
    fa `npm install -g openclaw` una volta sola). Idempotente."""
    installed, version = await _run_version()
    if installed:
        log.info("OpenClaw gia' installato (%s).", version or "versione ignota")
        return True
    log.warning(
        "OpenClaw NON installato. Installalo una volta sola con:\n"
        "    npm install -g openclaw\n"
        "poi ripremi 'Setup' dalla dashboard.")
    return False


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
