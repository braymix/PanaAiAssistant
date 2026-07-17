"""§5.2: verify_cmd obbligatorio, files_allowed non vuoto, depends_on aciclico."""

import pytest

from app.briefs import PlanDocument, validate_plan, PlanValidationError


def _task(tid, verify="pytest -q", files=None, deps=None):
    return {
        "id": tid, "title": tid, "instructions": "fai X",
        "files_allowed": files if files is not None else ["src/a.py"],
        "verify_cmd": verify, "depends_on": deps or [],
    }


def test_valid_plan_ok():
    plan = PlanDocument.from_dict({"repo_path": "/r", "tasks": [_task("t1")]})
    validate_plan(plan)  # non solleva


def test_missing_verify_cmd_rejected():
    plan = PlanDocument.from_dict({"repo_path": "/r", "tasks": [_task("t1", verify="")]})
    with pytest.raises(PlanValidationError, match="verify_cmd"):
        validate_plan(plan)


def test_one_bad_task_rejects_whole_plan():
    plan = PlanDocument.from_dict({"repo_path": "/r",
        "tasks": [_task("t1"), _task("t2", verify="")]})
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_empty_files_allowed_rejected():
    plan = PlanDocument.from_dict({"repo_path": "/r", "tasks": [_task("t1", files=[])]})
    with pytest.raises(PlanValidationError, match="files_allowed"):
        validate_plan(plan)


def test_dangling_dependency_rejected():
    plan = PlanDocument.from_dict({"repo_path": "/r",
        "tasks": [_task("t1", deps=["ghost"])]})
    with pytest.raises(PlanValidationError, match="inesistente"):
        validate_plan(plan)


def test_cycle_rejected():
    plan = PlanDocument.from_dict({"repo_path": "/r",
        "tasks": [_task("t1", deps=["t2"]), _task("t2", deps=["t1"])]})
    with pytest.raises(PlanValidationError, match="[Cc]iclo"):
        validate_plan(plan)


def test_empty_plan_rejected():
    plan = PlanDocument.from_dict({"repo_path": "/r", "tasks": []})
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_dangerous_verify_cmd_rejected():
    # un verify_cmd distruttivo (gira con shell=True) fa rifiutare il piano
    plan = PlanDocument.from_dict({"repo_path": "/r",
        "tasks": [_task("t1", verify="rm -rf / && pytest")]})
    with pytest.raises(PlanValidationError, match="distruttivo"):
        validate_plan(plan)
