# Argo — GATE kit

I tre GATE del bootstrap (§6) vanno eseguiti **sul tuo PC/telefono**, non in un
sandbox cloud: dipendono da hardware reale (telefono, Tailscale, GPU/Ollama).
Questo repo contiene le harness; i numeri li produci tu.

Ordine: **GATE 0 → GATE 1 → GATE 2**. Se uno fallisce, fermati e riporta — non
aggirarlo (§0, §9).

## Prerequisiti comuni

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r gates/requirements-gates.txt
```

Claude Code con abbonamento configurato (per GATE 1). Ollama ≥ v0.14.0 con GPU
(per GATE 2).

---

## GATE 0 — la push arriva a schermo spento (rischio #1)

```bash
cd gates/gate0_push
python gen_vapid.py            # una volta: crea vapid_keys.json (NON committare)
python gate0_server.py         # bind 127.0.0.1:8770
# in un altro terminale:
tailscale serve --bg 8770
```

Sul telefono: apri l'URL `*.ts.net` → **installa in Home** (iOS 16.4+
obbligatorio, §1.10) → apri dalla Home → "Iscrivi a push".

Dal PC: `python send_push.py`. Blocca il telefono, in tasca, 60s. Ripeti in 4G.

- ✅ arriva da locked, in 4G → procedi.
- ❌ non arriva → **fermati e riporta**.

## GATE 1 — l'approvazione blocca davvero

```bash
python spike_approval.py       # dalla root del repo
```

Deve bloccarsi finché rispondi (`a`/`d`+invio), mostrare i secondi, negare al
timeout, stampare il `session_id`. Se `approvals == 0` → qualcosa pre-approva
`Write` (§1.8): **fermati e riporta**.

## GATE 2 — Ollama regge il loop agentico

```bash
nvidia-smi                     # controlla la VRAM, scegli il modello
setx OLLAMA_CONTEXT_LENGTH 65536   # riapri il terminale dopo
ollama serve                   # in un terminale
ollama pull qwen3-coder        # in un altro
python gates/gate2_ollama/gate2_ollama_loop.py
```

Riporta i numeri veri: completato? quanti turni? ha loopato? durata. Se loopa su
un task banale, rivedi modello o dettaglio del TaskBrief — non nasconderlo dietro
un retry (§6 GATE 2, §1.9).

---

Solo dopo che **tutti e tre** passano si comincia M1 (§7).
