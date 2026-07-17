# Argo

Control plane personale per agenti di coding. Gira su un PC Windows e **si pilota
interamente dal telefono** via Tailscale.

> **Il modello costoso pensa, i modelli locali sgobbano.** Il planner (abbonamento,
> plan mode) produce piani dettagliati; gli executor (Claude Code → Ollama) li
> macinano gratis. **Il telefono è l'unica interfaccia; il PC è solo il motore.**

## Prima di tutto: i GATE

Non si supera M1 senza aver eseguito, **sul tuo hardware**, i tre gate in
[`gates/`](gates/README.md): push a schermo spento (0), approvazione bloccante
(1), loop Ollama (2). Non sono eseguibili in un sandbox cloud.

## Avvio (dev)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# root su cui gli agenti possono operare (regola 4.3): allowlist obbligatoria
export ARGO_ROOTS="C:\src\repoA;C:\src\repoB"     # os.pathsep separato
export ARGO_DEV_ALLOW_NO_IDENTITY=1               # SOLO in locale, senza Tailscale davanti
python -m app.main                                # http://127.0.0.1:8765
```

In produzione **non** si usa `ARGO_DEV_ALLOW_NO_IDENTITY`: l'auth è l'header
d'identità iniettato da Tailscale Serve (regola 4.2), e il bind resta `127.0.0.1`.

```bash
tailscale serve --bg 8765        # espone su https://<host>.ts.net
```

Push: `cd gates/gate0_push && python gen_vapid.py`, copia `vapid_keys.json` nella
root del progetto (o punta `ARGO_VAPID_KEYS`), poi dalla PWA premi "Attiva push".

## Avvio automatico — Task Scheduler, trigger "at logon"

Account utente, **non** LOCAL SYSTEM, **non** un servizio pre-login (§2):

```
schtasks /Create /TN "Argo" /SC ONLOGON /RL LIMITED ^
  /TR "cmd /c cd /d C:\path\Argo && .venv\Scripts\python -m app.main"
```

Non si auto-resumano i run al boot (anti-pattern §8): il PC si riavvia per
giocare, non per svegliare un agente.

## Struttura

```
app/
  config.py      settings + allowlist root/bash, timeout, concorrenza
  db.py          SQLite WAL, schema §5.1, event append-only
  security.py    path allowlist (4.3) + middleware d'identità (4.2)
  events.py      event bus + SSE con replay Last-Event-ID (4.15)
  approvals.py   ApprovalBroker: push, blocco, timeout→deny (M2)
  policy.py      PolicyGate §3.2 (dentro perimetro auto, fuori push)
  briefs.py      TaskBrief/PlanDocument + validate_plan (§5.2)
  backends.py    ClaudeAgentOptions per Ollama/abbonamento (§1.2/1.8)
  planner.py     chat plan mode + tasto VIA → PlanDocument (M3)
  executor.py    pool, depends_on, verify_cmd, retry, escalation (M4)
  stats.py       snapshot live (M5)
  routes/        health, chat, plans, runs(SSE), approvals, push, stats, ui
  templates/ static/   PWA mobile-first, dark, standalone
tests/           28 test: path, schema, replay, policy, plan, M2-shadow, http
gates/           harness GATE 0/1/2 (da eseguire sul PC/telefono)
```

## Test

```bash
pytest -q
```

Include il **test obbligatorio M2** (§7): `Write` fuori da `allowed_tools` produce
≥1 approvazione pending (§1.8).

## Cosa NON è verificato in cloud

Il planner, gli executor e la push reali richiedono abbonamento Claude Code,
Ollama+GPU e un telefono: quelle parti sono scritte contro l'SDK 0.2.120 ma la
verifica end-to-end è tua, sul PC (i GATE servono a questo).
