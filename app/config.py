"""Configurazione di Argo. Utente singolo, un processo, niente cloud oltre Claude."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


# --- settorializzazione LLM: registro dei tier di modello (§A.2) ----------------
@dataclass
class ModelTier:
    """Un livello del routing per peso. `backend='ollama'` -> modello locale;
    `backend='subscription'` -> Claude (est_vram_mb=0, non consuma GPU)."""
    name: str                # "light" | "mid" | "heavy" | "frontier"
    backend: str             # "ollama" | "subscription"
    model: str               # es. "qwen2.5-coder:7b" | "" (default SDK)
    est_vram_mb: int         # 0 per subscription
    rel_speed: int           # 1 (lento) .. 5 (veloce)
    quality: int             # 1 .. 5

    def to_dict(self) -> dict:
        return {
            "name": self.name, "backend": self.backend, "model": self.model,
            "est_vram_mb": self.est_vram_mb, "rel_speed": self.rel_speed,
            "quality": self.quality,
        }

    @staticmethod
    def from_dict(d: dict) -> "ModelTier":
        return ModelTier(
            name=d["name"], backend=d.get("backend", "ollama"),
            model=d.get("model", ""),
            est_vram_mb=int(d.get("est_vram_mb", 0) or 0),
            rel_speed=int(d.get("rel_speed", 3) or 3),
            quality=int(d.get("quality", 3) or 3),
        )


# Ordine canonico dei tier (dal piu' leggero al piu' pesante). Il router lo usa
# per l'escalation (`next_tier`) e per il declassamento in VRAM.
TIER_ORDER = ["light", "mid", "heavy", "frontier"]


def default_model_tiers() -> list[ModelTier]:
    """Template di default, tarato sul PROFILO HARDWARE (RTX 3080 Ti 12GB, il PC
    dell'utente — cfr. storia del repo). SOSTITUIBILE via ARGO_MODEL_TIERS (JSON).

    Le stime VRAM sono conservative (peso a runtime del modello quantizzato + KV
    cache); il router lascia sempre un headroom (ARGO_VRAM_HEADROOM_MB)."""
    return [
        ModelTier("light", "ollama", "qwen2.5-coder:7b",
                  est_vram_mb=6000, rel_speed=5, quality=2),
        ModelTier("mid", "ollama", os.environ.get("ARGO_OLLAMA_MODEL", "qwen3-coder"),
                  est_vram_mb=9000, rel_speed=3, quality=3),
        ModelTier("heavy", "ollama", "qwen2.5-coder:14b",
                  est_vram_mb=11000, rel_speed=2, quality=4),
        ModelTier("frontier", "subscription", "",
                  est_vram_mb=0, rel_speed=4, quality=5),
    ]


def _load_model_tiers() -> list[ModelTier]:
    raw = os.environ.get("ARGO_MODEL_TIERS", "").strip()
    if not raw:
        return default_model_tiers()
    try:
        data = json.loads(raw)
        tiers = [ModelTier.from_dict(t) for t in data]
        return tiers or default_model_tiers()
    except (ValueError, TypeError, KeyError):
        return default_model_tiers()


# --- politica di pazienza (§A.4) ------------------------------------------------
@dataclass
class PatiencePolicy:
    # per classe di complessita': quanto aspetto in locale prima di salire di tier
    latency_budget_s: dict[str, int]     # {"light":120,"mid":600,"heavy":1800}
    cost_preference: str                 # "prefer_local" | "balanced" | "prefer_speed"
    local_rounds: dict[str, int]         # round di autofix locali prima di escalation
    preset: str = "balanced"             # "patient" | "balanced" | "fast"


# Preset globale via ARGO_PATIENCE, espanso in una tabella (§A.4).
_PATIENCE_PRESETS: dict[str, PatiencePolicy] = {
    "patient": PatiencePolicy(
        latency_budget_s={"light": 300, "mid": 1200, "heavy": 3600},
        cost_preference="prefer_local",
        local_rounds={"light": 3, "mid": 4, "heavy": 5},
        preset="patient",
    ),
    "balanced": PatiencePolicy(
        latency_budget_s={"light": 120, "mid": 600, "heavy": 1800},
        cost_preference="balanced",
        local_rounds={"light": 2, "mid": 3, "heavy": 3},
        preset="balanced",
    ),
    "fast": PatiencePolicy(
        latency_budget_s={"light": 60, "mid": 240, "heavy": 600},
        cost_preference="prefer_speed",
        local_rounds={"light": 1, "mid": 1, "heavy": 2},
        preset="fast",
    ),
}


def patience_policy(preset: str | None) -> PatiencePolicy:
    """Espande un preset (patient/balanced/fast) nella sua PatiencePolicy.
    Preset sconosciuto/None -> 'balanced' (default sicuro)."""
    key = (preset or "balanced").strip().lower()
    return _PATIENCE_PRESETS.get(key, _PATIENCE_PRESETS["balanced"])


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

    # --- root dei documenti: casa di default di TUTTI i progetti ----------------
    # E' SEMPRE una root valida (cfr. resolved_roots): cosi' anche con ARGO_ROOTS
    # vuoto non e' "tutto negato". L'utente non digita mai questo path.
    document_root: Path = Path(os.environ.get(
        "ARGO_DOCUMENT_ROOT",
        r"C:\Users\miche\Desktop\assistant\document",
    ))

    # --- root del sorgente di Argo (progetto "se stesso"). Default: auto-rilevata
    # dalla posizione del codice, cosi' segue l'app se la sposti. Override con
    # ARGO_SELF_ROOT. Sulla macchina dell'utente risolve a:
    # C:\Users\miche\Desktop\assistant\PanaAiAssistant
    self_root: Path = Path(os.environ.get(
        "ARGO_SELF_ROOT",
        str(Path(__file__).resolve().parents[1]),   # <repo>/app/config.py -> <repo>
    ))

    # --- servizio Documenti: config del browser file (missione Documenti) -------
    # cap sull'anteprima inline dei .md; oltre -> solo download.
    docs_max_preview_bytes: int = int(
        os.environ.get("ARGO_DOCS_MAX_PREVIEW_BYTES", str(1_048_576)))
    # deep link Obsidian: nome del vault come configurato sul telefono, e la
    # sottocartella di document_root che e' la radice del vault ("" = document_root).
    obsidian_vault: str = os.environ.get("ARGO_OBSIDIAN_VAULT", "")
    obsidian_vault_subpath: str = os.environ.get("ARGO_OBSIDIAN_VAULT_SUBPATH", "")

    # --- guard sul progetto "se stesso": i file di sicurezza di Argo non sono mai
    # auto-allow, richiedono un OK esplicito dal telefono (§8). 0 = mano libera.
    self_protect: bool = os.environ.get("ARGO_SELF_PROTECT", "1") != "0"

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

    # --- settorializzazione LLM: routing per peso (missione settorializzazione) --
    # registro dei tier caricato da ARGO_MODEL_TIERS (JSON) o dal template default.
    model_tiers: list[ModelTier] = field(default_factory=_load_model_tiers)
    # preset globale di pazienza: patient | balanced | fast (override per-conv/task).
    patience: str = os.environ.get("ARGO_PATIENCE", "balanced")
    # margine di VRAM da lasciare libero per lo scheduler VRAM-aware (§A.6).
    vram_headroom_mb: int = int(os.environ.get("ARGO_VRAM_HEADROOM_MB", "1024"))
    # cap separato per i task 'frontier' (subscription): non consumano VRAM ma non
    # devono floodare l'API (§A.6).
    sub_concurrency: int = int(os.environ.get("ARGO_SUB_CONCURRENCY", "2"))

    # --- autofix loop feedback-driven (missione autofix) ------------------------
    # numero di tentativi PER TIER locale (include il tentativo iniziale). Deve
    # restare coerente/subordinato a max_local_retries: qui e' il conteggio dei
    # tentativi per tier, non un budget globale.
    autofix_max_rounds: int = int(os.environ.get("ARGO_AUTOFIX_MAX_ROUNDS", "3"))
    # modelli Ollama piu' forti da provare dopo il primario, in ordine (os.pathsep).
    # vuoto (default) = solo il modello primario, comportamento retro-compatibile.
    autofix_local_tiers: list[str] = field(
        default_factory=lambda: _env_list("ARGO_AUTOFIX_LOCAL_TIERS"))
    # righe di coda dell'output/diff da iniettare nel fix-brief (§7).
    autofix_diff_tail_lines: int = int(
        os.environ.get("ARGO_AUTOFIX_DIFF_TAIL_LINES", "80"))
    # se true e il repo e' git: `git checkout --` sui soli files_allowed prima di
    # ogni tentativo, per ripartire pulito invece di costruire sul lavoro parziale.
    autofix_reset_between_attempts: bool = (
        os.environ.get("ARGO_AUTOFIX_RESET", "0") == "1"
    )

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
        """Le root note (document_root + self_root, in testa) piu' le eventuali
        ARGO_ROOTS extra, deduplicate. document_root e' SEMPRE presente: cosi'
        anche con ARGO_ROOTS vuoto non e' "tutto negato" (§A.1). self_root e' la
        scelta extra "se stesso" (Addendum), non la destinazione di default."""
        known = [self.document_root.resolve(), self.self_root.resolve()]
        extra = [Path(r).resolve() for r in self.repo_roots]
        out: list[Path] = []
        for p in known + extra:
            if p not in out:
                out.append(p)
        return out


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
