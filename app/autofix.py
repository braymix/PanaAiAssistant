"""Autofix feedback-driven per gli executor locali (missione autofix).

Logica PURA sul carattere di `policy.py`: zero SDK a livello di modulo, cosi' e'
testabile da sola. L'orchestratore (`executor.py`) usa queste funzioni per
trasformare il retry cieco in un loop di autofix informato dall'errore:

  1. `classify_failure()` — diagnosi DETERMINISTICA del fallimento (regex sul
     tail dell'output). Non ci si fida MAI dell'autovalutazione del modello (§5.2).
  2. `snapshot_changes()` — capisce SE e QUALI file del perimetro sono cambiati,
     senza dipendere da git.
  3. `build_fix_prompt()` — costruisce il prompt del tentativo di fix con l'errore
     reale in coda e un hint mirato per classe (§7 del brief).

Invarianti rispettate qui: `verify_cmd` non viene mai riscritto (lo si stampa,
non lo si esegue); il perimetro `files_allowed` resta il solo insieme di file
toccabili (§3.2/§4.3).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FailureClass(str, Enum):
    VERIFY_ASSERTION = "verify_assertion"   # test/asserzione fallita (exit!=0, output di test)
    SYNTAX_ERROR = "syntax_error"           # SyntaxError/IndentationError/parse error
    IMPORT_ERROR = "import_error"           # ModuleNotFound/ImportError
    MISSING_FILE = "missing_file"           # FileNotFound / file atteso non creato
    NO_CHANGE = "no_change"                 # il modello non ha modificato nessun file
    PERIMETER_BLOCK = "perimeter_block"     # tentata Write fuori files_allowed (policy_deny/ask)
    TIMEOUT_LOOP = "timeout_loop"           # timeout wall-clock del tentativo (§1.9)
    VERIFY_INFRA = "verify_infra"           # verify_cmd rotto: command not found / verify_cwd errato
    UNKNOWN = "unknown"


# --- regex deterministiche (l'errore sta quasi sempre in coda all'output) --------
_RE_IMPORT = re.compile(r"ModuleNotFoundError|ImportError|No module named", re.I)
_RE_SYNTAX = re.compile(r"SyntaxError|IndentationError|TabError|unexpected EOF", re.I)
_RE_MISSING = re.compile(r"FileNotFoundError|No such file or directory", re.I)
_RE_ASSERT = re.compile(r"AssertionError|\bFAILED\b|^E\s|assert ", re.I | re.M)
# verify_cmd rotto: la shell non trova il comando, oppure il cwd non esiste.
_RE_INFRA = re.compile(
    r"command not found|is not recognized|WinError|"
    r"No such file or directory: cwd|verify_cwd fuori dalle root|"
    r"cannot find the path|\b127: ",
    re.I,
)


def classify_failure(*, verify_exit: int | None, verify_output: str,
                     run_error: str | None, changed_files: list[str],
                     policy_events: list[dict]) -> FailureClass:
    """Euristica deterministica. Ordine di priorita' (dal brief):

        infra > timeout > perimeter > no_change > import > syntax >
        missing_file > assertion > unknown

    Non fidarti MAI dell'autovalutazione del modello: qui conta solo l'output
    reale del verify e i fatti osservati (file cambiati, eventi di policy).
    """
    out = verify_output or ""
    err = run_error or ""

    # 1. infra: verify_cmd rotto (command not found / cwd errato / WinError).
    #    Va PRIMA di tutto: se non e' nemmeno partito il verify, ogni altra
    #    diagnosi sull'output e' rumore.
    if _RE_INFRA.search(out) or _RE_INFRA.search(err):
        return FailureClass.VERIFY_INFRA

    # 2. timeout wall-clock del tentativo (§1.9): probabile loop.
    if "timeout wall-clock" in err or "timeout wall-clock" in out:
        return FailureClass.TIMEOUT_LOOP

    # 3. perimetro: il modello ha provato a scrivere fuori da files_allowed e il
    #    PolicyGate ha emesso un policy_deny/ask su Write|Edit.
    for ev in policy_events or []:
        kind = ev.get("kind", "")
        tool = ev.get("tool_name", "")
        if kind in ("policy_deny", "policy_ask") and tool in ("Write", "Edit"):
            return FailureClass.PERIMETER_BLOCK

    # 4. no_change: nessun file del perimetro toccato e nessun problema d'infra.
    if not changed_files:
        return FailureClass.NO_CHANGE

    # 5-8. diagnosi sull'output del test (l'errore e' in coda).
    if _RE_IMPORT.search(out):
        return FailureClass.IMPORT_ERROR
    if _RE_SYNTAX.search(out):
        return FailureClass.SYNTAX_ERROR
    if _RE_MISSING.search(out):
        return FailureClass.MISSING_FILE
    if _RE_ASSERT.search(out):
        return FailureClass.VERIFY_ASSERTION

    return FailureClass.UNKNOWN


def snapshot_changes(repo: Path, files_allowed_rel: list[str]) -> dict[str, str]:
    """{path_rel: sha256(contenuto)} per i SOLI file del perimetro.

    Serve a capire (deterministicamente) SE e QUALI file un tentativo ha toccato,
    confrontando pre/post attempt, senza dipendere da git. Un file assente non
    compare nella mappa: cosi' passare da 'assente' a 'presente' (o viceversa)
    conta come cambiamento.
    """
    snap: dict[str, str] = {}
    for rel in files_allowed_rel:
        p = repo / rel
        try:
            data = p.read_bytes()
        except (OSError, FileNotFoundError):
            continue  # assente: non entra nella mappa (l'assenza e' informativa)
        snap[rel] = hashlib.sha256(data).hexdigest()
    return snap


def diff_changed(pre: dict[str, str], post: dict[str, str]) -> list[str]:
    """File del perimetro il cui contenuto (o presenza) e' cambiato tra pre e post."""
    keys = set(pre) | set(post)
    return sorted(f for f in keys if pre.get(f) != post.get(f))


