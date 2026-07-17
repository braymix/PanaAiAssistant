"""Regola 4.3: ogni path dentro le root, no symlink-escape/UNC/'..'."""

import pytest

from app.security import resolve_within_roots, PathNotAllowed


def test_inside_root_ok(roots):
    f = roots[0] / "src" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert resolve_within_roots(f, roots) == f.resolve()


def test_outside_root_denied(roots, tmp_path):
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x")
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(outside, roots)


def test_dotdot_escape_denied(roots):
    sneaky = roots[0] / ".." / "escape.py"
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(sneaky, roots)


def test_symlink_escape_denied(roots, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    link = roots[0] / "link.txt"
    link.symlink_to(secret)   # dentro la root ma punta FUORI
    with pytest.raises(PathNotAllowed):
        resolve_within_roots(link, roots)


def test_no_roots_denies_everything():
    with pytest.raises(PathNotAllowed):
        resolve_within_roots("/anything", [])
