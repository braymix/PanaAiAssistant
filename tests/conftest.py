"""Fixture: db temporaneo, settings isolate, reset dei singleton globali."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.config as config
import app.db as dbmod
import app.events as eventsmod
import app.approvals as approvalsmod
import app.executor as executormod


@pytest.fixture
def roots(tmp_path) -> list[Path]:
    root = tmp_path / "repos"
    root.mkdir()
    return [root]


@pytest.fixture
def settings(tmp_path, roots) -> config.Settings:
    # document_root/self_root a tmp dir: MAI il path Windows reale (§Test).
    document_root = tmp_path / "document"
    document_root.mkdir()
    self_root = tmp_path / "self"
    self_root.mkdir()
    s = config.Settings(
        db_path=tmp_path / "argo.db",
        repo_roots=[str(roots[0])],
        document_root=document_root,
        self_root=self_root,
        approval_timeout_s=2,
        vapid_keys_path=tmp_path / "nope.json",   # niente push nei test
        dev_allow_no_identity=True,
        # la fixture modella la modalita' GATED (human-in-the-loop): i test del
        # flusso approvazioni contano sul verdetto 'ask'. L'auto-approvazione,
        # attiva di default a runtime, e' testata a parte passando il flag.
        auto_approve=False,
        # workspace OpenClaw in tmp: MAI il path Windows reale (§Test).
        openclaw_workspace=tmp_path / "openclaw",
    )
    config.set_settings(s)
    return s


@pytest.fixture
def db(settings):
    database = dbmod.Database(settings.db_path)
    dbmod.set_db(database)
    # reset dei singleton che dipendono dal db/bus
    eventsmod._bus = None
    approvalsmod._broker = None
    executormod._pool = None
    yield database
    database.close()


@pytest.fixture
def client(db, settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app()
    # bypassa il lifespan (db gia' inizializzato dalla fixture)
    with TestClient(app) as c:
        yield c
