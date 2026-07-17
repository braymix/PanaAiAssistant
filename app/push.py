"""Push VAPID verso il telefono. Assumi lo schermo spento (regola 4.16).

pywebpush e' importato pigramente: l'app deve girare anche senza (i test non
dipendono dalla push reale).
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import get_settings
from .db import get_db, utcnow
from .ids import new_id


def save_subscription(sub: dict) -> str:
    keys = sub.get("keys", {})
    sid = new_id("sub")
    get_db().execute(
        "INSERT INTO push_subscription(id, endpoint, p256dh, auth, created_at) "
        "VALUES(?,?,?,?,?)",
        (sid, sub.get("endpoint"), keys.get("p256dh"), keys.get("auth"), utcnow()),
    )
    return sid


def _load_vapid_private() -> str | None:
    p: Path = get_settings().vapid_keys_path
    if not p.exists():
        return None
    return json.loads(p.read_text()).get("private_pem")


def send_push(title: str, body: str, url: str = "/", tag: str = "argo") -> int:
    """Invia a tutte le subscription. Ritorna quante push sono partite ok."""
    private_pem = _load_vapid_private()
    if not private_pem:
        return 0
    try:
        from pywebpush import webpush, WebPushException  # lazy
    except ImportError:
        return 0

    settings = get_settings()
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for row in get_db().query("SELECT * FROM push_subscription"):
        sub_info = {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=private_pem,
                vapid_claims={"sub": settings.vapid_sub},
                ttl=60,
            )
            sent += 1
        except WebPushException:
            # subscription morta: in un sistema reale la si potrebbe rimuovere.
            continue
    return sent
