"""Preflight dei backend: verifica che i DUE canali che Argo usa siano pronti.

Argo esegue tutto con la STESSA Claude Code CLI (via claude-agent-sdk). Cambia
solo `options.env` per-run (§1.2):
  - 'subscription' = Claude Code CLI sull'abbonamento Claude;
  - 'ollama'       = la MEDESIMA CLI con ANTHROPIC_BASE_URL puntato a Ollama
                     locale (backends.ollama_env). Ollama parla Anthropic
                     nativamente: niente proxy (§1.3).

Tutto e' tollerante: non solleva mai, ritorna dict serializzabili per
`/system/health` e per il log di avvio. Sola lettura, non modifica nulla.
"""

from __future__ import annotations

import shutil

import httpx

from .config import Settings


def check_cli() -> dict:
    """La CLI `claude` e' installata e l'SDK importabile? Serve a ENTRAMBI i
    backend: Ollama e' la stessa CLI con un base URL diverso."""
    cli_path = shutil.which("claude")
    try:
        import claude_agent_sdk  # noqa: F401
        sdk = True
    except Exception:  # noqa: BLE001 — ambiente senza SDK: lo segnaliamo, non crasha
        sdk = False
    return {
        "cli_installed": cli_path is not None,
        "cli_path": cli_path,
        "sdk_importable": sdk,
        "ok": cli_path is not None and sdk,
    }


async def check_ollama(settings: Settings) -> dict:
    """Ollama raggiungibile su OLLAMA_URL (GET /api/tags)? il modello primario e'
    installato? Non solleva: Ollama potrebbe essere spento."""
    url = settings.ollama_url.rstrip("/") + "/api/tags"
    primary = settings.ollama_model
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url)
            r.raise_for_status()
            models = [m.get("name", "") for m in (r.json().get("models") or [])]
    except Exception as e:  # noqa: BLE001 — Ollama potrebbe essere spento
        return {
            "reachable": False, "url": settings.ollama_url,
            "error": f"{type(e).__name__}: {e}", "models": [],
            "primary_model": primary, "primary_installed": False, "ok": False,
        }
    # match esatto o con tag ':latest' implicito (es. 'qwen3-coder' vs
    # 'qwen3-coder:latest'). Non facciamo match sul solo nome-base: 7b != 14b.
    installed = primary in models or f"{primary}:latest" in models
    return {
        "reachable": True, "url": settings.ollama_url, "models": models,
        "primary_model": primary, "primary_installed": installed, "ok": True,
    }


async def check_backends(settings: Settings) -> dict:
    """Preflight completo: entrambi i backend + lo stato delle approvazioni."""
    cli = check_cli()
    ollama = await check_ollama(settings)
    return {
        "subscription": {
            **cli,
            "model": settings.subscription_model or "default (SDK)",
        },
        "ollama": ollama,
        "auto_approve": settings.auto_approve,
        "allow_dangerous": settings.allow_dangerous,
    }
