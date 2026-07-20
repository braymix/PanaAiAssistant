"""Planner (M3) — modello forte in plan mode. Non tocca file (§3.1).

Due usi:
  * chat interattiva (stream SSE), per raffinare il piano dal divano;
  * tasto VIA -> genera un PlanDocument JSON (§5.2) e lo valida in codice.

Il system prompt del VIA istruisce il planner sulle REGOLE FERREE (§5.2):
verify_cmd obbligatorio, files_allowed = perimetro, istruzioni letterali per un
modello locale stupido. La validazione in codice (validate_plan) e' comunque
l'ultima parola: rifiuta se un solo task e' privo di verify_cmd.
"""

from __future__ import annotations

import json
import logging
import platform
from pathlib import Path, PurePosixPath, PureWindowsPath

from .backends import make_planner_options, make_via_options
from .briefs import PlanDocument, PlanValidationError, validate_plan
from .config import get_settings
from .db import get_db, utcnow
from .events import get_bus
from .ids import new_id
from .security import resolve_within_roots, PathNotAllowed

log = logging.getLogger("argo.planner")

PLAN_CHAT_SYSTEM = (
    "Sei il planner di Argo. Sei in plan mode: NON tocchi file, discuti e "
    "raffini un piano con l'utente, che lavora dal telefono. Rispondi conciso: "
    "ogni schermata deve essere utile in 2 secondi in piedi."
)

# Modalita' RICERCA ONLINE: si APPENDE al system prompt di Claude Code (non lo
# sostituisce), cosi' plan mode e il resto restano intatti.
RESEARCH_APPEND = (
    "MODALITA' RICERCA ONLINE. L'utente vuole un documento di ricerca affidabile "
    "e aggiornato. Regole:\n"
    "- Usa ATTIVAMENTE WebSearch per trovare fatti recenti; non fidarti solo della "
    "memoria, che puo' essere datata.\n"
    "- Verifica ogni affermazione importante su PIU' fonti indipendenti prima di "
    "darla per certa. Distingui fatti accertati da ipotesi/indiscrezioni.\n"
    "- CITA SEMPRE le fonti (titolo + URL) e riporta le date.\n"
    "- Struttura il risultato come un documento markdown: sommario, sezioni "
    "tematiche, cronologia, e una sezione 'Fonti' finale con i link.\n"
    "- Quando produci il PlanDocument, i task locali devono ASSEMBLARE e formattare "
    "il materiale che TU (planner) hai gia' raccolto e messo nel context di ogni "
    "TaskBrief: i modelli locali non hanno accesso al web, quindi il contenuto "
    "verificato deve stare nel brief, non essere 'cercato' da loro."
)

VIA_SYSTEM = (
    "Produci UN SOLO oggetto JSON valido, senza testo attorno, che rappresenti un "
    "PlanDocument per Argo. Schema:\n"
    '{"repo_path": str, "summary": str, "tasks": [TaskBrief...]}\n'
    "Ogni TaskBrief:\n"
    '{"id","title","depends_on":[],"files_allowed":[...],"context","instructions",'
    '"acceptance","verify_cmd","verify_cwd":".","max_turns":25,"timeout_s":900,'
    '"complexity","criticality"}\n'
    "SETTORIALIZZAZIONE (per il routing verso il modello giusto):\n"
    "- complexity: stima il PESO del task, uno di \"light\" (meccanico: rename, "
    "format, docstring, aggiunta campo), \"mid\" (lavoro normale) o \"heavy\" "
    "(algoritmico/di design: implementazione non banale, refactor, concorrenza, "
    "migrazione). E' una STIMA: il codice la puo' correggere.\n"
    "- criticality: quanto e' critico che sia CORRETTO, uno di \"low\", \"normal\" "
    "(default) o \"high\" (un errore qui e' costoso: sicurezza, dati, contratti "
    "pubblici). I task 'high' vengono instradati a un modello piu' forte.\n"
    "REGOLE FERREE:\n"
    "- verify_cmd e' OBBLIGATORIO ed eseguibile. Un task senza verify_cmd fa "
    "RIFIUTARE tutto il piano.\n"
    "- verify_cmd DEVE essere CROSS-PLATFORM e girare sul sistema operativo "
    "dell'utente (te lo dico nel messaggio). NON usare comandi Unix come test, "
    "grep, ls, cat, [ -f ], head: su Windows NON esistono e falliscono sempre. "
    "Usa SEMPRE Python (garantito). Per verificare un file di testo:\n"
    "  python -c \"import sys,pathlib; t=pathlib.Path('FILE.md').read_text("
    "encoding='utf-8'); sys.exit(0 if ('Titolo' in t and 'Sezione' in t) else 1)\"\n"
    "  Per codice: 'python -m pytest tests/x.py -q'.\n"
    "- files_allowed elenca ESATTAMENTE i file che il task puo' toccare (percorsi "
    "RELATIVI a repo_path): e' il perimetro che l'umano approva. Non vuoto.\n"
    "- Lo eseguira' un modello locale stupido e letterale: context e instructions "
    "devono essere completi, passo-passo, imperativi, zero ambiguita'.\n"
    "- depends_on referenzia altri id dello stesso piano.\n"
    "Nessun commento, nessun markdown: solo JSON."
)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # rimuove eventuale fence ```json ... ```
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Nessun JSON nel testo del planner.")
    return json.loads(text[start:end + 1])


