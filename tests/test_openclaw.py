"""Parte B — modulo OpenClaw (setup + processo).

Nessun OpenClaw reale ne' Ollama: si mocka `/api/tags` e si usa un processo finto
(un `python -c sleep`, reale ma innocuo) per verificare start/stop/log/task.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
import yaml

from app import openclaw_setup
from app.openclaw_process import OpenClawProcess


# --- setup ------------------------------------------------------------------
def test_check_status_not_installed(settings, db):
    """OpenClaw non installato: installed=False, nessun crash (§Test)."""
    st = asyncio.run(openclaw_setup.check_status(settings))
    assert st.installed is False
    assert st.process_running is False
    assert st.workspace == settings.openclaw_workspace


def test_generate_config_populates_models(settings, db, monkeypatch):
    """generate_config interroga /api/tags (3 modelli finti) e scrive un
    config.yaml valido: 3 entry, heartbeat off, shell/files/browser abilitati."""
    fake_models = [{"name": "qwen2.5-coder:7b"}, {"name": "llama3:8b"},
                   {"name": "mistral:latest"}]

    async def fake_tags(_s):
        return True, fake_models

    monkeypatch.setattr(openclaw_setup, "_ollama_tags", fake_tags)
    path = asyncio.run(openclaw_setup.generate_config(settings))
    assert path.exists()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))

    models = doc["models"]["providers"]["ollama"]["models"]
    assert len(models) == 3
    assert models[0]["id"] == "ollama/qwen2.5-coder:7b"
    assert models[0]["contextWindow"] == openclaw_setup.DEFAULT_CONTEXT_WINDOW
    assert models[0]["reasoning"] is False
    # primary = primo modello disponibile
    assert doc["agents"]["defaults"]["model"]["primary"] == "ollama/qwen2.5-coder:7b"
    # heartbeat off, accesso totale
    assert doc["agents"]["heartbeat"]["enabled"] is False
    assert doc["tools"]["shell"]["enabled"] is True
    assert doc["tools"]["shell"]["allowedCommands"] == "all"
    assert doc["tools"]["files"]["enabled"] is True
    assert doc["tools"]["files"]["allowedPaths"] == "all"
    assert doc["tools"]["browser"]["enabled"] is True
    # messaging predisposto ma disattivo
    assert doc["integrations"]["whatsapp"]["enabled"] is False


def test_generate_config_updates_only_models_if_exists(settings, db, monkeypatch):
    """Se config.yaml esiste, aggiorna solo la sezione modelli, preservando il
    resto della config utente."""
    async def one_model(_s):
        return True, [{"name": "solo:1b"}]

    monkeypatch.setattr(openclaw_setup, "_ollama_tags", one_model)
    path = asyncio.run(openclaw_setup.generate_config(settings))
    # inietta una chiave utente e un modello finto, poi rigenera
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["custom_user_key"] = {"keep": "me"}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    async def two_models(_s):
        return True, [{"name": "a:1b"}, {"name": "b:2b"}]

    monkeypatch.setattr(openclaw_setup, "_ollama_tags", two_models)
    asyncio.run(openclaw_setup.generate_config(settings))
    doc2 = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc2["custom_user_key"] == {"keep": "me"}     # preservata
    assert len(doc2["models"]["providers"]["ollama"]["models"]) == 2


def test_setup_workspace_creates_structure(settings, db):
    ws = asyncio.run(openclaw_setup.setup_workspace(settings))
    assert ws.exists()
    assert (ws / "logs").exists() and (ws / "sessions").exists()


# --- auto-download (scaricare OpenClaw da solo) -----------------------------
def test_ensure_installed_auto_downloads_when_missing(settings, db, monkeypatch):
    """OpenClaw manca + auto-install ON: ensure_installed SCARICA (chiama
    _npm_install) e ritorna True dopo la ri-verifica."""
    settings.openclaw_auto_install = True
    calls = {"version": 0, "install": 0}

    async def fake_version():
        calls["version"] += 1
        # prima chiamata: non installato; dopo l'install: installato.
        return (calls["install"] > 0), ("openclaw 1.0.0" if calls["install"] else None)

    async def fake_install(_s):
        calls["install"] += 1
        return True, None

    monkeypatch.setattr(openclaw_setup, "_run_version", fake_version)
    monkeypatch.setattr(openclaw_setup, "_npm_install", fake_install)
    ok = asyncio.run(openclaw_setup.ensure_installed(settings))
    assert ok is True
    assert calls["install"] == 1


def test_ensure_installed_no_auto_install_does_not_download(settings, db, monkeypatch):
    """Auto-install OFF: ensure_installed NON scarica, ritorna False."""
    settings.openclaw_auto_install = False
    installed_flag = False

    async def fake_version():
        return installed_flag, None

    async def boom(_s):
        raise AssertionError("_npm_install non deve essere chiamato con auto-install OFF")

    monkeypatch.setattr(openclaw_setup, "_run_version", fake_version)
    monkeypatch.setattr(openclaw_setup, "_npm_install", boom)
    ok = asyncio.run(openclaw_setup.ensure_installed(settings))
    assert ok is False


def test_ensure_installed_already_present_skips_download(settings, db, monkeypatch):
    """Gia' installato: nessun download, ritorna True subito."""
    async def fake_version():
        return True, "openclaw 2.3.4"

    async def boom(_s):
        raise AssertionError("_npm_install non deve girare se gia' installato")

    monkeypatch.setattr(openclaw_setup, "_run_version", fake_version)
    monkeypatch.setattr(openclaw_setup, "_npm_install", boom)
    assert asyncio.run(openclaw_setup.ensure_installed(settings)) is True


