"""
GATE 0 — genera la coppia di chiavi VAPID una volta sola.

    pip install py-vapid
    python gen_vapid.py

Scrive vapid_keys.json con:
  - private_pem : la chiave privata (per pywebpush lato server)
  - public_b64  : la application server key (base64url, per il browser)
NON committare vapid_keys.json.
"""

import json
from pathlib import Path

from py_vapid import Vapid02
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

OUT = Path(__file__).parent / "vapid_keys.json"


def uncompressed_public_b64url(vapid: Vapid02) -> str:
    import base64
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def main():
    if OUT.exists():
        print(f"{OUT} esiste gia'. Cancellalo se vuoi rigenerare.")
        return
    vapid = Vapid02()
    vapid.generate_keys()
    private_pem = vapid.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    OUT.write_text(json.dumps({
        "private_pem": private_pem,
        "public_b64": uncompressed_public_b64url(vapid),
    }, indent=2))
    print(f"Scritte chiavi in {OUT}")


if __name__ == "__main__":
    main()