# classe client iniettabile (i test ne passano una finta; a runtime e' l'SDK)
_CLIENT_CLS = None


def _get_client_cls():
    global _CLIENT_CLS
    if _CLIENT_CLS is None:
        from claude_agent_sdk import ClaudeSDKClient
        _CLIENT_CLS = ClaudeSDKClient
    return _CLIENT_CLS


def set_client_cls(cls) -> None:
    """Per i test: sostituisce il client SDK con un fake."""
    global _CLIENT_CLS
    _CLIENT_CLS = cls


def _is_absolute_any(f: str) -> bool:
    """Assoluto secondo la semantica Windows O Posix (robusto cross-platform)."""
    return PureWindowsPath(f).is_absolute() or PurePosixPath(f).is_absolute()


def _basename_if_absolute(f: str) -> str:
    # PureWindowsPath.name gestisce sia '\' sia '/', quindi ricava il nome file
    # anche per un path Posix; per i relativi lascia tutto invariato.
    return PureWindowsPath(f).name if _is_absolute_any(f) else f


def _safe_cwd(repo_cwd: str, settings) -> str:
    """Ritorna una working dir valida e dentro le root; altrimenti ripiega su
    document_root (§A.4), non su '.' (app dir). Cosi' chat e piani senza progetto
    esplicito lavorano dentro `document`, non nella cartella dell'app."""
    roots = settings.resolved_roots()
    try:
        resolved = resolve_within_roots(repo_cwd or str(settings.document_root), roots)
        resolved.mkdir(parents=True, exist_ok=True)
        return str(resolved)
    except (PathNotAllowed, OSError):
        pass
    # fallback: document_root, creata se manca. Ultimo ripiego "." solo se
    # nemmeno document_root e' utilizzabile (path Windows su CI senza override).
    try:
        settings.document_root.mkdir(parents=True, exist_ok=True)
        return str(settings.document_root.resolve())
    except OSError:
        return "."


def _conversation_mode(db, conversation_id: str) -> str:
    row = db.query_one("SELECT mode FROM conversation WHERE id=?", (conversation_id,))
    return (row["mode"] if row and row["mode"] else "generic")


def _latest_planner_session(db, conversation_id: str) -> str | None:
    """Ultima sessione del planner per questa conversazione (§1.7 -> resume)."""
    row = db.query_one(
        "SELECT session_id FROM run WHERE conversation_id=? AND backend='subscription' "
        "AND session_id IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1",
        (conversation_id,),
    )
    return row["session_id"] if row else None


def _record_planner_run(db, conversation_id: str, session_id: str | None,
                        cost: float = 0.0) -> None:
    from .ids import new_id
    db.execute(
        "INSERT INTO run(id, task_id, conversation_id, session_id, backend, model, "
        "status, cost_usd, started_at, ended_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (new_id("run"), None, conversation_id, session_id, "subscription",
         get_settings().subscription_model or "default", "done", cost,
         utcnow(), utcnow()),
    )


def _is_resume_failure(exc: Exception) -> bool:
    """Vero se l'SDK e' fallito all'avvio perche' la sessione da riprendere non
    esiste piu' nel CLI (store ripulito, altra macchina, sessione scaduta): il
    CLI stampa 'No conversation found with session ID: ...' ed esce con codice 1.
    Lo stderr catturato e' a volte generico, quindi trattiamo come recuperabile
    ogni errore dell'SDK avvenuto mentre stavamo riprendendo una sessione: in tal
    caso ripartire da zero e' meglio che rispondere 500."""
    try:
        from claude_agent_sdk import ClaudeSDKError
    except Exception:  # noqa: BLE001 — senza SDK non c'e' nulla da recuperare
        ClaudeSDKError = ()
    if isinstance(exc, ClaudeSDKError):
        return True
    blob = f"{exc} {getattr(exc, 'stderr', '') or ''}".lower()
    return "no conversation found" in blob or "session id" in blob


