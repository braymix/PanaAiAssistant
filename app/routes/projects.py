"""Project: dà una casa al repo_path (tabella §5.1). Il repo va dentro le root (4.3).

Casa di default: `document_root` (§A.3). L'utente crea un progetto col solo nome
e vive sotto `document_root/nome`; non digita mai il path. Due voci implicite
"pinnate" (Documenti di default + Argo se stesso) sono sempre in cima (Addendum §2).
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath, PurePosixPath

from fastapi import APIRouter, HTTPException, Request
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
    # opzionale (§A.3): se assente o relativo, il progetto vive dentro document_root.
    repo_path: str | None = None


def _is_absolute_any(f: str) -> bool:
    """Assoluto secondo la semantica Windows O Posix (robusto cross-platform)."""
    return PureWindowsPath(f).is_absolute() or PurePosixPath(f).is_absolute()


def implicit_projects(settings) -> list[dict]:
    """Le due scelte "pinnate" sempre disponibili, in testa alla lista (Addendum §2):
    Documenti (casa di default) e Argo — codice (il sorgente, se stesso)."""
    return [
        {"id": "proj-documents", "name": "Documenti (default)",
         "repo_path": str(settings.document_root), "kind": "default", "pinned": True},
        {"id": "proj-self", "name": "Argo — codice (se stesso)",
         "repo_path": str(settings.self_root), "kind": "self", "pinned": True},
    ]


def _candidate_path(settings, name: str, repo_path: str | None) -> str:
    """Ricava il path del progetto (§A.3): senza repo_path -> document_root/name;
    repo_path relativo -> document_root/repo_path; assoluto -> lasciato com'e'
    (verra' comunque validato dentro resolved_roots)."""
    raw = (repo_path or "").strip()
    if not raw:
        return str(settings.document_root / name)
    if _is_absolute_any(raw):
        return raw
    return str(settings.document_root / raw)


@router.get("")
async def list_projects(request: Request):
    """Stessa route per due consumatori (§ handoff): il browser (Accept: text/html)
    ottiene la pagina Progetti; le fetch di app.js/API ottengono il JSON. In cima
    sempre le due voci implicite pinnate (Documenti + Argo se stesso)."""
    settings = get_settings()
    rows = get_db().query("SELECT * FROM project ORDER BY created_at DESC")
    projects = implicit_projects(settings) + [dict(r) for r in rows]
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(request, "projects.html", {
            "request": request, "projects": projects,
        })
    return projects


@router.post("")
async def create_project(body: NewProject):
    settings = get_settings()
    roots = settings.resolved_roots()
    candidate = _candidate_path(settings, body.name, body.repo_path)
    try:
        resolved = resolve_within_roots(candidate, roots)
    except PathNotAllowed as e:
        raise HTTPException(status_code=422, detail=f"repo_path non consentito: {e}")
    # crea la cartella del progetto (dentro il guard, quindi dentro le root).
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=422, detail=f"impossibile creare {resolved}: {e}")
    pid = new_id("proj")
    get_db().execute(
        "INSERT INTO project(id, name, repo_path, created_at) VALUES(?,?,?,?)",
        (pid, body.name, str(resolved), utcnow()),
    )
    return {"id": pid, "repo_path": str(resolved)}
