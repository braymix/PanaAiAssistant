"""
GATE 0 — manda la push di prova al telefono.

    pip install pywebpush
    python send_push.py

Legge vapid_keys.json + subscription.json (creati dal server dopo l'iscrizione)
e invia una notifica. Poi: blocca il telefono, mettilo in tasca, aspetta 60s.
Ripeti fuori casa in 4G con Tailscale attivo.

✅ arriva a schermo spento, da locked, in 4G -> GATE 0 passa.
❌ non arriva -> fermati e riporta (§9, GATE 0): cambia il progetto, non il codice.
"""

import json
import time
from pathlib import Path

from pywebpush import webpush, WebPushException

HERE = Path(__file__).parent
VAPID_KEYS = HERE / "vapid_keys.json"
SUB_FILE = HERE / "subscription.json"

# email di contatto VAPID (claim "sub"). Cambiala con la tua.
VAPID_SUB = "mailto:michelepanarotto00@gmail.com"


def main():
    keys = json.loads(VAPID_KEYS.read_text())
    sub = json.loads(SUB_FILE.read_text())

    payload = json.dumps({
        "title": "Argo · GATE 0",
        "body": f"Push ricevuta a schermo spento? — {time.strftime('%H:%M:%S')}",
        "tag": "argo-gate0",
        "url": "/",
    })

    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=keys["private_pem"],
            vapid_claims={"sub": VAPID_SUB},
            ttl=60,
        )
        print("✅ push inviata. Guarda il telefono (bloccato).")
    except WebPushException as e:
        print(f"❌ invio fallito: {e}")
        if e.response is not None:
            print(f"   status={e.response.status_code} body={e.response.text}")


if __name__ == "__main__":
    main()
