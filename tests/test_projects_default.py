"""Default di progetto (§A) + progetto 'se stesso' (Addendum). document_root e
self_root sono tmp dir (fixture), MAI i path reali."""

from __future__ import annotations

import asyncio

import pytest

import app.config as config
from app.planner import _safe_cwd
from app.policy import evaluate
from app.routes.projects import implicit_projects
from app.security import resolve_within_roots, PathNotAllowed


# ------------------------------------------------------------------ config

def test_resolved_roots_includes_document_and_self(settings):
    roots = settings.resolved_roots()
    assert settings.document_root.resolve() in roots
    assert settings.self_root.resolve() in roots
    # document_root in testa (casa di default)
    assert roots[0] == settings.document_root.resolve()


def test_resolved_roots_dedup_and_extra(tmp_path):
    # se document_root e self_root coincidono, niente duplicati; le extra restano.
    doc = tmp_path / "doc"; doc.mkdir()
    extra = tmp_path / "extra"; extra.mkdir()
    s = config.Settings(document_root=doc, self_root=doc, repo_roots=[str(extra)])
    roots = s.resolved_roots()
    assert roots.count(doc.resolve()) == 1
    assert extra.resolve() in roots


def test_document_root_valid_even_without_argo_roots(tmp_path):
    # ARGO_ROOTS vuoto: document_root e' comunque una root valida (niente
    # "tutto negato").
    doc = tmp_path / "doc"; doc.mkdir()
    self_r = tmp_path / "self"; self_r.mkdir()
    s = config.Settings(document_root=doc, self_root=self_r, repo_roots=[])
    inside = doc / "file.md"
    inside.write_text("x", encoding="utf-8")
    assert resolve_within_roots(inside, s.resolved_roots()) == inside.resolve()


# ------------------------------------------------------------------ progetti

def test_create_project_by_name_only(client, settings):
    r = client.post("/projects", json={"name": "foo"})
    assert r.status_code == 200
    # vive sotto document_root/foo, creata e registrata.
    assert r.json()["repo_path"] == str((settings.document_root / "foo").resolve())
    assert (settings.document_root / "foo").is_dir()


def test_create_project_relative_path(client, settings):
    r = client.post("/projects", json={"name": "bar", "repo_path": "sub/bar"})
    assert r.status_code == 200
    assert (settings.document_root / "sub" / "bar").is_dir()


def test_create_project_absolute_outside_rejected(client, tmp_path):
    outside = tmp_path / "evil"
    outside.mkdir()
    r = client.post("/projects", json={"name": "x", "repo_path": str(outside)})
    assert r.status_code == 422


def test_project_inside_self_root_ok(client, settings):
    # un repo_path assoluto dentro self_root supera il guard.
    target = settings.self_root / "sub"
    target.mkdir()
    r = client.post("/projects", json={"name": "self-sub", "repo_path": str(target)})
    assert r.status_code == 200


def test_implicit_projects_pinned(client, settings):
    projs = client.get("/projects").json()
    assert projs[0]["id"] == "proj-documents" and projs[0]["kind"] == "default"
    assert projs[1]["id"] == "proj-self" and projs[1]["kind"] == "self"
    assert projs[0]["pinned"] and projs[1]["pinned"]


def test_implicit_projects_helper(settings):
    imp = implicit_projects(settings)
    assert [p["kind"] for p in imp] == ["default", "self"]
    assert imp[0]["repo_path"] == str(settings.document_root)
    assert imp[1]["repo_path"] == str(settings.self_root)


# ------------------------------------------------------------------ _safe_cwd

def test_safe_cwd_falls_back_to_document_root(settings):
    # path vuoto o fuori dalle root -> ripiega su document_root, non su ".".
    assert _safe_cwd("", settings) == str(settings.document_root.resolve())


def test_safe_cwd_keeps_valid_path(settings):
    proj = settings.document_root / "p"
    assert _safe_cwd(str(proj), settings) == str(proj.resolve())
    assert proj.is_dir()   # creata


# ------------------------------------------------------------------ self-protect guard

def _self_file(settings, rel):
    p = settings.self_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


def test_self_protect_asks_for_security_file(settings):
    f = _self_file(settings, "app/security.py")
    roots = settings.resolved_roots()
    # anche se il file e' nel perimetro (files_allowed), il guard forza "ask".
    verdict, reason = evaluate(
        "Write", {"file_path": str(f)}, {f.resolve()}, roots, [],
        self_root=settings.self_root.resolve(), self_protect=True)
    assert verdict == "ask" and "sensibile" in reason


def test_self_protect_disabled_allows(settings):
    f = _self_file(settings, "app/security.py")
    roots = settings.resolved_roots()
    verdict, _ = evaluate(
        "Write", {"file_path": str(f)}, {f.resolve()}, roots, [],
        self_root=settings.self_root.resolve(), self_protect=False)
    assert verdict == "allow"


def test_self_protect_claude_dir(settings):
    f = _self_file(settings, ".claude/settings.json")
    roots = settings.resolved_roots()
    verdict, _ = evaluate(
        "Write", {"file_path": str(f)}, {f.resolve()}, roots, [],
        self_root=settings.self_root.resolve(), self_protect=True)
    assert verdict == "ask"


def test_self_protect_ignores_non_sensitive_file(settings):
    f = _self_file(settings, "app/routes/ui.py")
    roots = settings.resolved_roots()
    verdict, _ = evaluate(
        "Write", {"file_path": str(f)}, {f.resolve()}, roots, [],
        self_root=settings.self_root.resolve(), self_protect=True)
    assert verdict == "allow"   # nel perimetro e non sensibile -> allow come prima
