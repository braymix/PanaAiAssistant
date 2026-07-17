"""Contratti dati §5.2. Il TaskBrief e' una SPECIFICA per un modello locale stupido.

Regole ferree (§5.2):
  * verify_cmd OBBLIGATORIO ed eseguibile: un task senza non entra in coda.
  * files_allowed e' il perimetro che il PolicyGate fa rispettare (§3.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PlanValidationError(ValueError):
    pass


@dataclass
class TaskBrief:
    id: str
    title: str
    files_allowed: list[str]
    context: str
    instructions: str
    acceptance: str
    verify_cmd: str
    verify_cwd: str = "."
    depends_on: list[str] = field(default_factory=list)
    max_turns: int = 25
    timeout_s: int = 900

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TaskBrief":
        missing = [k for k in ("id", "title", "instructions") if not d.get(k)]
        if missing:
            raise PlanValidationError(f"TaskBrief incompleto, mancano: {missing}")
        return TaskBrief(
            id=d["id"],
            title=d["title"],
            files_allowed=list(d.get("files_allowed") or []),
            context=d.get("context", ""),
            instructions=d["instructions"],
            acceptance=d.get("acceptance", ""),
            verify_cmd=(d.get("verify_cmd") or "").strip(),
            verify_cwd=d.get("verify_cwd", "."),
            depends_on=list(d.get("depends_on") or []),
            max_turns=int(d.get("max_turns", 25)),
            timeout_s=int(d.get("timeout_s", 900)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "files_allowed": self.files_allowed,
            "context": self.context, "instructions": self.instructions,
            "acceptance": self.acceptance, "verify_cmd": self.verify_cmd,
            "verify_cwd": self.verify_cwd, "depends_on": self.depends_on,
            "max_turns": self.max_turns, "timeout_s": self.timeout_s,
        }


@dataclass
class PlanDocument:
    repo_path: str
    tasks: list[TaskBrief]
    summary: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PlanDocument":
        tasks = [TaskBrief.from_dict(t) for t in (d.get("tasks") or [])]
        return PlanDocument(
            repo_path=d.get("repo_path", ""),
            tasks=tasks,
            summary=d.get("summary", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "summary": self.summary,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    def all_files(self) -> list[str]:
        seen: list[str] = []
        for t in self.tasks:
            for f in t.files_allowed:
                if f not in seen:
                    seen.append(f)
        return seen


def validate_plan(plan: PlanDocument) -> None:
    """Rifiuta il PlanDocument se anche UN solo task e' privo di verify_cmd (§5.2).

    Valida anche i riferimenti depends_on e l'assenza di cicli.
    """
    if not plan.tasks:
        raise PlanValidationError("PlanDocument senza task.")

    ids = [t.id for t in plan.tasks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise PlanValidationError(f"Id task duplicati: {sorted(dupes)}")

    for t in plan.tasks:
        if not t.verify_cmd:
            raise PlanValidationError(
                f"Task '{t.id}' ({t.title}) senza verify_cmd: rifiutato (§5.2)."
            )
        if not t.files_allowed:
            raise PlanValidationError(
                f"Task '{t.id}' senza files_allowed: perimetro vuoto, rifiutato (§3.2)."
            )
        for dep in t.depends_on:
            if dep not in ids:
                raise PlanValidationError(
                    f"Task '{t.id}' dipende da '{dep}' inesistente."
                )

    _assert_acyclic(plan.tasks)


def _assert_acyclic(tasks: list[TaskBrief]) -> None:
    graph = {t.id: list(t.depends_on) for t in tasks}
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(node: str, stack: list[str]) -> None:
        if state.get(node) == 1:
            return
        if state.get(node) == 0:
            cycle = " -> ".join(stack + [node])
            raise PlanValidationError(f"Ciclo in depends_on: {cycle}")
        state[node] = 0
        for dep in graph.get(node, []):
            visit(dep, stack + [node])
        state[node] = 1

    for t in tasks:
        visit(t.id, [])
