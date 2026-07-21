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


def test_exec_argv_wraps_windows_cmd_shim(monkeypatch):
    """Su Windows lo shim npm openclaw.cmd va lanciato via `cmd /c` (subprocess non
    lo risolve da solo -> WinError 2)."""
    monkeypatch.setattr(openclaw_setup.shutil, "which",
                        lambda n: r"C:\Users\x\AppData\Roaming\npm\openclaw.cmd")
    argv = openclaw_setup.exec_argv("openclaw", ["gateway", "--port", "8766"],
                                    is_windows=True)
    assert argv[:3] == ["cmd", "/c", r"C:\Users\x\AppData\Roaming\npm\openclaw.cmd"]
    assert argv[-3:] == ["gateway", "--port", "8766"]


def test_exec_argv_plain_executable_posix(monkeypatch):
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda n: "/usr/bin/openclaw")
    assert openclaw_setup.exec_argv("openclaw", ["gateway"], is_windows=False) == \
        ["/usr/bin/openclaw", "gateway"]


def test_exec_argv_missing_returns_none(monkeypatch):
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda n: None)
    # posix + which None -> None (nessun fallback npm su non-Windows)
    assert openclaw_setup.exec_argv("openclaw", ["x"], is_windows=False) is None


def test_exec_argv_falls_back_to_npm_shim(monkeypatch, tmp_path):
    """PATH stale su Windows: which() None ma lo shim c'e' in %APPDATA%\\npm."""
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "openclaw.cmd").write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(openclaw_setup.shutil, "which", lambda n: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    argv = openclaw_setup.exec_argv("openclaw", ["gateway"], is_windows=True)
    assert argv[:2] == ["cmd", "/c"]
    assert argv[2].endswith("openclaw.cmd")
    assert argv[-1] == "gateway"


def test_setup_workspace_creates_structure(settings, db):
    ws = asyncio.run(openclaw_setup.setup_workspace(settings))
    assert ws.exists()
    assert (ws / "logs").exists() and (ws / "sessions").exists()


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
