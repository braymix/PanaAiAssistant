"""Parte C — fondamenta "PC Agente" generico: registry + route + ponte inter-agente."""

from __future__ import annotations

import pytest

from app.pc_agents import registry
from app.pc_agents.base import PcAgent


class FakeAgent(PcAgent):
    name = "fake"
    icon = "🧪"
    description = "agente finto per i test"

    def __init__(self):
        self._running = False
        self._logs = ["l1", "l2", "l3"]

    async def check_status(self) -> dict:
        return {"name": self.name, "running": self._running, "installed": True}

    async def start(self) -> bool:
        self._running = True
        return True

    async def stop(self) -> bool:
        self._running = False
        return True

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    async def send_task(self, prompt: str) -> str:
        return "fake-task-1"

    def is_running(self) -> bool:
        return self._running

    def recent_logs(self, n: int = 100) -> list[str]:
        return self._logs[-n:]


@pytest.fixture
def fake_agent():
    agent = FakeAgent()
    registry.register(agent)
    yield agent
    registry._agents.pop("fake", None)   # non inquinare gli altri test


def test_register_and_all_agents(fake_agent):
    names = [a.name for a in registry.all_agents()]
    assert "fake" in names
    assert registry.get("fake") is fake_agent


def test_openclaw_registered_by_default():
    # l'agente OpenClaw si registra all'import del pacchetto (§C.2).
    assert registry.get("openclaw") is not None


def test_agents_list_route(client, fake_agent):
    body = client.get("/agents").json()
    names = [a["name"] for a in body["agents"]]
    assert "fake" in names and "openclaw" in names


def test_agent_status_route(client, fake_agent):
    r = client.get("/agents/fake/status")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "fake" and body["installed"] is True


def test_agent_lifecycle_routes(client, fake_agent):
    assert client.post("/agents/fake/start").json()["ok"] is True
    assert client.get("/agents/fake/status").json()["running"] is True
    assert client.post("/agents/fake/stop").json()["ok"] is True
    assert client.get("/agents/fake/status").json()["running"] is False


def test_agent_task_route(client, fake_agent):
    r = client.post("/agents/fake/task", json={"prompt": "ciao"})
    assert r.status_code == 200 and r.json()["task_id"] == "fake-task-1"


def test_unknown_agent_404(client):
    assert client.get("/agents/nope/status").status_code == 404
    assert client.post("/agents/nope/start").status_code == 404


def test_openclaw_aliases_redirect(client):
    # /openclaw/status e' un alias/redirect verso /agents/openclaw/status (§C.4).
    r = client.get("/openclaw/status")
    assert r.status_code == 200   # TestClient segue il 307


# --- ponte inter-agente (§C.6): stub -> NotImplementedError ------------------
def test_receive_from_is_stub(fake_agent):
    import asyncio
    with pytest.raises(NotImplementedError):
        asyncio.run(fake_agent.receive_from("altro", {"x": 1}))


def test_send_to_calls_receive_from(fake_agent):
    import asyncio
    other = FakeAgent()
    other.name = "other"
    registry.register(other)
    try:
        # receive_from non e' implementato -> send_to propaga NotImplementedError
        with pytest.raises(NotImplementedError):
            asyncio.run(fake_agent.send_to("other", {"hi": 1}))
    finally:
        registry._agents.pop("other", None)


def test_send_to_unknown_agent_raises(fake_agent):
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(fake_agent.send_to("does-not-exist", {}))