def git_diff_tail(repo: Path, files_allowed_rel: list[str],
                  max_lines: int) -> str | None:
    """`git diff -- <files>` troncato alle ultime `max_lines` righe, se il repo e'
    git. Non-distruttivo (solo lettura). Ritorna None se git non c'e' / non e' un
    repo / non ci sono modifiche, cosi' il chiamante fa fallback ai path cambiati.
    """
    import subprocess  # lazy: logica pura di default
    if not (repo / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "diff", "--", *files_allowed_rel],
            cwd=str(repo), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


@dataclass
class AttemptRecord:
    attempt: int
    backend: str            # "ollama" | "ollama:<model2>" | "subscription"
    failure_class: FailureClass
    verify_exit: int | None
    output_tail: str        # ultime N righe: gli errori stanno in fondo
    changed_files: list[str] = field(default_factory=list)


# Hint mirato per classe (§7). Imperativo, per modelli locali letterali.
HINTS: dict[FailureClass, str] = {
    FailureClass.SYNTAX_ERROR:
        "C'e' un errore di sintassi. Vai al file/riga indicati nel traceback e "
        "correggi li'.",
    FailureClass.IMPORT_ERROR:
        "Un import fallisce. Controlla nome del modulo/percorso; non installare "
        "pacchetti, usa quelli gia' presenti.",
    FailureClass.VERIFY_ASSERTION:
        "Un'asserzione/test fallisce. Confronta valore atteso vs ottenuto "
        "nell'output e allinea il comportamento del codice, MAI il test.",
    FailureClass.MISSING_FILE:
        "Un file atteso non esiste. Crealo nel percorso indicato, dentro il "
        "perimetro.",
    FailureClass.NO_CHANGE:
        "Il tentativo precedente non ha modificato alcun file. Questa volta "
        "applica una modifica concreta con Write/Edit.",
    FailureClass.PERIMETER_BLOCK:
        "Hai provato a scrivere fuori dai file consentiti. Ottieni il risultato "
        "modificando SOLO i file elencati.",
    FailureClass.TIMEOUT_LOOP:
        "Il tentativo e' andato in timeout (probabile loop). Fai UNA modifica "
        "decisa e fermati; non ripetere lo stesso tool all'infinito.",
    FailureClass.VERIFY_INFRA:
        "Il comando di verifica non e' partito correttamente (comando o cwd). "
        "NON modificare il comando di verifica: sistema il codice/i file perche' "
        "il comando esistente possa girare.",
    FailureClass.UNKNOWN:
        "Causa non classificata. Leggi con attenzione l'output qui sotto e "
        "applica la correzione minima indicata dall'errore.",
}


