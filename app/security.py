"""Regole non negoziabili §4: path allowlist e auth d'identita'.

Nessun `bypassPermissions` esiste in questo sistema (regola 4.1).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings


class PathNotAllowed(ValueError):
    pass


def resolve_within_roots(candidate: str | Path, roots: list[Path]) -> Path:
    """Risolve `candidate` e verifica che cada dentro una root consentita.

    Rifiuta symlink-escape, path UNC e '..' perche' usa resolve() (che segue i
    symlink e normalizza) e poi confronta l'ancestor REALE. Vale per repo_path e
    per ogni files_allowed (regola 4.3). Da chiamare alla CREAZIONE e allo START.
    """
    if not roots:
        raise PathNotAllowed(
            "Nessuna root configurata (ARGO_ROOTS): tutto e' negato per default."
        )
    p = Path(candidate)
    # UNC su Windows: \\server\share — rifiutato esplicitamente.
    if str(p).startswith("\\\\") or str(p).startswith("//"):
        raise PathNotAllowed(f"Path UNC non consentito: {candidate}")
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as e:
        raise PathNotAllowed(f"Impossibile risolvere {candidate}: {e}")

    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PathNotAllowed(
        f"{resolved} e' fuori dalle root consentite {[str(r) for r in roots]}"
    )


def validate_paths(paths: list[str], roots: list[Path]) -> list[Path]:
    return [resolve_within_roots(p, roots) for p in paths]


# --- auth di trasporto (regola 4.2) --------------------------------------------
# Il bind e' 127.0.0.1; l'unico modo di arrivarci con l'header d'identita' e'
# passare da Tailscale Serve. Se l'header manca -> 401 (a meno del flag dev).
_PUBLIC_PREFIXES = ("/static", "/manifest.webmanifest", "/sw.js", "/healthz")


class IdentityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        path = request.url.path
        if any(path == p or path.startswith(p + "/") or path.startswith(p)
               for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        identity = request.headers.get(settings.identity_header)
        if not identity and not settings.dev_allow_no_identity:
            return JSONResponse(
                {"error": "unauthenticated",
                 "detail": "Accesso solo via Tailscale Serve (header d'identita' assente)."},
                status_code=401,
            )
        # rende l'identita' disponibile alle route
        request.state.user = identity or "dev-local"
        return await call_next(request)
