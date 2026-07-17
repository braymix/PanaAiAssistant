"""PolicyGate §3.2: dentro il perimetro auto-allow, fuori chiedi, path fuori root nega."""

from app.policy import evaluate, cmd_matches_allowlist


def test_write_inside_perimeter_auto_allow(roots):
    f = (roots[0] / "src" / "a.py")
    f.parent.mkdir(parents=True)
    f.write_text("x")
    verdict, _ = evaluate("Write", {"file_path": str(f)}, {f.resolve()},
                          roots, [])
    assert verdict == "allow"


def test_write_outside_perimeter_asks(roots):
    inside_root = (roots[0] / "src" / "b.py")
    inside_root.parent.mkdir(parents=True)
    inside_root.write_text("x")
    # dentro la root ma NON nel perimetro del piano -> eccezione -> ask
    verdict, _ = evaluate("Write", {"file_path": str(inside_root)}, set(),
                          roots, [])
    assert verdict == "ask"


def test_write_outside_root_denied(roots, tmp_path):
    outside = tmp_path / "evil.py"
    verdict, _ = evaluate("Write", {"file_path": str(outside)}, set(), roots, [])
    assert verdict == "deny"


def test_bash_allowlist_allows_and_asks():
    allow = ["pytest", "git status"]
    assert cmd_matches_allowlist("pytest tests/x.py -q", allow)
    assert not cmd_matches_allowlist("rm -rf /", allow)
    v_ok, _ = evaluate("Bash", {"command": "pytest -q"}, set(), [], allow)
    v_no, _ = evaluate("Bash", {"command": "rm -rf /"}, set(), [], allow)
    assert v_ok == "allow" and v_no == "ask"


def test_unknown_tool_asks(roots):
    verdict, _ = evaluate("WebFetch", {"url": "http://x"}, set(), roots, [])
    assert verdict == "ask"
