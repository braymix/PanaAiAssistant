"""Comandi di sistema (§ sistema): pulizia DB, riavvio/spegnimento app, spegni PC.

Gli effetti OS sono FINTI (SystemEffects iniettati): registrano le chiamate senza
uccidere il processo ne' spegnere la macchina di CI.
"""

from __future__ import annotations

import pytest

import app.system as systemmod
from app.system import (
    SystemEffects, build_poweroff_command, build_restart_command, wipe_database,
)


# --------------------------------------------------------------- effetti finti
def _fake_effects():
    calls = {"detached": [], "terminate": 0, "scheduled": 0}

    def run_detached(cmd, cwd):
        calls["detached"].append((cmd, cwd))

    def terminate():
        calls["terminate"] += 1

    def schedule(delay, fn):
        calls["scheduled"] += 1
        fn()  # esegui subito: nei test la grazia non serve

    return SystemEffects(run_detached, terminate, schedule), calls


@pytest.fixture
def fake_effects():
    eff, calls = _fake_effects()
    systemmod.set_effects(eff)
    yield calls
    systemmod.set_effects(None)


# --------------------------------------------------------------- wipe DB (puro)
def test_wipe_keeps_push_by_default(db):
    db.execute("INSERT INTO push_subscription(id, endpoint, p256dh, auth, created_at) "
               "VALUES(?,?,?,?,?)", ("p1", "e", "k", "a", "t"))
    db.execute("INSERT INTO conversation(id, title, plan_mode, created_at) "
               "VALUES(?,?,?,?)", ("c1", "t", 1, "t"))
    removed = wipe_database(db)
    assert removed["conversation"] == 1
    assert db.query_one("SELECT id FROM push_subscription WHERE id=?", ("p1",)) is not None
    assert db.query_one("SELECT id FROM conversation WHERE id=?", ("c1",)) is None


def test_wipe_can_drop_push(db):
    db.execute("INSERT INTO push_subscription(id, endpoint, p256dh, auth, created_at) "
               "VALUES(?,?,?,?,?)", ("p1", "e", "k", "a", "t"))
    wipe_database(db, keep_push=False)
    assert db.query_one("SELECT id FROM push_subscription WHERE id=?", ("p1",)) is None


def test_wipe_keeps_projects_by_default(db):
    db.execute("INSERT INTO project(id, name, repo_path, created_at) VALUES(?,?,?,?)",
               ("pr1", "n", "/r", "t"))
    wipe_database(db)
    assert db.query_one("SELECT id FROM project WHERE id=?", ("pr1",)) is not None
    wipe_database(db, keep_projects=False)
    assert db.query_one("SELECT id FROM project WHERE id=?", ("pr1",)) is None


# --------------------------------------------------- costruzione comandi (pura)
def test_build_restart_command_windows_auto():
    c = build_restart_command("", 2, is_windows=True)
    assert c.startswith("cmd /c") and "app.main" in c and "timeout" in c


def test_build_restart_command_posix_custom():
    c = build_restart_command("mycmd --x", 2, is_windows=False)
    assert c.startswith("sh -c") and "mycmd --x" in c and "sleep" in c


def test_build_poweroff_command():
    assert build_poweroff_command("", is_windows=True) == "shutdown /s /t 0"
    assert build_poweroff_command("", is_windows=False) == "shutdown -h now"
    assert build_poweroff_command("custom cmd") == "custom cmd"


# ------------------------------------------------------------------- HTTP: app
def test_app_restart_requires_confirm(client):
    assert client.post("/system/app/restart", json={}).status_code == 400


def test_app_restart_runs_effects(client, fake_effects):
    r = client.post("/system/app/restart", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "restarting"
    assert fake_effects["terminate"] == 1
    assert len(fake_effects["detached"]) == 1  # rilancio staccato


def test_app_shutdown_runs_effects(client, fake_effects):
    r = client.post("/system/app/shutdown", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "shutting_down"
    assert fake_effects["terminate"] == 1
    assert fake_effects["detached"] == []  # spegni != rilancia


def test_pc_shutdown_runs_poweroff(client, fake_effects):
    r = client.post("/system/pc/shutdown", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "powering_off"
    assert len(fake_effects["detached"]) == 1  # comando shutdown
    assert fake_effects["terminate"] == 0      # il PC si spegne, non il processo


def test_os_actions_disabled_403(client, settings, fake_effects):
    settings.system_controls_enabled = False
    try:
        for path in ("/system/app/restart", "/system/app/shutdown",
                     "/system/pc/shutdown"):
            assert client.post(path, json={"confirm": True}).status_code == 403
        assert fake_effects["scheduled"] == 0  # nessun effetto pianificato
    finally:
        settings.system_controls_enabled = True


# ------------------------------------------------- HTTP: reset (pulizia + riavvio)
def test_reset_requires_confirm(client):
    assert client.post("/system/reset", json={}).status_code == 400


def test_reset_wipes_db(client, db):
    cid = client.post("/chat/new", json={}).json()["conversation_id"]
    db.execute("INSERT INTO message(conversation_id, role, content, ts) "
               "VALUES(?,?,?,?)", (cid, "user", "x", "t"))
    r = client.post("/system/reset", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "reset_done"
    assert db.query_one("SELECT id FROM conversation WHERE id=?", (cid,)) is None
    assert db.query("SELECT id FROM message WHERE conversation_id=?", (cid,)) == []


def test_reset_not_gated_by_os_switch(client, db, settings):
    # il reset DB e' a caldo, non tocca OS/processo: resta disponibile anche con
    # i comandi di sistema OS disattivati.
    settings.system_controls_enabled = False
    try:
        assert client.post("/system/reset", json={"confirm": True}).status_code == 200
    finally:
        settings.system_controls_enabled = True


def test_services_restart(client):
    r = client.post("/system/services/restart", json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "services_restarted"
