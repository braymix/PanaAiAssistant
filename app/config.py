"""Configurazione di Argo. Utente singolo, un processo, niente cloud oltre Claude."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


@dataclass
class Settings:
    # --- rete / bind (regola 4.2: SOLO loopback) --------------------------------
    host: str = "127.0.0.1"
    port: int = int(os.environ.get("ARGO_PORT", "8765"))

    # Header d'identita' iniettato da Tailscale Serve. E' l'auth (regola 4.2).
    identity_header: str = os.environ.get(
        "ARGO_IDENTITY_HEADER", "tailscale-user-login"
    )
    # In dev locale (senza Tailscale davanti) si puo' allentare SOLO se esplicito.
    # Non e' bypassPermissions: e' l'auth di trasporto, e resta chiusa di default.
    dev_allow_no_identity: bool = (
        os.environ.get("ARGO_DEV_ALLOW_NO_IDENTITY", "0") == "1"
    )

    # --- storage ----------------------------------------------------------------
    db_path: Path = Path(os.environ.get("ARGO_DB", "argo.db"))
    artifacts_dir: Path = Path(os.environ.get("ARGO_ARTIFACTS", "artifacts"))

    # --- allowlist delle root su cui si puo' operare (regola 4.3) ----------------
    # ARGO_ROOTS = path separati da os.pathsep. Ogni repo_path e ogni files_allowed
    # DEVE cadere sotto una di queste, risolto, senza symlink-escape/UNC/'..'.
    repo_roots: list[str] = field(default_factory=lambda: _env_list("ARGO_ROOTS"))

    # --- executor locale (§1.9, GATE 2) -----------------------------------------
    max_local_concurrency: int = int(os.environ.get("ARGO_MAX_CONCURRENCY", "3"))
    max_local_retries: int = int(os.environ.get("ARGO_MAX_RETRIES", "2"))
    local_task_timeout_s: int = int(os.environ.get("ARGO_TASK_TIMEOUT_S", "900"))

    # backend Ollama (§1.2/1.3)
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = os.environ.get("ARGO_OLLAMA_MODEL", "qwen3-coder")
    ollama_context_length: str = os.environ.get("OLLAMA_CONTEXT_LENGTH", "65536")
    # log del server Ollama (default: posizione tipica su Windows). Override con
    # ARGO_OLLAMA_LOG se il tuo e' altrove.
    ollama_log: str = os.environ.get(
        "ARGO_OLLAMA_LOG",
        os.path.expandvars(r"%LOCALAPPDATA%\Ollama\server.log"),
    )

    # backend abbonamento (planner + escalation)
    subscription_model: str = os.environ.get("ARGO_SUB_MODEL", "")  # "" = default SDK

    # --- approvazioni (regola 4.6: il timeout NEGA) -----------------------------
    approval_timeout_s: int = int(os.environ.get("ARGO_APPROVAL_TIMEOUT_S", "300"))

    # --- display costo: il costo dall'SDK e' in USD ed e' STIMATO (equivalente
    # API), non un addebito reale sull'abbonamento. Lo mostriamo in EUR. --------
    usd_to_eur: float = float(os.environ.get("ARGO_USD_EUR", "0.92"))

    # --- allowlist comandi Bash per il PolicyGate (§3.2) ------------------------
    # prefissi consentiti senza chiedere al telefono. Tutto il resto -> push.
    bash_allowlist: list[str] = field(
        default_factory=lambda: _env_list(
            "ARGO_BASH_ALLOWLIST",
            os.pathsep.join(
                ["pytest", "python -m pytest", "python", "go test", "npm test",
                 "cargo test", "ruff", "mypy", "ls", "cat", "grep", "git status",
                 "git diff"]
            ),
        )
    )

    # --- push VAPID -------------------------------------------------------------
    vapid_keys_path: Path = Path(os.environ.get("ARGO_VAPID_KEYS", "vapid_keys.json"))
    vapid_sub: str = os.environ.get("ARGO_VAPID_SUB", "mailto:argo@localhost")

    def resolved_roots(self) -> list[Path]:
        return [Path(r).resolve() for r in self.repo_roots]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(s: Settings) -> None:
    """Per i test: sostituisce le settings globali."""
    global _settings
    _settings = s
