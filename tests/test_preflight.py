"""Preflight dei backend: forma dei risultati e tolleranza (Ollama spento)."""

import asyncio

from app.preflight import check_backends, check_cli, check_ollama


def test_check_cli_shape():
    r = check_cli()
    assert {"cli_installed", "cli_path", "sdk_importable", "ok"} <= set(r)
    assert isinstance(r["cli_installed"], bool)
    assert isinstance(r["sdk_importable"], bool)
    # 'ok' e' la congiunzione: mai True se la CLI non c'e'.
    assert r["ok"] == (r["cli_installed"] and r["sdk_importable"])


def test_check_ollama_unreachable_is_tolerant(settings):
    # OLLAMA_URL punta a una porta morta: deve tornare non-raggiungibile, non
    # sollevare.
    settings.ollama_url = "http://127.0.0.1:1"
    r = asyncio.run(check_ollama(settings))
    assert r["reachable"] is False
    assert r["ok"] is False
    assert r["primary_installed"] is False
    assert r["models"] == []
    assert "error" in r


def test_check_backends_reports_auto_approve(settings):
    settings.ollama_url = "http://127.0.0.1:1"
    settings.auto_approve = True
    h = asyncio.run(check_backends(settings))
    assert "subscription" in h and "ollama" in h
    assert h["auto_approve"] is True
    assert h["allow_dangerous"] is False
