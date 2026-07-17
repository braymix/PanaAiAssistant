"""ApprovalBroker — l'human-in-the-loop per le ECCEZIONI (§3.2, M2).

Flusso (regole 4.4/4.6/4.16):
  1. persiste l'evento `approval_requested` PRIMA di rispondere all'SDK;
  2. crea la riga `approval` (pending) e manda la push (schermo spento);
  3. blocca finche' arriva la decisione o scade il timeout;
  4. il timeout NEGA (regola 4.6), mai il contrario.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .config import get_settings
from .db import get_db, utcnow
from .events import get_bus
from .ids import new_id
from .push import send_push


@dataclass
class Decision:
    status: str                      # allowed | denied | timeout | interrupted
    reason: str = ""
    updated_input: dict | None = None


class ApprovalBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Decision]] = {}

    async def request(self, run_id: str, tool_name: str, tool_input: dict,
                      title: str = "") -> Decision:
        """Chiede al telefono e blocca. Ritorna la Decision (deny al timeout)."""
        settings = get_settings()
        bus = get_bus()

        # 1. evento append-only PRIMA della risposta all'SDK (regola 4.4)
        event_id = await bus.emit(run_id, "approval_requested", {
            "tool_name": tool_name,
            "tool_input": tool_input,        # §4.7: grezzo, mai riassunto
            "title": title,
        })

        approval_id = new_id("apr")
        get_db().execute(
            "INSERT INTO approval(id, run_id, event_id, tool_name, tool_input, "
            "status, pushed_at) VALUES(?,?,?,?,?,?,?)",
            (approval_id, run_id, event_id, tool_name, json.dumps(tool_input),
             "pending", utcnow()),
        )

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._pending[approval_id] = fut

        # 2. push (regola 4.16: se non arriva, non e' successo)
        preview = _preview(tool_name, tool_input)
        send_push(
            title=f"Argo · approva {tool_name}",
            body=preview,
            url=f"/approvals/{approval_id}",
            tag=f"approval-{approval_id}",
        )

        # 3/4. blocca con timeout -> deny
        try:
            decision = await asyncio.wait_for(fut, timeout=settings.approval_timeout_s)
        except asyncio.TimeoutError:
            decision = Decision(status="timeout",
                                reason="Approvazione scaduta; negata (regola 4.6).")
        finally:
            self._pending.pop(approval_id, None)

        self._persist_decision(approval_id, run_id, decision)
        return decision

    def resolve(self, approval_id: str, allow: bool, reason: str = "",
                updated_input: dict | None = None) -> bool:
        """Chiamata dalla route POST decisione. True se c'era un'attesa viva."""
        fut = self._pending.get(approval_id)
        if fut is None or fut.done():
            return False
        status = "allowed" if allow else "denied"
        fut.set_result(Decision(status=status, reason=reason,
                                 updated_input=updated_input))
        return True

    def _persist_decision(self, approval_id: str, run_id: str,
                          decision: Decision) -> None:
        get_db().execute(
            "UPDATE approval SET status=?, decided_at=?, reason=?, updated_input=? "
            "WHERE id=?",
            (decision.status, utcnow(), decision.reason,
             json.dumps(decision.updated_input) if decision.updated_input else None,
             approval_id),
        )
        asyncio.ensure_future(get_bus().emit(run_id, "approval_resolved", {
            "approval_id": approval_id,
            "status": decision.status,
            "reason": decision.reason,
        }))


def _preview(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Write", "Edit"):
        return f"{tool_name} {tool_input.get('file_path', '?')}"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"Bash: {cmd[:80]}"
    return f"{tool_name} {json.dumps(tool_input)[:80]}"


_broker: ApprovalBroker | None = None


def get_broker() -> ApprovalBroker:
    global _broker
    if _broker is None:
        _broker = ApprovalBroker()
    return _broker
