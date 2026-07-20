"""PolicyGate §3.2 — applica il PIANO, non chiama il telefono per ogni Write.

L'umano approva UNA volta (il tasto VIA). Dentro il perimetro `files_allowed` e
la bash-allowlist -> auto-allow. Fuori -> UNA push (eccezione).

`evaluate()` e' logica pura (niente SDK): testabile a parte. La factory
`make_policy_gate()` costruisce il callback `can_use_tool` che l'SDK invoca.
Auto-allow != no-gate: il gate gira SEMPRE (anti-pattern §8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .approvals import get_broker
from .config import get_settings
from .events import get_bus
from .security import resolve_within_roots, PathNotAllowed


@dataclass
class GateContext:
    run_id: str
    files_allowed_resolved: set[Path]
    roots: list[Path]
    push_counter: dict = field(default_factory=dict)  # {"pushes": int}
    # buffer dei verdetti non-allow del run corrente (deny/ask). L'autofix lo
    # legge per diagnosticare un PERIMETER_BLOCK (Write fuori files_allowed)
    # senza dover interrogare il bus/DB (missione autofix).
    policy_events: list = field(default_factory=list)


def cmd_matches_allowlist(command: str, allowlist: list[str]) -> bool:
    c = command.strip()
    return any(c == pfx or c.startswith(pfx + " ") for pfx in allowlist)


# Strato deterministico (§8): questi non si auto-permettono e non si girano
# ciecamente all'umano — si negano. E' il "buon secondo strato" oltre al gate umano.
_DANGEROUS = [
    re.compile(r"\brm\s+(-\w*\s+)*-\w*[rf]", re.I),   # rm -rf / rm -fr ...
    re.compile(r":\(\)\s*\{.*\|.*&\s*\}\s*;", re.I),  # fork bomb
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\b.*\bof=/dev/", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
    re.compile(r"\bgit\s+push\b.*--force(?!-with-lease)", re.I),
    re.compile(r"\bgit\s+push\b.*\s-f(\s|$)", re.I),
    re.compile(r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash)\b", re.I),  # curl|sh
    re.compile(r"\bchmod\s+-R\s+777\b", re.I),
    re.compile(r"\bshutdown\b|\breboot\b", re.I),
]


def is_dangerous_bash(command: str) -> bool:
    return any(p.search(command) for p in _DANGEROUS)


# File di sicurezza del progetto "se stesso" (Addendum §4): toccarli non e' mai
# auto-allow, nemmeno dentro files_allowed. Percorsi RELATIVI a self_root.
SELF_PROTECTED = (
    "app/security.py",
    "app/policy.py",
    "app/config.py",
    "app/backends.py",
    ".claude",   # tutta la cartella .claude/
)


def _is_self_protected(resolved: Path, self_root: Path | None) -> bool:
    """True se `resolved` cade dentro self_root E combacia con SELF_PROTECTED
    (un file esatto o qualsiasi cosa dentro una cartella protetta)."""
    if self_root is None:
        return False
    try:
        rel = resolved.relative_to(self_root)
    except ValueError:
        return False
    rel_posix = rel.as_posix()
    for pat in SELF_PROTECTED:
        if rel_posix == pat or rel_posix.startswith(pat + "/"):
            return True
    return False


def evaluate(tool_name: str, tool_input: dict, files_allowed: set[Path],
             roots: list[Path], bash_allowlist: list[str],
             self_root: Path | None = None, self_protect: bool = False) -> tuple[str, str]:
    """Ritorna ('allow'|'deny'|'ask', motivo). Nessun effetto collaterale.

    Se `self_protect` e' attivo e il path risolto e' un file sensibile di Argo
    dentro `self_root` (SELF_PROTECTED), il verdetto e' sempre 'ask' anche se il
    file e' nel perimetro `files_allowed` (Addendum §4)."""
    if tool_name in ("Write", "Edit"):
        raw = tool_input.get("file_path")
        if not raw:
            return "ask", "Write/Edit senza file_path"
        try:
            resolved = resolve_within_roots(raw, roots)
        except PathNotAllowed as e:
            return "deny", f"path fuori dalle root consentite: {e}"
        # guard "se stesso": i file di sicurezza di Argo non sono mai auto-allow.
        if self_protect and _is_self_protected(resolved, self_root):
            return "ask", f"file sensibile di Argo (progetto se stesso): {resolved}"
        if resolved in files_allowed:
            return "allow", "dentro il perimetro approvato col VIA"
        return "ask", f"file fuori dal perimetro del piano: {resolved}"

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if is_dangerous_bash(cmd):
            # deny deterministico, prima di tutto (§8, secondo strato)
            return "deny", "comando distruttivo bloccato dalla policy deterministica"
        if cmd_matches_allowlist(cmd, bash_allowlist):
            return "allow", "comando in allowlist"
        return "ask", "comando fuori allowlist"

    # tutto il resto (Read incluso di norma e' in allowed_tools): chiedi.
    return "ask", f"tool non coperto dal piano: {tool_name}"


def make_policy_gate(ctx: GateContext):
    """Costruisce il callback can_use_tool (§1.6) per un executor locale."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny  # lazy

    settings = get_settings()
    bus = get_bus()

    async def can_use_tool(tool_name, input_data, context):
        verdict, reason = evaluate(
            tool_name, input_data, ctx.files_allowed_resolved,
            ctx.roots, settings.bash_allowlist,
            self_root=settings.self_root.resolve(),
            self_protect=settings.self_protect,
        )

        if verdict == "allow":
            # auto-allow, ma tracciato: e' comunque un passaggio dal gate.
            await bus.emit(ctx.run_id, "policy_auto_allow", {
                "tool_name": tool_name, "reason": reason,
            })
            return PermissionResultAllow()

        if verdict == "deny":
            # traccia il verdetto per l'autofix (perimeter block) e sul bus.
            ctx.policy_events.append({"kind": "policy_deny", "tool_name": tool_name})
            await bus.emit(ctx.run_id, "policy_deny", {
                "tool_name": tool_name, "reason": reason,
                "tool_input": input_data,
            })
            return PermissionResultDeny(message=reason, interrupt=False)

        # verdict == "ask": ECCEZIONE -> UNA push. Metrica qualita' del piano.
        ctx.policy_events.append({"kind": "policy_ask", "tool_name": tool_name})
        ctx.push_counter["pushes"] = ctx.push_counter.get("pushes", 0) + 1
        decision = await get_broker().request(
            run_id=ctx.run_id, tool_name=tool_name, tool_input=input_data,
            title=reason,
        )
        if decision.status == "allowed":
            return PermissionResultAllow(updated_input=decision.updated_input)
        return PermissionResultDeny(
            message=decision.reason or "negato", interrupt=False)

    return can_use_tool
