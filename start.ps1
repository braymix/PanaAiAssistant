# Argo — avvio rapido per USO LOCALE dal PC (browser su http://127.0.0.1:8765).
# Doppio click NON basta: click destro -> "Esegui con PowerShell",
# oppure nel terminale:  .\start.ps1
#
# Per l'uso dal TELEFONO servi Tailscale davanti (vedi docs/RUNBOOK.md, Fase B):
# in quel caso NON usare questo script, o togli la riga ARGO_DEV_ALLOW_NO_IDENTITY.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- root dei progetti (la cartella genitore dell'app: ...\assistant) ---
$env:ARGO_ROOTS = (Split-Path $PSScriptRoot -Parent)

# --- uso locale dal browser del PC: disattiva l'auth d'identita' Tailscale ---
# (SOLO in locale; con l'app esposta su Tailscale togli questa riga)
$env:ARGO_DEV_ALLOW_NO_IDENTITY = "1"

# --- modello Ollama per gli executor: DEVE stare nella VRAM (RTX 3080 Ti = 12 GB) ---
# qwen3-coder:30b (18 GB) NON ci sta -> lento e va in loop. qwen2.5-coder:14b (9 GB) si'.
$env:ARGO_OLLAMA_MODEL = "qwen2.5-coder:14b"
# se vedi ancora lentezza/loop, abbassa il contesto (meno VRAM per la KV cache):
# $env:OLLAMA_CONTEXT_LENGTH = "32768"

# --- chiavi push, se le hai generate (opzionale) ---
$vapid = Join-Path $PSScriptRoot "gates\gate0_push\vapid_keys.json"
if (Test-Path $vapid) { $env:ARGO_VAPID_KEYS = $vapid }

Write-Host "ARGO_ROOTS = $env:ARGO_ROOTS"
Write-Host "Apri il browser su: http://127.0.0.1:8765" -ForegroundColor Green

& "$PSScriptRoot\.venv\Scripts\python.exe" -m app.main
