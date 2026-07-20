"""Rinomina del titolo di una chat (PATCH /chat/{id})."""

from __future__ import annotations


def test_rename_chat(client, db):
    cid = client.post("/chat/new", json={}).json()["conversation_id"]
    r = client.patch(f"/chat/{cid}", json={"title": "Nuovo titolo"})
    assert r.status_code == 200 and r.json()["title"] == "Nuovo titolo"
    row = db.query_one("SELECT title FROM conversation WHERE id=?", (cid,))
    assert row["title"] == "Nuovo titolo"


def test_rename_chat_trims_and_caps(client, db):
    cid = client.post("/chat/new", json={}).json()["conversation_id"]
    long = "  " + "x" * 200 + "  "
    r = client.patch(f"/chat/{cid}", json={"title": long})
    assert r.status_code == 200
    row = db.query_one("SELECT title FROM conversation WHERE id=?", (cid,))
    assert row["title"] == "x" * 120  # trim + cap a 120


def test_rename_chat_empty_422(client, db):
    cid = client.post("/chat/new", json={}).json()["conversation_id"]
    assert client.patch(f"/chat/{cid}", json={"title": "   "}).status_code == 422


def test_rename_chat_404(client):
    assert client.patch("/chat/nope", json={"title": "x"}).status_code == 404
