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

from .backends import make_planner_options
from .briefs import PlanDocument, validate_plan
from .config import get_settings
from .db import get_db, utcnow
from .events import get_bus
from .ids import new_id

PLAN_CHAT_SYSTEM = (
    "Sei il planner di Argo. Sei in plan mode: NON tocchi file, discuti e "
    "raffini un piano con l'utente, che lavora dal telefono. Rispondi conciso: "
    "ogni schermata deve essere utile in 2 secondi in piedi."
)

VIA_SYSTEM = (
    "Produci UN SOLO oggetto JSON valido, senza testo attorno, che rappresenti un "
    "PlanDocument per Argo. Schema:\n"
    '{"repo_path": str, "summary": str, "tasks": [TaskBrief...]}\n'
    "Ogni TaskBrief:\n"
    '{"id","title","depends_on":[],"files_allowed":[...],"context","instructions",'
    '"acceptance","verify_cmd","verify_cwd":".","max_turns":25,"timeout_s":900}\n'
    "REGOLE FERREE:\n"
    "- verify_cmd e' OBBLIGATORIO ed eseguibile (es. 'pytest tests/x.py -q'). "
    "Un task senza verify_cmd fa RIFIUTARE tutto il piano.\n"
    "- files_allowed elenca ESATTAMENTE i file che il task puo' toccare: e' il "
    "perimetro che l'umano approva. Non lasciarlo vuoto.\n"
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


async def _ask_phone_gate(tool_name, input_data, context):
    # Il planner in plan mode non dovrebbe toccare nulla; se ci prova, chiedi.
    from .approvals import get_broker
    # run_id None: usa un canale globale per l'eccezione del planner
    decision = await get_broker().request(
        run_id="planner", tool_name=tool_name, tool_input=input_data,
        title="Il planner ha tentato un'azione in plan mode",
    )
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
    if decision.status == "allowed":
        return PermissionResultAllow(updated_input=decision.updated_input)
    return PermissionResultDeny(message=decision.reason or "negato")


async def chat_stream(conversation_id: str, repo_cwd: str, user_text: str,
                      resume_session: str | None = None):
    """Esegue un turno di chat in plan mode; yield-a i pezzi di testo (per SSE).

    Persiste il messaggio utente e la risposta. Ritorna (via return) il session_id.
    """
    from claude_agent_sdk import ClaudeSDKClient

    db = get_db()
    bus = get_bus()
    db.execute(
        "INSERT INTO message(conversation_id, role, content, ts) VALUES(?,?,?,?)",
        (conversation_id, "user", user_text, utcnow()),
    )

    settings = get_settings()
    options = make_planner_options(settings, repo_cwd, _ask_phone_gate)
    if resume_session:
        options.resume = resume_session

    assistant_text: list[str] = []
    session_id = resume_session

    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_text)
        async for msg in client.receive_response():
            name = type(msg).__name__
            data = getattr(msg, "data", None)
            if getattr(msg, "subtype", None) == "init" and isinstance(data, dict):
                session_id = data.get("session_id") or session_id
            if name == "AssistantMessage":
                for block in getattr(msg, "content", []) or []:
                    if type(block).__name__ == "TextBlock":
                        chunk = getattr(block, "text", "")
                        assistant_text.append(chunk)
                        await bus.emit(None, "chat_delta", {
                            "conversation_id": conversation_id, "text": chunk,
                        })

    full = "".join(assistant_text)
    db.execute(
        "INSERT INTO message(conversation_id, role, content, ts) VALUES(?,?,?,?)",
        (conversation_id, "assistant", full, utcnow()),
    )
    await bus.emit(None, "chat_done", {
        "conversation_id": conversation_id, "session_id": session_id,
    })
    return session_id


async def generate_plan(conversation_id: str, repo_path: str,
                        resume_session: str | None = None) -> str:
    """Tasto VIA: genera, valida e persiste un PlanDocument. Ritorna plan_id.

    Solleva PlanValidationError se un task e' privo di verify_cmd (§5.2).
    """
    from claude_agent_sdk import ClaudeSDKClient

    settings = get_settings()
    db = get_db()
    options = make_planner_options(settings, repo_path, _ask_phone_gate)
    if resume_session:
        options.resume = resume_session
    # inietta il contratto d'output del VIA
    options.system_prompt = VIA_SYSTEM

    raw_text: list[str] = []
    cost = 0.0
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Genera ORA il PlanDocument JSON per quanto discusso. Solo JSON."
        )
        async for msg in client.receive_response():
            if type(msg).__name__ == "AssistantMessage":
                for block in getattr(msg, "content", []) or []:
                    if type(block).__name__ == "TextBlock":
                        raw_text.append(getattr(block, "text", ""))
            if type(msg).__name__ == "ResultMessage":
                cost = getattr(msg, "total_cost_usd", 0) or 0.0

    data = _extract_json("".join(raw_text))
    if not data.get("repo_path"):
        data["repo_path"] = repo_path
    plan = PlanDocument.from_dict(data)
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
