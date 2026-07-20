"""Router per peso (settorializzazione, §A.3/A.5). LOGICA PURA: nessun import SDK.

Il planner PROPONE (brief.complexity/criticality/latency_tolerance), il CODICE
DECIDE — stesso pattern di `validate_plan`. `route()` e' deterministico e
documentato: data una coppia (task, hardware) produce SEMPRE la stessa
`RouteDecision`, con una `reason` leggibile che finisce nell'evento e nella UI.

Il router e' anche la scala di escalation dell'autofix: `next_tier()` restituisce
il tier immediatamente superiore *disponibile* (salta i locali che non entrano in
VRAM, arriva sempre a 'frontier').
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ModelTier, PatiencePolicy, TIER_ORDER, patience_policy
from .hardware import HardwareProfile


@dataclass
class RouteDecision:
    tier: str
    backend: str
    model: str
    concurrency_weight: int   # ~ est_vram_mb (per lo scheduler VRAM, §A.6)
    timeout_s: int
    autofix_local_rounds: int
    reason: str


# mappa complexity -> tier candidato (§A.5 passo 2)
_COMPLEXITY_TO_TIER = {"light": "light", "mid": "mid", "heavy": "heavy"}

# keyword-bucket per la stima deterministica (§A.3). Meccanico -> light;
# algoritmico -> heavy; il resto -> mid.
_LIGHT_KW = re.compile(
    r"\b(rename|rinomina|format|formatta|docstring|import|typo|refuso|"
    r"add field|aggiungi campo|comment|commento|whitespace|lint)\b", re.I)
_HEAVY_KW = re.compile(
    r"\b(implement|implementa|refactor|refactoring|optimi[sz]e|ottimizza|"
    r"concurrency|concorrenza|design|progetta|algorit|migrazione|migration|"
    r"async|thread|schedul|architett)\b", re.I)


def estimate_complexity(brief) -> str:
    """Fallback e sanity-check DETERMINISTICO (l'autorita' finale e' il codice).

    Segnali: n. file, lunghezza istruzioni, max_turns, profondita' depends_on,
    keyword-bucket. Ritorna 'light' | 'mid' | 'heavy'.
    """
    text = f"{getattr(brief, 'title', '')} {getattr(brief, 'instructions', '')}"

    # segnali forti da keyword (hanno la precedenza sul dimensionamento grezzo).
    heavy_kw = bool(_HEAVY_KW.search(text))
    light_kw = bool(_LIGHT_KW.search(text))

    n_files = len(getattr(brief, "files_allowed", []) or [])
    n_instr = len(getattr(brief, "instructions", "") or "")
    max_turns = int(getattr(brief, "max_turns", 25) or 25)
    n_deps = len(getattr(brief, "depends_on", []) or [])

    # punteggio dimensionale
    score = 0
    if n_files >= 4:
        score += 2
    elif n_files >= 2:
        score += 1
    if n_instr >= 800:
        score += 2
    elif n_instr >= 300:
        score += 1
    if max_turns >= 40:
        score += 2
    elif max_turns >= 25:
        score += 1
    if n_deps >= 2:
        score += 1

    if heavy_kw or score >= 4:
        return "heavy"
    if light_kw and score <= 1:
        return "light"
    if score <= 1:
        return "light"
    return "mid"


def _find_tier(tiers: list[ModelTier], name: str) -> ModelTier | None:
    for t in tiers:
        if t.name == name:
            return t
    return None


def available_tiers(profile: HardwareProfile, tiers: list[ModelTier],
                    headroom_mb: int = 1024) -> list[ModelTier]:
    """Tier utilizzabili su QUESTA macchina (§A.2). Tiene i tier locali che
    entrano in VRAM (`est_vram_mb + headroom <= vram_free`) e SEMPRE i tier
    subscription. Senza GPU misurabile (fallback), niente filtro: tutti i tier
    restano, cosi' il comportamento di default (Ollama locale) non si rompe."""
    if profile.gpu_name is None or profile.vram_total_mb <= 0:
        return list(tiers)  # niente misura VRAM -> nessun vincolo (backward compat)
    budget = profile.vram_free_mb
    out: list[ModelTier] = []
    for t in tiers:
        if t.backend != "ollama":
            out.append(t)  # subscription sempre disponibile
        elif t.est_vram_mb + headroom_mb <= budget:
            out.append(t)
    return out


def tier_warnings(profile: HardwareProfile, tiers: list[ModelTier]) -> list[str]:
    """Warning di config (§A.2): un modello locale del registro non e' fra gli
    `installed_models` di Ollama. Ritorna i dettagli; l'executor li emette come
    evento `config_warning` (il router resta puro)."""
    if not profile.installed_models:
        return []  # non so cosa e' installato: non allarmo a vuoto
    installed = set(profile.installed_models)

    def _known(model: str) -> bool:
        if model in installed:
            return True
        # tag esplicito (es. ':14b') -> serve match ESATTO: '14b' != '7b'.
        # senza tag (es. 'qwen3-coder') -> match tollerante col ':latest' & simili.
        if ":" in model:
            return False
        return any(m == model or m.split(":")[0] == model for m in installed)

    warnings: list[str] = []
    for t in tiers:
        if t.backend == "ollama" and t.model and not _known(t.model):
            warnings.append(
                f"tier '{t.name}': modello '{t.model}' non risulta installato in "
                f"Ollama (installati: {sorted(installed)})")
    return warnings


def _effective_policy(brief, policy: PatiencePolicy) -> PatiencePolicy:
    """Override per-task (brief.latency_tolerance) sul preset globale (§A.4)."""
    lt = getattr(brief, "latency_tolerance", None)
    if lt == "impatient":
        return policy if policy.preset == "fast" else patience_policy("fast")
    if lt == "patient":
        return policy if policy.preset == "patient" else patience_policy("patient")
    return policy


def _bump(name: str) -> str:
    """Tier immediatamente superiore nell'ordine canonico (satura a 'frontier')."""
    if name not in TIER_ORDER:
        return "frontier"
    idx = min(TIER_ORDER.index(name) + 1, len(TIER_ORDER) - 1)
    return TIER_ORDER[idx]


def _build_decision(tier_name: str, complexity: str, eff_policy: PatiencePolicy,
                    tiers: list[ModelTier], reason: str) -> RouteDecision:
    tier = _find_tier(tiers, tier_name)
    if tier is None:
        # registro senza quel tier: ripiega su un subscription sintetico (frontier).
        tier = ModelTier(tier_name, "subscription", "", 0, 4, 5)
    is_local = tier.backend == "ollama"
    rounds = eff_policy.local_rounds.get(complexity, 1) if is_local else 1
    timeout = eff_policy.latency_budget_s.get(complexity, 900)
    return RouteDecision(
        tier=tier.name, backend=tier.backend, model=tier.model,
        concurrency_weight=tier.est_vram_mb, timeout_s=timeout,
        autofix_local_rounds=max(1, rounds), reason=reason,
    )


def route(brief, profile: HardwareProfile, tiers: list[ModelTier],
          policy: PatiencePolicy, headroom_mb: int = 1024) -> RouteDecision:
    """Deterministico (§A.5). Passi: complexity -> tier candidato -> bump per
    criticality -> pazienza -> VINCOLO HARDWARE (declassa o escala a frontier)."""
    complexity = getattr(brief, "complexity", None) or estimate_complexity(brief)
    eff = _effective_policy(brief, policy)
    criticality = getattr(brief, "criticality", "normal") or "normal"
    reasons: list[str] = [f"complexity={complexity}"]

    target = _COMPLEXITY_TO_TIER.get(complexity, "mid")

    # 3. criticality 'high' -> sale di un tier (correttezza critica).
    if criticality == "high":
        target = _bump(target)
        reasons.append(f"criticality=high -> +1 tier ({target})")

    # 4. pazienza: fast/impaziente -> consenti il salto a frontier su mid/heavy;
    #    patient -> resta locale, accetta 'heavy' lento.
    if eff.preset == "fast" and target in ("mid", "heavy"):
        target = "frontier"
        reasons.append("fast/impaziente -> frontier")
    elif eff.preset == "patient" and target == "frontier" and criticality != "high":
        target = "heavy"
        reasons.append("patient -> resta locale (heavy)")

    # 5. VINCOLO HARDWARE.
    avail = available_tiers(profile, tiers, headroom_mb)
    avail_names = {t.name for t in avail}
    chosen = _find_tier(tiers, target)
    if chosen is not None and chosen.backend == "ollama" and target not in avail_names:
        local_fit = [t for t in avail if t.backend == "ollama"]
        if criticality == "high" or not local_fit:
            target = "frontier"
            reasons.append("VRAM insufficiente"
                           + (" + criticality alta" if criticality == "high" else "")
                           + " -> frontier")
        else:
            best = max(local_fit, key=lambda t: TIER_ORDER.index(t.name))
            target = best.name
            reasons.append(f"VRAM insufficiente per '{chosen.name}' -> declassa a '{target}'")

    return _build_decision(target, complexity, eff, tiers, "; ".join(reasons))


def next_tier(current: str, brief, profile: HardwareProfile,
              tiers: list[ModelTier], policy: PatiencePolicy,
              headroom_mb: int = 1024) -> RouteDecision | None:
    """Escalation dell'autofix: il tier immediatamente superiore DISPONIBILE dopo
    `current` (salta i locali che non entrano in VRAM). None se `current` e' gia'
    in cima ('frontier')."""
    complexity = getattr(brief, "complexity", None) or estimate_complexity(brief)
    eff = _effective_policy(brief, policy)
    avail_names = {t.name for t in available_tiers(profile, tiers, headroom_mb)}
    if current not in TIER_ORDER:
        return None
    for name in TIER_ORDER[TIER_ORDER.index(current) + 1:]:
        if name in avail_names:
            return _build_decision(
                name, complexity, eff, tiers,
                f"escalation autofix da '{current}' a '{name}'")
    return None
