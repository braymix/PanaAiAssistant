"""Comandi di sistema: riavvio/spegnimento dell'app, spegnimento del PC, reset
totale (pulizia DB + riavvio dei servizi in-process).

Queste operazioni sono IRREVERSIBILI e toccano il processo o l'OS: ognuna, come
il purge (§B.3), richiede confirm=true nel chiamante ed emette un evento SSE
PRIMA di agire, cosi' il telefono sa cosa succede anche se poi cade la
connessione.

Separazione LOGICA/EFFETTI (stesso spirito di `router.py` e `lifecycle.py`):
`wipe_database`, `build_restart_command`, `build_poweroff_command` e le guardie
sono funzioni PURE e testabili; gli effetti OS (uccidere il processo,
rilanciarlo, spegnere il PC) passano per `SystemEffects`, un contenitore di
callable iniettabile — i test ne passano di finti, cosi' la CI non si riavvia
da sola.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .db import Database

# --- pulizia totale del DB -------------------------------------------------------
# Tabelle "dati operativi": svuotate dal reset. Lo schema resta intatto. L'ordine
# rispetta le dipendenze logiche (figli prima dei genitori), anche senza FK reali.
_DATA_TABLES = [
    "event", "approval", "usage_sample", "run", "task",
    "plan_document", "message", "conversation", "app_state",
]


def wipe_database(db: Database, *, keep_push: bool = True,
                  keep_projects: bool = True) -> dict[str, int]:
    """Svuota le tabelle dati del DB. Ritorna il conteggio delle righe rimosse
    per tabella (per l'evento e la UI).

    `keep_push=True` (default): NON tocca push_subscription — perderla obbliga a
    ri-attivare la push dal telefono. `keep_projects=True`: lascia i progetti
    (config utente, non spazzatura operativa). Metti entrambi a False per un wipe
    davvero totale."""
    tables = list(_DATA_TABLES)
    if not keep_projects:
        tables.append("project")
    if not keep_push:
        tables.append("push_subscription")
    removed: dict[str, int] = {}
    for t in tables:
        row = db.query_one(f"SELECT COUNT(*) AS c FROM {t}")
        removed[t] = int(row["c"]) if row else 0
        db.execute(f"DELETE FROM {t}")
    # recupera lo spazio su disco: best-effort, non deve far fallire il wipe.
    try:
        db.execute("VACUUM")
    except Exception:  # noqa: BLE001 — VACUUM opzionale, il wipe e' gia' fatto
        pass
    return removed


# --- costruzione dei comandi shell (pura, testabile) ----------------------------
def build_restart_command(restart_cmd: str, delay_s: float, *,
                          is_windows: bool | None = None) -> str:
    """Comando shell (staccato) che ASPETTA la morte del vecchio processo e poi
    rilancia l'app. `restart_cmd` vuoto -> auto: interprete corrente + `-m
    app.main`. L'attesa (delay+3s) evita che il nuovo processo trovi la porta
    ancora occupata dal vecchio."""
    if is_windows is None:
        is_windows = os.name == "nt"
    base = restart_cmd.strip() or f'"{sys.executable}" -m app.main'
    wait = max(1, int(delay_s) + 3)
    if is_windows:
        # `timeout /t N` attende; `&` concatena i comandi in cmd.exe.
        return f'cmd /c "timeout /t {wait} /nobreak >nul & {base}"'
    return f'sh -c "sleep {wait}; {base}"'


def build_poweroff_command(poweroff_cmd: str, *,
                           is_windows: bool | None = None) -> str:
    """Comando per spegnere il PC. Vuoto -> auto per piattaforma."""
    if poweroff_cmd.strip():
        return poweroff_cmd.strip()
    if is_windows is None:
        is_windows = os.name == "nt"
    return "shutdown /s /t 0" if is_windows else "shutdown -h now"


# --- effetti OS iniettabili ------------------------------------------------------
@dataclass
class SystemEffects:
    """Effetti OS iniettabili. A runtime: reali; nei test: finti (registrano le
    chiamate senza spegnere nulla)."""
    run_detached: Callable[[str, "Path | None"], None]
    terminate_self: Callable[[], None]
    schedule: Callable[[float, Callable[[], None]], None]


def _default_run_detached(command: str, cwd: "Path | None") -> None:
    """Lancia un comando shell in un processo STACCATO che sopravvive alla morte
    del parent (serve al rilancio dopo un riavvio)."""
    kwargs: dict = {"shell": True, "cwd": str(cwd) if cwd else None}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: nessuna console ereditata.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)  # noqa: S602 — comando fidato (config)


def _default_terminate_self() -> None:
    """Termina QUESTO processo. SIGTERM: uvicorn lo intercetta per uno shutdown
    pulito; su Windows os.kill(SIGTERM) chiama TerminateProcess (comunque
    efficace)."""
    os.kill(os.getpid(), signal.SIGTERM)


def _default_schedule(delay_s: float, fn: Callable[[], None]) -> None:
    threading.Timer(max(0.0, delay_s), fn).start()


def default_effects() -> SystemEffects:
    return SystemEffects(
        run_detached=_default_run_detached,
        terminate_self=_default_terminate_self,
        schedule=_default_schedule,
    )


_effects: SystemEffects | None = None


def get_effects() -> SystemEffects:
    global _effects
    if _effects is None:
        _effects = default_effects()
    return _effects


def set_effects(e: SystemEffects | None) -> None:
    """Per i test: sostituisce (o azzera) gli effetti OS."""
    global _effects
    _effects = e


# --- azioni a livello di processo/OS --------------------------------------------
def shutdown_app(settings, effects: SystemEffects) -> None:
    """Spegne l'app: termina il processo dopo la grazia (la risposta HTTP e
    l'evento SSE partono prima)."""
    effects.schedule(settings.system_action_delay_s, effects.terminate_self)


def restart_app(settings, effects: SystemEffects) -> None:
    """Riavvia l'app: lancia un rilancio staccato che aspetta la morte del vecchio
    processo, poi termina questo processo."""
    cmd = build_restart_command(settings.restart_cmd, settings.system_action_delay_s)
    cwd = Path(settings.self_root)

    def _do() -> None:
        effects.run_detached(cmd, cwd)
        effects.terminate_self()

    effects.schedule(settings.system_action_delay_s, _do)


def poweroff_pc(settings, effects: SystemEffects) -> None:
    """Spegne il PC dopo la grazia."""
    cmd = build_poweroff_command(settings.poweroff_cmd)
    effects.schedule(settings.system_action_delay_s,
                     lambda: effects.run_detached(cmd, None))


# --- riavvio dei servizi in-process (a caldo, senza toccare il processo) --------
async def restart_services() -> dict[str, int]:
    """Riavvio 'a caldo' dei servizi in-process: annulla il lavoro in volo e
    ricrea il pool executor (scheduler VRAM compreso) e il broker approvazioni.
    NON tocca il processo ne' il bus SSE, cosi' il telefono resta agganciato."""
    import app.approvals as approvalsmod
    import app.executor as executormod
    cancelled = 0
    if executormod._pool is not None:
        cancelled = await executormod._pool.cancel_all()
    executormod._pool = None
    approvalsmod._broker = None
    return {"cancelled_plans": cancelled}


async def factory_reset(db: Database, settings, *, keep_push: bool = True,
                        keep_projects: bool = True) -> dict:
    """Pulizia totale del DB + riavvio totale dei servizi. Ordine: prima si ferma
    il lavoro in volo (niente scritture su tabelle che stiamo per svuotare), poi
    si svuota il DB, poi si ricreano i servizi."""
    import app.approvals as approvalsmod
    import app.executor as executormod
    cancelled = 0
    if executormod._pool is not None:
        cancelled = await executormod._pool.cancel_all()
    removed = wipe_database(db, keep_push=keep_push, keep_projects=keep_projects)
    executormod._pool = None
    approvalsmod._broker = None
    return {"removed": removed, "cancelled_plans": cancelled}
