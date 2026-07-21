"""Pagine HTML mobile-first. Tema scuro, azioni in basso (§4.9-14)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_db()
    # conversazioni + stato dell'ultimo piano collegato (per la chip "recente")
    convs = db.query(
        "SELECT c.*, (SELECT p.status FROM plan_document p "
        " WHERE p.conversation_id=c.id AND p.deleted_at IS NULL "
        " ORDER BY p.created_at DESC LIMIT 1) "
        " AS last_plan_status "
        "FROM conversation c WHERE c.deleted_at IS NULL "
        "ORDER BY c.created_at DESC LIMIT 20")
    # contatori per i badge delle tile (reali)
    draft_plans = db.query_one(
        "SELECT COUNT(*) c FROM plan_document WHERE status='draft'")["c"]
    active_runs = db.query_one(
        "SELECT COUNT(*) c FROM run WHERE status='running'")["c"]
    pending_count = db.query_one(
        "SELECT COUNT(*) c FROM approval WHERE status='pending'")["c"]
    # agenti PC registrati (§C.5): la sezione "PC Agenti" li itera. Lo stato live
    # (🔴/🟢/⚠️) lo aggiorna il client via /agents; qui solo nome/icona/descrizione.
    from ..pc_agents import registry
    agents = [{"name": a.name, "icon": a.icon, "description": a.description}
              for a in registry.all_agents()]
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "conversations": convs,
        "draft_plans": draft_plans, "active_runs": active_runs,
        "pending_count": pending_count, "agents": agents,
        "user": getattr(request.state, "user", "?"),
    })


@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request):
    plans = get_db().query(
        "SELECT p.id, p.status, p.created_at, "
        "(SELECT COUNT(*) FROM task t WHERE t.plan_id=p.id "
        " AND t.deleted_at IS NULL) AS n_tasks "
        "FROM plan_document p WHERE p.deleted_at IS NULL "
        "ORDER BY p.created_at DESC LIMIT 50")
    return templates.TemplateResponse(request, "plans.html", {
        "request": request, "plans": plans,
    })


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    runs = get_db().query(
        "SELECT r.id, r.backend, r.model, r.status, r.started_at, "
        "t.title AS task_title FROM run r LEFT JOIN task t ON r.task_id=t.id "
        "ORDER BY r.started_at DESC, r.id DESC LIMIT 50")
    return templates.TemplateResponse(request, "runs.html", {
        "request": request, "runs": runs,
    })


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request):
    approvals = get_db().query(
        "SELECT a.id, a.tool_name, a.pushed_at, t.title AS task_title "
        "FROM approval a LEFT JOIN run r ON a.run_id=r.id "
        "LEFT JOIN task t ON r.task_id=t.id "
        "WHERE a.status='pending' ORDER BY a.pushed_at DESC", ())
    return templates.TemplateResponse(request, "approvals.html", {
        "request": request, "approvals": approvals,
    })


@router.get("/chat/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: str):
    db = get_db()
    conv = db.query_one("SELECT * FROM conversation WHERE id=?", (conversation_id,))
    msgs = db.query(
        "SELECT * FROM message WHERE conversation_id=? ORDER BY id ASC",
        (conversation_id,))
    # repo_path viaggia via query param (?repo=): conversation §5.1 non ha la colonna.
    # Default (§A.4): document_root, cosi' l'utente non incolla mai il path.
    from ..config import get_settings
    repo = request.query_params.get("repo") or str(get_settings().document_root)
    return templates.TemplateResponse(request, "chat.html", {
        "request": request, "conversation": conv, "messages": msgs, "repo": repo,
    })


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
async def plan_page(request: Request, plan_id: str):
    db = get_db()
    plan = db.query_one("SELECT * FROM plan_document WHERE id=?", (plan_id,))
    tasks = db.query("SELECT * FROM task WHERE plan_id=? AND deleted_at IS NULL "
                     "ORDER BY seq", (plan_id,))
    briefs = [json.loads(t["brief_json"]) for t in tasks]
    raw = json.loads(plan["raw_json"]) if plan else {}
    return templates.TemplateResponse(request, "plan.html", {
        "request": request, "plan": plan, "tasks": tasks, "briefs": briefs,
        "all_files": raw.get("tasks") and _collect_files(raw), "summary": raw.get("summary", ""),
    })


@router.get("/ollama", response_class=HTMLResponse)
async def ollama_page(request: Request):
    return templates.TemplateResponse(request, "ollama.html", {"request": request})


@router.get("/openclaw", response_class=HTMLResponse)
async def openclaw_page(request: Request):
    """Pagina dedicata OpenClaw (§B.6). Alias comodo di /agents/openclaw."""
    return _agent_page(request, "openclaw")


@router.get("/agents/{name}", response_class=HTMLResponse)
async def agent_page(request: Request, name: str):
    """Pagina generica di un agente PC (§C.5)."""
    return _agent_page(request, name)


def _agent_page(request: Request, name: str) -> HTMLResponse:
    from ..pc_agents import registry
    agent = registry.get(name)
    meta = None
    if agent is not None:
        meta = {"name": agent.name, "icon": agent.icon,
                "description": agent.description}
    return templates.TemplateResponse(request, "openclaw.html", {
        "request": request, "agent": meta, "name": name,
    })


@router.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request):
    """Navigatore mobile-first della cartella `document` (§B.5). I dati arrivano
    via fetch da /documents/tree; il deep link Obsidian da /documents/config."""
    return templates.TemplateResponse(request, "documents.html", {"request": request})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str):
    db = get_db()
    run = db.query_one("SELECT * FROM run WHERE id=?", (run_id,))
    task = None
    if run and run["task_id"]:
        task = db.query_one("SELECT * FROM task WHERE id=?", (run["task_id"],))
    return templates.TemplateResponse(request, "run.html", {
        "request": request, "run": run, "task": task,
    })


@router.get("/approvals/{approval_id}", response_class=HTMLResponse)
async def approval_page(request: Request, approval_id: str):
    db = get_db()
    apr = db.query_one("SELECT * FROM approval WHERE id=?", (approval_id,))
    tool_input = {}
    if apr and apr["tool_input"]:
        try:
            tool_input = json.loads(apr["tool_input"])
        except ValueError:
            tool_input = {"raw": apr["tool_input"]}
    return templates.TemplateResponse(request, "approval.html", {
        "request": request, "approval": apr,
        "tool_input_pretty": json.dumps(tool_input, indent=2, ensure_ascii=False),
    })


def _collect_files(raw: dict) -> list[str]:
    seen: list[str] = []
    for t in raw.get("tasks", []):
        for f in t.get("files_allowed", []):
            if f not in seen:
                seen.append(f)
    return seen
