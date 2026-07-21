"""Parte A — pulizia UI.

Verifica che "Cerca online" (modalita' ricerca web) sia stata rimossa del tutto e
che "Chat generica" sia stata rinominata in "Claudio Codice".
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

# file sorgente da scansionare (codice + template + asset), niente __pycache__.
_SRC = [p for p in APP.rglob("*")
        if p.suffix in (".py", ".html", ".js", ".css")
        and "__pycache__" not in p.parts]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def test_no_web_search_feature_left():
    """Grep finale (§A.1): nessun riferimento alla ricerca web nel codice.

    I commenti che documentano la rimozione sono ammessi, quindi si ignorano le
    righe che iniziano con un marcatore di commento."""
    pattern = re.compile(
        r"cerca[ _.]online|search_online|web_search|search_mode|RESEARCH_APPEND",
        re.IGNORECASE)
    offenders: list[str] = []
    for p in _SRC:
        for i, line in enumerate(_read(p).splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("#", "//", "<!--", "*")):
                continue   # commenti "rimosso" ok
            if pattern.search(line):
                offenders.append(f"{p.relative_to(APP.parent)}:{i}: {line.strip()}")
    assert not offenders, "riferimenti alla ricerca web ancora presenti:\n" + \
        "\n".join(offenders)


def test_research_mode_value_gone_from_code():
    """Il valore di modalita' 'research' non compare piu' nel codice/route."""
    for p in _SRC:
        if p.name == "test_cleanup.py":
            continue
        text = _read(p)
        assert "'research'" not in text and '"research"' not in text, \
            f"valore mode 'research' ancora in {p}"


def test_chat_generica_renamed_to_claudio_codice():
    """'Chat generica' non compare piu'; 'Claudio Codice' e' presente."""
    for p in _SRC:
        assert "Chat generica" not in _read(p), f"'Chat generica' ancora in {p}"
    dashboard = _read(APP / "templates" / "dashboard.html")
    chat = _read(APP / "templates" / "chat.html")
    assert "Claudio Codice" in dashboard
    assert "Claudio Codice" in chat


def test_claudio_codice_identifier_used():
    """L'identificatore snake_case 'claudio_codice' e' usato nel backend."""
    chat_route = _read(APP / "routes" / "chat.py")
    assert "claudio_codice" in chat_route


def test_app_boots_after_cleanup(client):
    """L'app parte e la dashboard risponde senza errori dopo le rimozioni."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Claudio Codice" in r.text
    # e la creazione chat usa la nuova modalita'
    body = client.post("/chat/new", json={}).json()
    assert body["mode"] == "claudio_codice"