async def _ask_phone_gate(tool_name, input_data, context):
    # Il planner e' in plan mode e ha Write/Edit/Bash/AskUserQuestion gia'
    # DISALLOWED (backends.PLANNER_DISALLOWED): tutto cio' che arriva qui e' un
    # tool di sola pianificazione (WebSearch, Task/sub-agenti, Read/Glob/Grep) che
    # NON muta il repo. Auto-allow: niente push, niente timeout. Le domande di
    # chiarimento il planner le fa come testo in chat, e l'utente risponde scrivendo.
    from claude_agent_sdk import PermissionResultAllow
    await get_bus().emit(None, "policy_auto_allow",
                         {"tool_name": tool_name, "reason": "planner (plan mode)"})
    return PermissionResultAllow()


async def chat_stream(conversation_id: str, repo_cwd: str, user_text: str,
                      resume_session: str | None = None):
    """Esegue un turno di chat in plan mode; yield-a i pezzi di testo (per SSE).

    Persiste il messaggio utente e la risposta. Ritorna (via return) il session_id.
    """
    db = get_db()
    bus = get_bus()
    db.execute(
        "INSERT INTO message(conversation_id, role, content, ts) VALUES(?,?,?,?)",
        (conversation_id, "user", user_text, utcnow()),
    )

    settings = get_settings()
    # cwd valido: se la cartella non esiste o e' fuori dalle root, ripiega su "."
    # (evita WinError 267 "Nome di directory non valido" all'avvio della CLI).
    repo_cwd = _safe_cwd(repo_cwd, settings)
    # continuita' della conversazione: se non passato, riprendi l'ultima sessione
    if resume_session is None:
        resume_session = _latest_planner_session(db, conversation_id)
    is_research = _conversation_mode(db, conversation_id) == "research"

    def _options(resume):
        opts = make_planner_options(settings, repo_cwd, _ask_phone_gate)
        if resume:
            opts.resume = resume
        # modalita' 'research': APPEND al preset claude_code, non lo sostituisce
        if is_research:
            opts.system_prompt = {"type": "preset", "preset": "claude_code",
                                  "append": RESEARCH_APPEND}
        return opts

    # `streamed` accumula i pezzi e fa anche da flag "ho gia' emesso dei delta":
    # se il primo tentativo fallisce PRIMA di emettere, possiamo ripartire pulito
    # senza rischiare delta duplicati.
    streamed: list[str] = []

    async def _run_turn(resume):
        sid = resume
        async with _get_client_cls()(options=_options(resume)) as client:
            await client.query(user_text)
            async for msg in client.receive_response():
                data = getattr(msg, "data", None)
                if getattr(msg, "subtype", None) == "init" and isinstance(data, dict):
                    sid = data.get("session_id") or sid
                if type(msg).__name__ == "AssistantMessage":
                    for block in getattr(msg, "content", []) or []:
                        if type(block).__name__ == "TextBlock":
                            chunk = getattr(block, "text", "")
                            streamed.append(chunk)
                            await bus.emit(None, "chat_delta", {
                                "conversation_id": conversation_id, "text": chunk,
                            })
        return sid

    try:
        session_id = await _run_turn(resume_session)
    except Exception as e:  # noqa: BLE001
        # sessione da riprendere non piu' valida: riparti da zero, una volta sola
        if resume_session and not streamed and _is_resume_failure(e):
            log.warning("resume del planner fallito (%s); riparto senza sessione", e)
            session_id = await _run_turn(None)
        else:
            raise

    full = "".join(streamed)
    db.execute(
        "INSERT INTO message(conversation_id, role, content, ts) VALUES(?,?,?,?)",
        (conversation_id, "assistant", full, utcnow()),
    )
    # persisti la sessione (§1.7): senza, il turno dopo ripartirebbe da zero
    if session_id:
        _record_planner_run(db, conversation_id, session_id)
    await bus.emit(None, "chat_done", {
        "conversation_id": conversation_id, "session_id": session_id,
    })
    return session_id


