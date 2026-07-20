"""Profilo hardware auto-rilevato (missione settorializzazione, §A.1).

LOGICA PURA: nessun import dell'SDK a livello di modulo (criterio d'accettazione).
Serve al router per sapere COSA regge la macchina: VRAM libera, modelli Ollama
installati, presenza di GPU. L'auto-rilevamento e' il default; l'override via
`ARGO_HW_PROFILE` (JSON) e' l'eccezione (macchine senza nvidia-smi, CI, test).

Rilevamento:
  * VRAM: `nvidia-smi --query-gpu=memory.total,memory.free,name` (parsing robusto;
    assente -> gpu_name=None, vram_*=0 -> il router ripiega sul cap semplice).
  * Modelli installati: GET {ollama_url}/api/tags (HTTP, niente shell).

Il profilo e' rilevato UNA volta all'avvio e messo in cache; `refresh()` lo forza.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HardwareProfile:
    gpu_name: str | None
    vram_total_mb: int
    vram_free_mb: int
    ram_mb: int
    cpu_cores: int
    installed_models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gpu_name": self.gpu_name,
            "vram_total_mb": self.vram_total_mb,
            "vram_free_mb": self.vram_free_mb,
            "ram_mb": self.ram_mb,
            "cpu_cores": self.cpu_cores,
            "installed_models": list(self.installed_models),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "HardwareProfile":
        return HardwareProfile(
            gpu_name=d.get("gpu_name"),
            vram_total_mb=int(d.get("vram_total_mb", 0) or 0),
            vram_free_mb=int(d.get("vram_free_mb", 0) or 0),
            ram_mb=int(d.get("ram_mb", 0) or 0),
            cpu_cores=int(d.get("cpu_cores", 0) or 0),
            installed_models=list(d.get("installed_models") or []),
        )


# --- rilevamento VRAM (nvidia-smi) ----------------------------------------------
def _detect_gpu() -> tuple[str | None, int, int]:
    """(gpu_name, vram_total_mb, vram_free_mb). Assente -> (None, 0, 0).

    Parsing robusto: prende la PRIMA GPU della lista CSV; ignora righe malformate.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.total,memory.free,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, 0, 0
    if proc.returncode != 0:
        return None, 0, 0
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total = int(float(parts[0]))
            free = int(float(parts[1]))
        except (ValueError, IndexError):
            continue
        name = parts[2] or None
        return name, total, free
    return None, 0, 0


# --- rilevamento RAM / CPU ------------------------------------------------------
def _detect_ram_mb() -> int:
    try:
        # POSIX: sysconf; robusto e senza dipendenze.
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass
    # Windows: GlobalMemoryStatusEx via ctypes.
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return int(stat.ullTotalPhys / (1024 * 1024))
    except Exception:  # noqa: BLE001 — best-effort: RAM non nota -> 0
        return 0


def _detect_cpu_cores() -> int:
    return os.cpu_count() or 0


# --- modelli Ollama installati (HTTP, niente shell) -----------------------------
def _detect_installed_models(ollama_url: str) -> list[str]:
    import urllib.error
    import urllib.request

    url = ollama_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — loopback
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    models = data.get("models") or []
    names: list[str] = []
    for m in models:
        name = m.get("name") or m.get("model")
        if name:
            names.append(name)
    return names


def _override_from_env() -> HardwareProfile | None:
    raw = os.environ.get("ARGO_HW_PROFILE", "").strip()
    if not raw:
        return None
    try:
        return HardwareProfile.from_dict(json.loads(raw))
    except (ValueError, TypeError):
        return None


def detect_profile(settings) -> HardwareProfile:
    """Rileva il profilo hardware. `ARGO_HW_PROFILE` (JSON) ha la precedenza.

    Se l'override e' presente ma non specifica `installed_models`, li completa via
    Ollama (utile in dev: fisso la VRAM ma leggo i modelli reali)."""
    override = _override_from_env()
    if override is not None:
        if not override.installed_models:
            override.installed_models = _detect_installed_models(settings.ollama_url)
        return override

    gpu_name, vram_total, vram_free = _detect_gpu()
    return HardwareProfile(
        gpu_name=gpu_name,
        vram_total_mb=vram_total,
        vram_free_mb=vram_free,
        ram_mb=_detect_ram_mb(),
        cpu_cores=_detect_cpu_cores(),
        installed_models=_detect_installed_models(settings.ollama_url),
    )


# --- cache di modulo ------------------------------------------------------------
_profile: HardwareProfile | None = None


def get_profile(settings) -> HardwareProfile:
    global _profile
    if _profile is None:
        _profile = detect_profile(settings)
    return _profile


def refresh(settings) -> HardwareProfile:
    """Forza un nuovo rilevamento (es. dopo che Ollama ha scaricato un modello)."""
    global _profile
    _profile = detect_profile(settings)
    return _profile


def set_profile(profile: HardwareProfile | None) -> None:
    """Per i test: inietta un profilo (o None per resettare la cache)."""
    global _profile
    _profile = profile