def test_npm_install_runs_command_and_streams_log(settings, db, monkeypatch):
    """_npm_install esegue davvero il comando (qui innocuo) e streamma l'output
    come eventi openclaw_log; exit 0 -> successo."""
    from app.events import get_bus

    settings.openclaw_install_cmd = "echo scaricando-openclaw"
    lines: list[str] = []

    bus = get_bus()
    orig_emit = bus.emit

    async def spy_emit(conv, kind, payload):
        if kind == "openclaw_log":
            lines.append(payload.get("line", ""))
        return await orig_emit(conv, kind, payload)

    monkeypatch.setattr(bus, "emit", spy_emit)
    ok, err = asyncio.run(openclaw_setup._npm_install(settings))
    assert ok is True and err is None
    assert any("scaricando-openclaw" in ln for ln in lines)


def test_npm_install_reports_failure_on_nonzero_exit(settings, db):
    """Comando che fallisce -> (False, 'exit code N'), nessuna eccezione."""
    settings.openclaw_install_cmd = "exit 3"
    ok, err = asyncio.run(openclaw_setup._npm_install(settings))
    assert ok is False
    assert err and "3" in err


# --- processo ---------------------------------------------------------------
def _fake_cmd() -> list[str]:
    # processo reale ma innocuo: dorme, cosi' resta "vivo" per il test.
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def test_start_stop_lifecycle_and_events(settings, db):
    async def _go():
        p = OpenClawProcess(settings)
        p.command_override = _fake_cmd()
        assert await p.start() is True
        assert p.is_running() is True
        assert p.pid_file.exists()
        assert await p.start() is True          # idempotente: gia' attivo
        assert await p.stop() is True
        assert p.is_running() is False
        assert not p.pid_file.exists()
        assert await p.stop() is True           # idempotente: gia' fermo

    asyncio.run(_go())
    kinds = [r["kind"] for r in db.query("SELECT kind FROM event ORDER BY id")]
    assert "openclaw_started" in kinds
    assert "openclaw_stopped" in kinds


def test_recent_logs_ring_buffer(settings, db):
    p = OpenClawProcess(settings)
    for i in range(600):
        p._log_lines.append(f"line-{i}")
    logs = p.recent_logs(5)
    assert logs == [f"line-{i}" for i in range(595, 600)]
    # il ring buffer e' limitato (maxlen=500)
    assert len(p.recent_logs(1000)) == 500


def test_send_task_returns_task_id(settings, db, monkeypatch):
    p = OpenClawProcess(settings)

    async def fake_post(_prompt):
        return {"task_id": "oc-123"}

    monkeypatch.setattr(p, "_post_task", fake_post)
    tid = asyncio.run(p.send_task("fai qualcosa"))
    assert tid == "oc-123"
    kinds = [r["kind"] for r in db.query("SELECT kind FROM event ORDER BY id")]
    assert "openclaw_task_sent" in kinds


def test_send_task_generates_id_if_api_silent(settings, db, monkeypatch):
    p = OpenClawProcess(settings)

    async def empty(_prompt):
        return {}

    monkeypatch.setattr(p, "_post_task", empty)
    tid = asyncio.run(p.send_task("x"))
    assert isinstance(tid, str) and tid   # id locale generato
