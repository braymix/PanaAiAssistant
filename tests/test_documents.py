"""Servizio Documenti (§B.7). SICUREZZA prioritaria: nessun byte fuori da
document_root deve mai essere servito. document_root e' una tmp dir (fixture),
MAI il path Windows reale."""

from __future__ import annotations

import sys

import pytest

from app.docs_browser import (
    classify, guess_media_type, list_dir, resolve_doc_path,
)
from app.security import PathNotAllowed


# ----------------------------------------------------------------- logica pura

def test_classify_by_ext():
    assert classify("a.md") == "md"
    assert classify("A.MARKDOWN") == "md"
    assert classify("doc.pdf") == "pdf"
    assert classify("x.PNG") == "image"
    assert classify("y.jpeg") == "image"
    assert classify("z.svg") == "image"
    assert classify("t.txt") == "other"
    assert classify("noext") == "other"


def test_guess_media_type():
    assert guess_media_type("a.pdf") == "application/pdf"
    assert guess_media_type("a.md") == "text/markdown"
    assert guess_media_type("a.png") == "image/png"
    assert guess_media_type("a.webp") == "image/webp"
    assert guess_media_type("a.svg") == "image/svg+xml"
    assert guess_media_type("bin.unknownext") == "application/octet-stream"


def test_list_dir_folders_before_files(settings):
    root = settings.document_root
    (root / "zeta_dir").mkdir()
    (root / "alpha_dir").mkdir()
    (root / "b.md").write_text("x", encoding="utf-8")
    (root / "A.txt").write_text("x", encoding="utf-8")
    entries = list_dir(root, "")
    kinds = [(e.is_dir, e.name) for e in entries]
    # cartelle prima (ordine case-insensitive), poi file
    assert kinds == [
        (True, "alpha_dir"), (True, "zeta_dir"),
        (False, "A.txt"), (False, "b.md"),
    ]
    # rel_path sempre relativo alla root, in POSIX
    assert all(not e.rel_path.startswith("/") for e in entries)


def test_list_dir_stays_inside_root(settings):
    root = settings.document_root
    sub = root / "sub"
    sub.mkdir()
    (sub / "note.md").write_text("ciao", encoding="utf-8")
    entries = list_dir(root, "sub")
    assert [e.name for e in entries] == ["note.md"]
    assert entries[0].rel_path == "sub/note.md"


# ----------------------------------------------------------------- guard di path

def test_resolve_rejects_dotdot(settings):
    with pytest.raises(PathNotAllowed):
        resolve_doc_path(settings.document_root, "../..")


def test_resolve_rejects_absolute_outside(settings, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    with pytest.raises(PathNotAllowed):
        # un path assoluto FUORI dal client: viene trattato come relativo e non
        # deve mai raggiungere l'esterno.
        resolve_doc_path(settings.document_root, str(outside))


def test_resolve_rejects_unc(settings):
    with pytest.raises(PathNotAllowed):
        resolve_doc_path(settings.document_root, r"\\server\share")


@pytest.mark.skipif(sys.platform == "win32", reason="symlink test su POSIX")
def test_resolve_rejects_symlink_escape(settings, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("top secret", encoding="utf-8")
    link = settings.document_root / "escape"
    link.symlink_to(secret)   # dentro document ma punta FUORI
    with pytest.raises(PathNotAllowed):
        resolve_doc_path(settings.document_root, "escape")


# ----------------------------------------------------------------- endpoint HTTP

def test_tree_lists_root(client, settings):
    root = settings.document_root
    (root / "d").mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    r = client.get("/documents/tree?path=")
    assert r.status_code == 200
    body = r.json()
    assert body["cwd"] == "" and body["parent"] is None
    names = [e["name"] for e in body["entries"]]
    assert names == ["d", "a.md"]   # cartella prima


def test_tree_traversal_rejected(client):
    for bad in ["../..", "..", r"\\server\share"]:
        r = client.get("/documents/tree", params={"path": bad})
        assert r.status_code == 400, bad


def test_tree_absolute_outside_rejected(client, tmp_path):
    outside = tmp_path / "nope"
    outside.mkdir()
    r = client.get("/documents/tree", params={"path": str(outside)})
    assert r.status_code == 400


@pytest.mark.skipif(sys.platform == "win32", reason="symlink test su POSIX")
def test_file_symlink_escape_rejected(client, settings, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (settings.document_root / "leak").symlink_to(secret)
    r = client.get("/documents/file", params={"path": "leak", "mode": "download"})
    assert r.status_code == 400
    assert "SECRET" not in r.text


def test_file_pdf_view_and_download(client, settings):
    (settings.document_root / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    v = client.get("/documents/file", params={"path": "doc.pdf", "mode": "view"})
    assert v.status_code == 200
    assert v.headers["content-type"].startswith("application/pdf")
    assert v.headers["content-disposition"].startswith("inline")
    d = client.get("/documents/file", params={"path": "doc.pdf", "mode": "download"})
    assert d.headers["content-disposition"].startswith("attachment")
    assert 'filename="doc.pdf"' in d.headers["content-disposition"]


def test_file_image_inline(client, settings):
    (settings.document_root / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = client.get("/documents/file", params={"path": "p.png", "mode": "view"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["content-disposition"].startswith("inline")


def test_file_other_always_attachment(client, settings):
    (settings.document_root / "data.bin").write_bytes(b"\x00\x01\x02")
    # anche in mode=view un file 'other' e' sempre attachment (mai inline).
    r = client.get("/documents/file", params={"path": "data.bin", "mode": "view"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")


def test_raw_md_returns_text(client, settings):
    (settings.document_root / "n.md").write_text("# Titolo\ncorpo", encoding="utf-8")
    r = client.get("/documents/raw", params={"path": "n.md"})
    assert r.status_code == 200
    assert "# Titolo" in r.text


def test_raw_rejects_non_md(client, settings):
    (settings.document_root / "x.txt").write_text("ciao", encoding="utf-8")
    r = client.get("/documents/raw", params={"path": "x.txt"})
    assert r.status_code == 400


def test_raw_over_cap_denied(client, settings):
    # riduci il cap per il test e supera la soglia -> niente raw (413).
    settings.docs_max_preview_bytes = 10
    (settings.document_root / "big.md").write_text("x" * 50, encoding="utf-8")
    r = client.get("/documents/raw", params={"path": "big.md"})
    assert r.status_code == 413


def test_config_exposes_obsidian(client, settings):
    settings.obsidian_vault = "MyVault"
    settings.obsidian_vault_subpath = "notes"
    body = client.get("/documents/config").json()
    assert body["obsidian_vault"] == "MyVault"
    assert body["vault_subpath"] == "notes"


def test_folder_creates_dir(client, settings):
    r = client.post("/documents/folder", json={"parent_rel": "", "name": "progettoX"})
    assert r.status_code == 200
    assert (settings.document_root / "progettoX").is_dir()
    assert r.json()["rel_path"] == "progettoX"


def test_folder_rejects_traversal_name(client):
    for bad in ["..", "a/b", r"a\b", "."]:
        r = client.post("/documents/folder", json={"parent_rel": "", "name": bad})
        assert r.status_code == 400, bad


def test_documents_page_renders(client):
    r = client.get("/documents")
    assert r.status_code == 200
    assert "Documenti" in r.text
