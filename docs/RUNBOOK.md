# RUNBOOK — avviare Argo sul PC (Windows)

Procedura a fasi. Puoi fermarti dopo ogni fase e avere qualcosa che funziona.
Comandi in **PowerShell**. Il separatore di path su Windows è `;`.

> Prerequisito di progetto: i tre GATE (`gates/README.md`) andrebbero passati
> prima di fidarti del flusso "pianifico e me ne vado". Ma per *avviare* l'app e
> vederla in faccia bastano le fasi qui sotto.

---

## Fase A — l'app gira in locale (nessuna AI ancora)

Serve solo Python 3.12.

```powershell
cd C:\percorso\Argo
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# root su cui gli agenti potranno operare (OBBLIGATORIA, ';' separati)
$env:ARGO_ROOTS = "C:\src\repoA;C:\src\repoB"
# in locale, senza Tailscale davanti, disattiva l'auth d'identità (SOLO dev)
$env:ARGO_DEV_ALLOW_NO_IDENTITY = "1"

python -m app.main
```

Apri `http://127.0.0.1:8765` nel browser del PC. Devi vedere la dashboard.
`http://127.0.0.1:8765/healthz` deve rispondere `{"status":"ok"}`.

- Se `ARGO_ROOTS` è vuoto, ogni Write/Bash sarà **negato**: è voluto (regola 4.3).
- Crea un "Progetto" dalla dashboard puntando a un repo dentro una root.

A questo punto puoi già chattare col planner **solo dopo la Fase C** (serve la
CLI). La UI, i piani salvati e le stats funzionano da subito.

---

## Fase B — dal telefono, PWA e push

1. Installa Tailscale sul PC e sul telefono, stesso account, ed esegui il login.
2. Esponi l'app (mai port forwarding, §2):
   ```powershell
   tailscale serve --bg 8765
   ```
   Ti dà un URL `https://<nome-pc>.ts.net`.
3. **Togli** il flag di dev e riavvia, così l'auth d'identità di Tailscale è attiva:
   ```powershell
   Remove-Item Env:\ARGO_DEV_ALLOW_NO_IDENTITY
   python -m app.main
   ```
4. Sul telefono apri l'URL `*.ts.net` → **installa in Home**
   (iOS: Condividi → Aggiungi a Home; obbligatorio per la push, §1.10).
5. Chiavi push (una volta):
   ```powershell
   cd gates\gate0_push
   pip install py-vapid pywebpush
   python gen_vapid.py            # crea gates\gate0_push\vapid_keys.json
   ```
   Poi dì all'app dove sono le chiavi (o copiale nella cartella dell'app):
   ```powershell
   $env:ARGO_VAPID_KEYS = "C:\percorso\Argo\gates\gate0_push\vapid_keys.json"
   ```
   Riavvia l'app, apri la PWA dalla Home e premi **Attiva push**.

Da qui approvazioni ed escalation ti arrivano come notifica anche a schermo spento.

---

## Fase C — il planner (abbonamento Claude)

L'SDK lancia la CLI `claude` come motore, quindi:

```powershell
# Node.js necessario, poi la CLI di Claude Code
npm install -g @anthropic-ai/claude-code
claude            # esegui una volta per fare il login con il tuo abbonamento
```

Riavvia l'app. Ora dal telefono: apri una chat su un progetto, discuti il piano
(il planner è in plan mode, non tocca file), poi premi **VIA** per generare il
PlanDocument. Se un task è senza `verify_cmd`, il piano viene rifiutato (§5.2).

---

## Fase D — gli executor (Ollama, gratis)

```powershell
# 1) context alto: la manopola che fa o rompe tutto (§1.9)
setx OLLAMA_CONTEXT_LENGTH 65536     # riapri il terminale dopo questo
ollama serve                          # in un terminale
ollama pull qwen3-coder               # o un modello con tool-use forte adatto alla tua VRAM
```

Configura l'app (se cambi modello/URL):
```powershell
$env:ARGO_OLLAMA_MODEL = "qwen3-coder"
$env:OLLAMA_URL = "http://localhost:11434"
$env:ARGO_MAX_CONCURRENCY = "3"       # sei GPU-bound: alza dopo il GATE 2
```

Premi **Esegui** su un piano approvato: i task girano in locale, l'orchestratore
esegue lui i `verify_cmd`, ritenta, e dopo `ARGO_MAX_RETRIES` fa escalation
all'abbonamento. Segui tutto dal monitor del piano e dalla pagina di ogni run.

---

## Avvio automatico al login (Task Scheduler)

Account utente, non un servizio pre-login (§2):

```powershell
schtasks /Create /TN "Argo" /SC ONLOGON /RL LIMITED `
  /TR "cmd /c cd /d C:\percorso\Argo && .venv\Scripts\python -m app.main"
```

Le variabili `ARGO_*` mettile come variabili d'ambiente utente permanenti
(`setx ARGO_ROOTS "..."`, ecc.) così il task le trova. Non auto-resumare i run
al boot (anti-pattern §8).

---

## Verifica rapida che tutto sia a posto

```powershell
.\.venv\Scripts\Activate.ps1
pytest -q                 # 59 test, tutti verdi
```

## Problemi comuni

- **401 su ogni pagina** dal telefono: stai arrivando via Tailscale? L'auth è
  l'header d'identità; senza Tailscale davanti usa `ARGO_DEV_ALLOW_NO_IDENTITY=1`
  solo in locale.
- **Ogni Write negato**: `ARGO_ROOTS` non è impostata o il repo è fuori (4.3).
- **Niente push**: manca `vapid_keys.json` o la PWA non è installata in Home (iOS).
- **Planner non risponde / errore CLI**: `claude` non installato o non loggato.
- **Executor che loopa**: `OLLAMA_CONTEXT_LENGTH` troppo basso, o modello debole
  di tool-use (§1.9) → è esattamente ciò che il GATE 2 serve a scoprire.
