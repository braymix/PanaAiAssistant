"""Servizio Documenti (§B.3): navigatore in sola lettura della cartella `document`.

⚠️ SICUREZZA (§B.1): un file-browser = potenziale lettura file arbitraria. Ogni
path dal client e' RELATIVO a document_root e risolto col guard §4.3, confinato
alla SOLA document_root (non a tutto resolved_roots). Traversal/'..'/UNC/path
assoluti/symlink-escape -> PathNotAllowed -> 4xx, nessun byte servito. Nessun
endpoint di scrittura/eliminazione file: solo lettura + creazione cartella (che
passa comunque dal guard). Gli endpoint restano dietro IdentityMiddleware (§4.2).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..config import get_settings
from ..docs_browser import (
    Entry, classify, guess_media_type, list_dir, resolve_doc_path,
)
from ..security import PathNotAllowed

router = APIRouter(prefix="/documents")


def _document_root() -> Path:
    return get_settings().document_root.resolve()


def _guard(rel: str) -> Path:
    """Risolve rel dentro document_root o solleva HTTP 400 (nessun byte servito)."""
    try:
        return resolve_doc_path(_document_root(), rel)
    except PathNotAllowed as e:
        raise HTTPException(status_code=400, detail=f"path non consentito: {e}")


@router.get("/config")
async def documents_config():
    """Espone i dati per comporre il deep link Obsidian lato UI (§B.4)."""
    s = get_settings()
    return {
        "obsidian_vault": s.obsidian_vault,
        "vault_subpath": s.obsidian_vault_subpath,
        "max_preview_bytes": s.docs_max_preview_bytes,
    }


@router.get("/tree")
async def documents_tree(path: str = Query("")):
    """JSON {cwd, parent, entries}. path="" = radice (document_root)."""
    root = _document_root()
    target = _guard(path)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="il path non e' una cartella")
    rel = target.relative_to(root).as_posix()
    cwd = "" if rel == "." else rel
    # parent: None se siamo alla radice, altrimenti il rel del genitore.
    parent = None
    if cwd:
        parent = target.parent.relative_to(root).as_posix()
        parent = "" if parent == "." else parent
    entries = [e.to_dict() for e in list_dir(root, cwd)]
    return {"cwd": cwd, "parent": parent, "entries": entries}


@router.get("/raw", response_class=PlainTextResponse)
async def documents_raw(path: str = Query(...)):
    """Testo di un .md per l'anteprima inline, con cap dimensione (§B.1.4)."""
    target = _guard(path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="il path non e' un file")
    if classify(target) != "md":
        raise HTTPException(status_code=400, detail="anteprima raw solo per file .md")
    cap = get_settings().docs_max_preview_bytes
    size = target.stat().st_size
    if size > cap:
        # oltre il cap: niente raw, solo download (§B.1.4).
        raise HTTPException(
            status_code=413,
            detail=f"file troppo grande per l'anteprima ({size} > {cap} byte): scaricalo.")
    return PlainTextResponse(
        target.read_text(encoding="utf-8", errors="replace"),
        media_type="text/markdown; charset=utf-8")


@router.get("/file")
async def documents_file(path: str = Query(...), mode: str = Query("view")):
    """FileResponse per pdf/image/other (e md se download esplicito).

    - pdf: application/pdf, inline (view) o attachment (download);
    - image: media type rilevato, inline;
    - other: sempre attachment (download);
    - md: di norma non passa di qui, ma se download -> attachment.
    """
    target = _guard(path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="il path non e' un file")
    kind = classify(target)
    filename = target.name
    download = (mode == "download")

    if kind == "pdf":
        media_type = "application/pdf"
        disposition = "attachment" if download else "inline"
    elif kind == "image":
        media_type = guess_media_type(target)
        disposition = "attachment" if download else "inline"
    elif kind == "md":
        # md passa di qui solo per il download esplicito.
        media_type = guess_media_type(target)
        disposition = "attachment"
    else:
        # other: sempre download, mai inline (niente rendering di file arbitrari).
        media_type = guess_media_type(target)
        disposition = "attachment"

    # header Content-Disposition esplicito (nome file tra virgolette).
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return FileResponse(target, media_type=media_type, headers=headers)


class NewFolder(BaseModel):
    parent_rel: str = ""
    name: str


@router.post("/folder")
async def documents_folder(body: NewFolder):
    """Crea document_root/parent_rel/name (guard + mkdir). Utile per creare al
    volo una cartella/progetto dal telefono. Nessun '..' costruibile: il nome e'
    un singolo componente e il tutto ripassa dal guard."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="nome cartella mancante")
    # il nome deve essere un SINGOLO componente: niente separatori ne' '..'.
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="nome cartella non valido")
    # risolvi il genitore col guard, poi il target (che ripassa dal guard).
    parent = _guard(body.parent_rel)
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="parent non e' una cartella")
    root = _document_root()
    parent_rel = parent.relative_to(root).as_posix()
    parent_rel = "" if parent_rel == "." else parent_rel
    child_rel = f"{parent_rel}/{name}" if parent_rel else name
    target = _guard(child_rel)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"impossibile creare: {e}")
    return {"rel_path": target.relative_to(root).as_posix()}
