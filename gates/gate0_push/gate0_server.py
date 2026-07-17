"""
GATE 0 — server minimo per il test push.

    pip install fastapi uvicorn py-vapid
    python gen_vapid.py           # una volta
    python gate0_server.py        # bind 127.0.0.1:8770

Poi esponilo (§2, mai port forwarding):
    tailscale serve --bg 8770
Sul telefono apri l'URL *.ts.net, installa in Home, premi "Iscrivi a push".
Infine dal PC:  python send_push.py

Serve la PWA statica, espone la chiave pubblica VAPID e salva la subscription
in subscription.json (che send_push.py rilegge). Bind su 127.0.0.1 (regola 4.2):
l'unico ingresso e' Tailscale Serve.
"""

import json
import struct
import zlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

HERE = Path(__file__).parent
STATIC = HERE / "static"
VAPID_KEYS = HERE / "vapid_keys.json"
SUB_FILE = HERE / "subscription.json"


def solid_png(size: int, rgb=(76, 194, 255)) -> bytes:
    """PNG a tinta unita, puro Python (niente Pillow). Basta per un'icona di gate."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    r, g, b = rgb
    row = b"\x00" + bytes((r, g, b)) * size          # filter byte + pixel RGB
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    return png


def ensure_icons():
    for size in (192, 512):
        p = STATIC / f"icon-{size}.png"
        if not p.exists():
            p.write_bytes(solid_png(size))


app = FastAPI()


@app.get("/vapid-public-key")
async def vapid_public_key():
    keys = json.loads(VAPID_KEYS.read_text())
    return {"publicKey": keys["public_b64"]}


@app.post("/subscribe")
async def subscribe(request: Request):
    sub = await request.json()
    SUB_FILE.write_text(json.dumps(sub, indent=2))
    print(f"[subscribe] salvata in {SUB_FILE}")
    return JSONResponse({"ok": True})


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# static per manifest/sw/icone
app.mount("/", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    if not VAPID_KEYS.exists():
        raise SystemExit("Manca vapid_keys.json — esegui prima:  python gen_vapid.py")
    ensure_icons()
    # regola 4.2: bind su loopback, mai 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=8770)
