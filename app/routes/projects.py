"""Project: dà una casa al repo_path (tabella §5.1). Il repo va dentro le root (4.3)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..config import get_settings
from ..db import get_db, utcnow
from ..ids import new_id
from ..security import resolve_within_roots, PathNotAllowed

router = APIRouter(prefix="/projects")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


class NewProject(BaseModel):
    name: str
    repo_path: str


@router.get("")
async def list_projects(request: Request):
    """Stessa route per due consumatori (§ handoff): il browser (Accept: text/html)
    ottiene la pagina Progetti; le fetch di app.js/API ottengono il JSON."""
    rows = get_db().query("SELECT * FROM project ORDER BY created_at DESC")
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(request, "projects.html", {
            "request": request, "projects": rows,
        })
    return [dict(r) for r in rows]


@router.post("")
async def create_project(body: NewProject):
    roots = get_settings().resolved_roots()
    try:
        resolved = resolve_within_roots(body.repo_path, roots)
    except PathNotAllowed as e:
        raise HTTPException(status_code=422, detail=f"repo_path non consentito: {e}")
    pid = new_id("proj")
    get_db().execute(
        "INSERT INTO project(id, name, repo_path, created_at) VALUES(?,?,?,?)",
        (pid, body.name, str(resolved), utcnow()),
    )
    return {"id": pid, "repo_path": str(resolved)}