async def generate_plan(conversation_id: str, repo_path: str,
                        resume_session: str | None = None) -> str:
    """Tasto VIA: genera, valida e persiste un PlanDocument. Ritorna plan_id.

    Solleva PlanValidationError se un task e' privo di verify_cmd (§5.2).
    """
    settings = get_settings()
    db = get_db()
    # cwd valido e dentro le root, creato se manca: senza, la CLI fallisce con
    # WinError 267. repo_path diventa cosi' anche il repo_path (assoluto) del piano.
    repo_path = _safe_cwd(repo_path, settings)
    # il piano deve riflettere la discussione: riprendi la sessione del planner
    if resume_session is None:
        resume_session = _latest_planner_session(db, conversation_id)
    research_note = ""
    if _conversation_mode(db, conversation_id) == "research":
        research_note = (
            "MODALITA' RICERCA: i modelli locali NON hanno accesso al web. Percio' "
            "il contenuto verificato (fatti, date, citazioni, URL delle fonti) che "
            "hai raccolto DEVE stare INTERO nel campo context di ogni TaskBrief; il "
            "task locale deve solo formattarlo/assemblarlo, non 'cercarlo'.\n")

    via_query = (
        f"Genera ORA il PlanDocument JSON per la feature discussa.\n"
        f"{research_note}"
        f"Sistema operativo dell'utente: {platform.system()}. Ogni verify_cmd "
        f"DEVE funzionare qui: se Windows, NIENTE test/grep/ls/cat, usa "
        f"python -c \"...\" (vedi regole).\n"
        f"repo_path DEVE essere ESATTAMENTE: {repo_path}\n"
        f"In files_allowed usa SOLO percorsi RELATIVI a repo_path (es. "
        f"'README.md' o 'sezioni/intro.md'), MAI percorsi assoluti e "
        f"MAI cartelle diverse da repo_path.\n"
        f"Rispondi con UN SOLO oggetto JSON, niente altro testo."
    )

    def _options(resume):
        # NON plan mode (vedi make_via_options): serve JSON, non un piano proposto.
        opts = make_via_options(settings, repo_path, _ask_phone_gate)
        if resume:
            opts.resume = resume
        opts.system_prompt = VIA_SYSTEM   # inietta il contratto d'output del VIA
        return opts

    async def _run(resume):
        raw_text: list[str] = []
        cost = 0.0
        async with _get_client_cls()(options=_options(resume)) as client:
            await client.query(via_query)
            async for msg in client.receive_response():
                if type(msg).__name__ == "AssistantMessage":
                    for block in getattr(msg, "content", []) or []:
                        if type(block).__name__ == "TextBlock":
                            raw_text.append(getattr(block, "text", ""))
                if type(msg).__name__ == "ResultMessage":
                    cost = getattr(msg, "total_cost_usd", 0) or 0.0
                    # fallback: alcune risposte arrivano solo nel campo result
                    res = getattr(msg, "result", None)
                    if res and not raw_text:
                        raw_text.append(str(res))
        return raw_text, cost

    try:
        raw_text, cost = await _run(resume_session)
    except Exception as e:  # noqa: BLE001
        # sessione da riprendere non piu' valida: riparti da zero, una volta sola
        if resume_session and _is_resume_failure(e):
            log.warning("resume del VIA fallito (%s); riparto senza sessione", e)
            raw_text, cost = await _run(None)
        else:
            raise

    joined = "".join(raw_text).strip()
    if not joined:
        raise PlanValidationError(
            "Il planner non ha prodotto testo. Controlla: hai prima descritto la "
            "feature in chat? La CLI `claude` e' installata e loggata? "
            "(vedi RUNBOOK Fase C)")
    try:
        data = _extract_json(joined)
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("VIA: JSON non estraibile. Testo del planner:\n%s", joined[:2000])
        raise PlanValidationError(
            f"Il planner non ha risposto in JSON ({e}). Anteprima: "
            f"{joined[:300]!r}. Riprova chiedendo in chat un piano piu' concreto.")
    # FORZA il repo_path al progetto scelto: il planner tende a inventarne uno
    # (es. C:\Users\...\Documents\Progetto) che cade fuori dalle root e fa fallire
    # tutto. E' l'utente col progetto a decidere DOVE si scrive, non il planner.
    data["repo_path"] = repo_path
    plan = PlanDocument.from_dict(data)
    # ri-radica dentro il repo: percorsi assoluti -> solo nome file; verify_cwd
    # assoluto -> ".". Cosi' i files_allowed cadono sempre dentro il progetto.
    for t in plan.tasks:
        t.files_allowed = [_basename_if_absolute(f) for f in t.files_allowed]
        if t.verify_cwd and _is_absolute_any(t.verify_cwd):
            t.verify_cwd = "."
    validate_plan(plan)   # rifiuta se manca un verify_cmd (§5.2)

    plan_id = new_id("plan")
    db.execute(
        "INSERT INTO plan_document(id, conversation_id, status, raw_json, cost_usd, "
        "created_at) VALUES(?,?,?,?,?,?)",
        (plan_id, conversation_id, "draft", json.dumps(plan.to_dict()), cost,
         utcnow()),
    )
    for seq, t in enumerate(plan.tasks):
        db.execute(
            "INSERT INTO task(id, plan_id, seq, title, brief_json, status, backend, "
            "attempts, depends_on) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("t"), plan_id, seq, t.title, json.dumps(t.to_dict()),
             "pending", "ollama", 0, json.dumps(t.depends_on)),
        )
    return plan_id
