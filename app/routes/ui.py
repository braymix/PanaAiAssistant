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
    convs = db.query("SELECT * FROM conversation ORDER BY created_at DESC LIMIT 20")
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "conversations": convs,
        "user": getattr(request.state, "user", "?"),
    })


@router.get("/chat/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: str):
    db = get_db()
    conv = db.query_one("SELECT * FROM conversation WHERE id=?", (conversation_id,))
    msgs = db.query(
        "SELECT * FROM message WHERE conversation_id=? ORDER BY id ASC",
        (conversation_id,))
    # repo_path viaggia via query param (?repo=): conversation §5.1 non ha la colonna
    repo = request.query_params.get("repo", "")
    return templates.TemplateResponse(request, "chat.html", {
        "request": request, "conversation": conv, "messages": msgs, "repo": repo,
    })


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
async def plan_page(request: Request, plan_id: str):
    db = get_db()
    plan = db.query_one("SELECT * FROM plan_document WHERE id=?", (plan_id,))
    tasks = db.query("SELECT * FROM task WHERE plan_id=? ORDER BY seq", (plan_id,))
    briefs = [json.loads(t["brief_json"]) for t in tasks]
    raw = json.loads(plan["raw_json"]) if plan else {}
    return templates.TemplateResponse(request, "plan.html", {
        "request": request, "plan": plan, "tasks": tasks, "briefs": briefs,
        "all_files": raw.get("tasks") and _collect_files(raw), "summary": raw.get("summary", ""),
    })


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