def build_fix_prompt(brief, history: list[AttemptRecord],
                     diff_tail: str | None, tail_lines: int) -> str:
    """Prompt del tentativo di fix (§7). `brief` e' un TaskBrief (import evitato
    per non creare cicli: si usano solo attributi).

    Garanzie verificabili dai test:
      * ripete titolo/acceptance;
      * mostra TESTUALMENTE `verify_cmd` e il tail dell'output fallito (tagliato
        dal FONDO: l'errore sta in coda);
      * dichiara la FailureClass e l'hint per classe;
      * vincola: correggi SOLO dentro files_allowed, NON modificare verify_cmd,
        unico criterio di successo = verify_cmd exit 0.
    """
    last = history[-1] if history else None
    fc = last.failure_class if last else FailureClass.UNKNOWN
    verify_exit = last.verify_exit if last else None
    hint = HINTS.get(fc, HINTS[FailureClass.UNKNOWN])

    # tail dell'output: taglia dal FONDO (l'errore e' quasi sempre in coda).
    output_tail = _tail_lines(last.output_tail if last else "", tail_lines)

    # "cosa hai gia' cambiato": preferisci il git diff, altrimenti la lista dei
    # file toccati per tentativo (fatti osservati, non autodichiarazioni).
    if diff_tail:
        changed_block = diff_tail
    else:
        lines = []
        for rec in history:
            files = ", ".join(rec.changed_files) if rec.changed_files else "(nessun file)"
            lines.append(f"- tentativo {rec.attempt} [{rec.backend}] "
                         f"({rec.failure_class.value}): {files}")
        changed_block = "\n".join(lines) if lines else "(nessuna modifica finora)"

    files_block = "\n".join(f"- {f}" for f in brief.files_allowed)

    return (
        f"# FIX — Task: {brief.title}\n\n"
        "Il tentativo precedente NON ha superato la verifica. Correggi e riprova.\n\n"
        "## Criterio di accettazione (unico giudice: il comando di verifica qui sotto)\n"
        f"{brief.acceptance}\n\n"
        "## Comando di verifica (NON modificarlo, NON toccare i file di test)\n"
        f"$ {brief.verify_cmd}     (cwd: {brief.verify_cwd})\n\n"
        f"## Diagnosi automatica del fallimento: {fc.value}\n"
        f"{hint}\n\n"
        f"## Output REALE dell'ultimo tentativo (exit {verify_exit}) — leggi da qui la causa:\n"
        f"{output_tail}\n"
        f"# ultime {tail_lines} righe; l'errore e' quasi sempre in fondo\n\n"
        "## Cosa hai gia' cambiato finora (non rifare gli stessi errori):\n"
        f"{changed_block}\n\n"
        "## File che puoi toccare (SOLO questi — tutto il resto e' vietato dal perimetro):\n"
        f"{files_block}\n\n"
        "## Istruzioni\n"
        "1. Individua la causa dall'output sopra, non indovinare.\n"
        "2. Applica la correzione MINIMA dentro i file consentiti.\n"
        "3. Non dichiararti \"finito\": e' il comando di verifica a deciderlo.\n"
    )


def _tail_lines(text: str, n: int) -> str:
    lines = (text or "").splitlines()
    if len(lines) > n:
        lines = lines[-n:]
    return "\n".join(lines)
