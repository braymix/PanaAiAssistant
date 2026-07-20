"""Logica del servizio Documenti (§B.2): classificazione, listing, media type.

Quasi puro e testabile a parte: NON importa l'SDK, non esegue comandi shell, usa
solo `pathlib` e `mimetypes` (cross-platform). Ogni path che arriva dal client e'
relativo a `document_root` e va risolto col guard §4.3 (resolve_within_roots)
confinato alla SOLA document_root: il browser non deve mai servire byte fuori.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .security import PathNotAllowed, resolve_within_roots

# estensione (minuscola) -> kind logico
KIND_BY_EXT = {
    ".md": "md", ".markdown": "md",
    ".pdf": "pdf",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image",
}


def classify(path: str | Path) -> str:
    """Ritorna "md" | "pdf" | "image" | "other" in base all'estensione."""
    return KIND_BY_EXT.get(Path(path).suffix.lower(), "other")


@dataclass
class Entry:
    name: str
    rel_path: str        # sempre relativo a document_root, con '/' (POSIX)
    is_dir: bool
    size: int            # byte (0 per le cartelle)
    mtime: float         # epoch secondi
    kind: str            # "dir" per le cartelle; kind del file altrimenti

    def to_dict(self) -> dict:
        return {
            "name": self.name, "rel_path": self.rel_path, "is_dir": self.is_dir,
            "size": self.size, "mtime": self.mtime, "kind": self.kind,
        }


def _rel_to_root(child: Path, root: Path) -> str:
    """Path di `child` relativo a `root`, in forma POSIX ('' per la radice)."""
    rel = child.relative_to(root)
    s = rel.as_posix()
    return "" if s == "." else s


def resolve_doc_path(document_root: Path, rel: str) -> Path:
    """Risolve document_root/rel col guard §4.3, confinato alla SOLA document_root.

    Solleva PathNotAllowed su traversal ('..'), path assoluti dal client, UNC,
    symlink-escape. `rel` vuoto = la radice (document_root)."""
    root = document_root.resolve()
    rel = (rel or "").strip()
    if not rel:
        return resolve_within_roots(root, [root])
    # UNC (\\server\share o //host/share) dal client: rifiutato subito.
    if rel.startswith("\\\\") or rel.startswith("//"):
        raise PathNotAllowed(f"Path UNC non consentito: {rel}")
    # path ASSOLUTO dal client (semantica Windows O Posix): rifiutato. Il client
    # deve mandare SOLO percorsi relativi a document_root; l'assoluto vincerebbe
    # il join di pathlib e potrebbe puntare ovunque.
    if PureWindowsPath(rel).is_absolute() or PurePosixPath(rel).is_absolute():
        raise PathNotAllowed(f"Path assoluto non consentito dal client: {rel}")
    # resolve_within_roots gestisce '..'/symlink e confronta l'ancestor reale.
    return resolve_within_roots(root / rel, [root])


def list_dir(document_root: Path, rel: str) -> list[Entry]:
    """Elenca le voci di document_root/rel: cartelle prima, poi file, ordine
    case-insensitive. Risolve col guard; NON attraversa fuori da document_root."""
    root = document_root.resolve()
    target = resolve_doc_path(root, rel)
    entries: list[Entry] = []
    for child in target.iterdir():
        try:
            st = child.stat()
        except OSError:
            continue
        is_dir = child.is_dir()
        entries.append(Entry(
            name=child.name,
            rel_path=_rel_to_root(child, root),
            is_dir=is_dir,
            size=0 if is_dir else st.st_size,
            mtime=st.st_mtime,
            kind="dir" if is_dir else classify(child),
        ))
    # cartelle prima dei file, poi ordine alfabetico case-insensitive.
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def guess_media_type(path: str | Path) -> str:
    """MIME type via `mimetypes`, con fallback ragionevoli e application/octet-stream."""
    p = Path(path)
    ext = p.suffix.lower()
    # mimetypes su alcune piattaforme non conosce .md/.webp/.svg: fallback espliciti.
    fallback = {
        ".md": "text/markdown", ".markdown": "text/markdown",
        ".webp": "image/webp", ".svg": "image/svg+xml",
    }
    if ext in fallback:
        return fallback[ext]
    mt, _ = mimetypes.guess_type(str(p))
    return mt or "application/octet-stream"
