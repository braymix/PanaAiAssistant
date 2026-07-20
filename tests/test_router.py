"""Router per peso (§A). LOGICA PURA: nessun SDK, nessuna GPU, nessun Ollama.

Tabelle deterministiche: estimate_complexity e route (complexity x criticality x
pazienza x hardware -> tier atteso), declassamento VRAM, frontier forzato,
backward-compat dei brief senza complexity.
"""

import pytest

from app.briefs import TaskBrief
from app.config import default_model_tiers, patience_policy
from app.hardware import HardwareProfile
from app.router import (
    available_tiers, estimate_complexity, next_tier, route, tier_warnings,
)

TIERS = default_model_tiers()
ALL_MODELS = ["qwen2.5-coder:7b", "qwen3-coder", "qwen2.5-coder:14b"]


def _profile(vram_free=13000, gpu="RTX 3080 Ti", total=12288, installed=None):
    return HardwareProfile(
        gpu_name=gpu, vram_total_mb=total if gpu else 0,
        vram_free_mb=vram_free if gpu else 0, ram_mb=32000, cpu_cores=16,
        installed_models=ALL_MODELS if installed is None else installed,
    )


def _brief(complexity=None, criticality="normal", latency=None,
           instructions="fai la cosa", files=("a.py",), max_turns=25, deps=()):
    return TaskBrief(
        id="t1", title="task", files_allowed=list(files), context="",
        instructions=instructions, acceptance="", verify_cmd="pytest -q",
        max_turns=max_turns, depends_on=list(deps),
        complexity=complexity, criticality=criticality, latency_tolerance=latency)


# =============================================================================
# estimate_complexity — tabella
# =============================================================================
@pytest.mark.parametrize("instr,files,turns,expected", [
    ("rinomina la variabile x", ("a.py",), 10, "light"),
    ("add field email al modello", ("a.py",), 10, "light"),
    ("fix typo nel docstring", ("a.py",), 10, "light"),
    ("implementa l'algoritmo di scheduling", ("a.py",), 25, "heavy"),
    ("refactor del modulo con concorrenza async", ("a.py",), 25, "heavy"),
    ("progetta la migrazione dello schema", ("a.py",), 25, "heavy"),
    ("aggiorna il testo", ("a.py",), 10, "light"),
])
def test_estimate_complexity_keywords(instr, files, turns, expected):
    assert estimate_complexity(_brief(instructions=instr, files=files,
                                      max_turns=turns)) == expected


def test_estimate_complexity_size_drives_heavy():
    # nessuna keyword, ma molti file + istruzioni lunghe + molti turni -> heavy
    b = _brief(instructions="x" * 900, files=("a.py", "b.py", "c.py", "d.py"),
               max_turns=40)
    assert estimate_complexity(b) == "heavy"


def test_estimate_complexity_default_mid():
    # segnali medi, nessuna keyword -> mid
    b = _brief(instructions="y" * 400, files=("a.py", "b.py"), max_turns=25)
    assert estimate_complexity(b) == "mid"


# =============================================================================
# route — tabella complexity x criticality x pazienza x hardware
# =============================================================================
def test_route_light_normal_balanced():
    d = route(_brief(complexity="light"), _profile(), TIERS,
              patience_policy("balanced"))
    assert d.tier == "light" and d.backend == "ollama"
    assert d.model == "qwen2.5-coder:7b"
    assert d.concurrency_weight == 6000


def test_route_heavy_normal_balanced():
    d = route(_brief(complexity="heavy"), _profile(), TIERS,
              patience_policy("balanced"))
    assert d.tier == "heavy" and d.backend == "ollama"


def test_route_high_criticality_bumps_one_tier():
    d = route(_brief(complexity="mid", criticality="high"), _profile(), TIERS,
              patience_policy("balanced"))
    assert d.tier == "heavy"   # mid -> +1 -> heavy


def test_route_fast_patience_jumps_to_frontier():
    d = route(_brief(complexity="mid"), _profile(), TIERS, patience_policy("fast"))
    assert d.tier == "frontier" and d.backend == "subscription"


def test_route_patient_stays_local_on_heavy():
    # heavy + high criticality con preset patient: high bumpa heavy->frontier, ma
    # patient lo riporta a locale 'heavy' (criticality alta non forza qui perche'
    # il declassamento patient vale solo se criticality != high)...
    d = route(_brief(complexity="heavy"), _profile(), TIERS,
              patience_policy("patient"))
    assert d.tier == "heavy" and d.backend == "ollama"


