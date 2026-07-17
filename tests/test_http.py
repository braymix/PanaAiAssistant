"""M1: health, auth d'identita' (regola 4.2), asset PWA pubblici."""

from fastapi.testclient import TestClient

import app.config as config
from app.main import create_app


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_dashboard_ok_with_dev_identity(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Argo" in r.text


def test_manifest_and_sw_public(client):
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/sw.js").status_code == 200


def test_direct_hit_without_identity_rejected(db, settings):
    """Senza header d'identita' e senza dev-flag: 401 (regola 4.2).

    Simula il colpo diretto sulla LAN che salterebbe Tailscale Serve.
    """
    strict = config.Settings(
        db_path=settings.db_path, repo_roots=settings.repo_roots,
        dev_allow_no_identity=False,
    )
    config.set_settings(strict)
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        # ma gli asset pubblici e health restano raggiungibili
        assert c.get("/healthz").status_code == 200
        assert c.get("/manifest.webmanifest").status_code == 200
    config.set_settings(settings)


def test_project_create_validates_root(client, roots):
    # dentro una root -> ok
    good = roots[0] / "myrepo"
    good.mkdir()
    r = client.post("/projects", json={"name": "A", "repo_path": str(good)})
    assert r.status_code == 200
    assert client.get("/projects").json()[0]["name"] == "A"


def test_project_outside_root_rejected(client, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    r = client.post("/projects", json={"name": "B", "repo_path": str(outside)})
    assert r.status_code == 422


def test_plan_status_endpoint(client, db):
    import json as _json
    from app.db import utcnow
    db.execute(
        "INSERT INTO plan_document(id, conversation_id, status, raw_json, created_at) "
        "VALUES(?,?,?,?,?)",
        ("plan-1", "c1", "executing", _json.dumps({"tasks": []}), utcnow()))
    db.execute(
        "INSERT INTO task(id, plan_id, seq, title, brief_json, status, backend, attempts) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("task-1", "plan-1", 0, "T1", "{}", "running", "ollama", 1))
    r = client.get("/plans/plan-1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_status"] == "executing"
    assert body["tasks"][0]["title"] == "T1" and body["tasks"][0]["status"] == "running"
    assert body["pending_approvals"] == []


def test_plan_status_404(client):
    assert client.get("/plans/nope/status").status_code == 404


def test_new_research_conversation_stores_mode(client, db):
    r = client.post("/chat/new", json={"mode": "research"})
    assert r.status_code == 200 and r.json()["mode"] == "research"
    cid = r.json()["conversation_id"]
    row = db.query_one("SELECT title, mode FROM conversation WHERE id=?", (cid,))
    assert row["mode"] == "research" and row["title"] == "Ricerca online"


def test_stats_reports_euro(client):
    body = client.get("/stats").json()
    assert "cost_today_eur" in body


def test_via_rejects_plan_without_verify(client, monkeypatch):
    """Il PlanDocument e' rifiutato se un task e' privo di verify_cmd (§5.2).

    Mocka il planner per non dipendere dall'SDK/abbonamento: restituisce un piano
    con un task senza verify_cmd -> generate_plan deve sollevare -> HTTP 422.
    """
    import app.planner as planner
    from app.briefs import PlanDocument, validate_plan

    async def fake_generate_plan(conversation_id, repo_path, resume_session=None):
        plan = PlanDocument.from_dict({"repo_path": repo_path, "tasks": [
            {"id": "t1", "title": "x", "instructions": "y",
             "files_allowed": ["a.py"], "verify_cmd": ""}]})
        validate_plan(plan)  # solleva PlanValidationError
        return "unreachable"

    monkeypatch.setattr(planner, "generate_plan", fake_generate_plan)
    r = client.post("/plans/via",
                    json={"conversation_id": "c1", "repo_path": "/r"})
    assert r.status_code == 422
    assert "verify_cmd" in r.json()["detail"]