def test_route_impatient_task_override_forces_frontier():
    # override per-task: latency_tolerance impaziente su una policy globale patient
    d = route(_brief(complexity="heavy", latency=None), _profile(), TIERS,
              patience_policy("patient"))
    assert d.tier == "heavy"
    d2 = route(_brief(complexity="heavy", latency="impatient"), _profile(), TIERS,
               patience_policy("patient"))
    assert d2.tier == "frontier"


def test_route_high_criticality_and_impatient_forces_frontier():
    d = route(_brief(complexity="heavy", criticality="high", latency="impatient"),
              _profile(), TIERS, patience_policy("balanced"))
    assert d.tier == "frontier" and d.backend == "subscription"


# =============================================================================
# VINCOLO HARDWARE — declassamento e escalation forzata
# =============================================================================
def test_route_downgrades_when_vram_insufficient():
    # solo 'light' entra (7024 <= 8000); mid/heavy no -> heavy declassa a light
    prof = _profile(vram_free=8000)
    d = route(_brief(complexity="heavy"), prof, TIERS, patience_policy("balanced"),
              headroom_mb=1024)
    assert d.tier == "light" and d.backend == "ollama"
    assert "declassa" in d.reason


def test_route_high_criticality_escalates_instead_of_downgrading():
    # mid + high -> heavy; heavy non entra in VRAM; criticality alta -> frontier
    prof = _profile(vram_free=8000)
    d = route(_brief(complexity="mid", criticality="high"), prof, TIERS,
              patience_policy("balanced"), headroom_mb=1024)
    assert d.tier == "frontier" and d.backend == "subscription"


def test_route_no_local_fits_escalates_to_frontier():
    # VRAM minuscola: nessun locale entra -> frontier anche a criticality normale
    prof = _profile(vram_free=2000)
    d = route(_brief(complexity="light"), prof, TIERS, patience_policy("balanced"))
    assert d.tier == "frontier"


# =============================================================================
# backward compat — brief senza complexity instrada comunque
# =============================================================================
def test_route_without_complexity_uses_estimate():
    b = _brief(complexity=None, instructions="implementa l'algoritmo")
    d = route(b, _profile(), TIERS, patience_policy("balanced"))
    assert d.tier == "heavy"   # stimato heavy dalle keyword


def test_route_no_gpu_keeps_local_routing():
    # senza GPU: nessun filtro VRAM, routing locale normale (backward compat)
    prof = HardwareProfile(gpu_name=None, vram_total_mb=0, vram_free_mb=0,
                           ram_mb=16000, cpu_cores=8, installed_models=[])
    d = route(_brief(complexity="heavy"), prof, TIERS, patience_policy("balanced"))
    assert d.tier == "heavy" and d.backend == "ollama"


# =============================================================================
# available_tiers / next_tier / tier_warnings
# =============================================================================
def test_available_tiers_filters_by_vram():
    names = {t.name for t in available_tiers(_profile(vram_free=8000), TIERS, 1024)}
    assert "light" in names and "frontier" in names
    assert "heavy" not in names and "mid" not in names


def test_available_tiers_no_gpu_returns_all():
    prof = HardwareProfile(None, 0, 0, 0, 0, [])
    assert len(available_tiers(prof, TIERS, 1024)) == len(TIERS)


def test_next_tier_walks_up_and_stops_at_frontier():
    prof = _profile()
    pol = patience_policy("balanced")
    assert next_tier("light", _brief(), prof, TIERS, pol).tier == "mid"
    assert next_tier("mid", _brief(), prof, TIERS, pol).tier == "heavy"
    assert next_tier("heavy", _brief(), prof, TIERS, pol).tier == "frontier"
    assert next_tier("frontier", _brief(), prof, TIERS, pol) is None


def test_next_tier_skips_local_that_does_not_fit():
    # 8000 free: da 'light' il prossimo locale (mid/heavy) non entra -> frontier
    prof = _profile(vram_free=8000)
    d = next_tier("light", _brief(), prof, TIERS, patience_policy("balanced"))
    assert d.tier == "frontier"


def test_tier_warnings_flags_uninstalled_model():
    prof = _profile(installed=["qwen2.5-coder:7b"])   # manca 14b e qwen3-coder
    warns = tier_warnings(prof, TIERS)
    assert any("14b" in w for w in warns)


def test_tier_warnings_tolerant_latest_suffix():
    prof = _profile(installed=["qwen2.5-coder:7b", "qwen3-coder:latest",
                               "qwen2.5-coder:14b"])
    assert tier_warnings(prof, TIERS) == []


def test_tier_warnings_empty_when_installed_unknown():
    prof = _profile(installed=[])   # non so cosa e' installato: niente allarmi
    assert tier_warnings(prof, TIERS) == []
