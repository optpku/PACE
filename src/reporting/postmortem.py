from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.reporting.terminal_report import (
    FinalReportEnvelope,
    FinalReportEnvelopeError,
)
from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.output.workspace import CURRENT_TURN_REF, utc_now, work_ref


def postmortem_rescan(
    batch_dir: Path,
    *,
    expected_problem_ids: list[str] | tuple[str, ...] | None = None,
    active: bool = False,
) -> dict[str, Any]:
    root = Path(batch_dir)
    expected = list(dict.fromkeys(str(item) for item in (expected_problem_ids or []) if str(item)))
    discovered = _problem_ids(root)
    problem_ids = expected or discovered
    problems = [_problem_summary(root, problem_id, active=active) for problem_id in problem_ids]
    completed = sum(row["artifact_class"] == "final_report" for row in problems)
    summary = {
        "schema_version": "runner_summary/v4",
        "updated_at": utc_now(),
        "run_status": "running" if active and completed < len(problems) else "completed",
        "expected_problem_count": len(problem_ids),
        "completed_problem_count": completed,
        "problems": problems,
    }
    (root / "_runner_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_binary_batch_aggregate(root, problems)
    return summary


def write_binary_batch_aggregate(
    batch_dir: Path,
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    root = Path(batch_dir)
    rows = [_aggregate_row(row) for row in problems if isinstance(row, dict)]
    counts = {
        result: sum(row["result"] == result for row in rows)
        for result in ("PASS", "FAILED", "missing")
    }
    payload = {
        "schema_version": "batch_aggregate/v4",
        "updated_at": utc_now(),
        "batch_dir": str(root),
        "final_report_count": sum(bool(row["final_report"]) for row in rows),
        "counts_by_result": counts,
        "rows": rows,
        "policy": "final_report/v8 is the sole terminal entry; missing is an artifact state",
    }
    (root / "_aggregate_final_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _aggregate_row(problem: dict[str, Any]) -> dict[str, Any]:
    report_path = Path(problem["final_report_file"]) if problem.get("final_report_file") else None
    report = _read_json(report_path)
    is_terminal = _is_terminal_report(report)
    result = str(report.get("result") or "missing").upper() if is_terminal else "missing"
    return {
        "problem_id": str(problem.get("problem_id") or ""),
        "result": result,
        "run_status": str(report.get("run_status") or "missing") if is_terminal else "missing",
        "run_dir": str(problem.get("run_dir") or ""),
        "final_report": str(report_path) if report_path else "",
        "termination_owner": str(report.get("termination_owner") or "") if is_terminal else "",
        "operational_failure_owner": (
            str(report.get("operational_failure_owner") or "") if is_terminal else ""
        ),
        "artifact_refs": _as_dict(report.get("artifact_refs")) if is_terminal else {},
    }


def _problem_ids(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith((".", "_")))


def _problem_summary(root: Path, problem_id: str, *, active: bool) -> dict[str, Any]:
    directory = root / problem_id
    row = _run_summary(directory)
    completed = bool(row["final_report_file"])
    return {
        **row, "problem_id": problem_id,
        "run_status": row["run_status"] if completed else "running" if active else "missing",
        "result": row["result"] if completed else "missing",
        "artifact_class": "final_report" if completed else "missing",
    }


def _run_summary(run_dir: Path) -> dict[str, Any]:
    report = run_dir / work_ref(run_dir, "final_report.json") if (run_dir / CURRENT_TURN_REF).is_file() else None
    payload = _read_json(report)
    is_terminal = _is_terminal_report(payload)
    artifact_refs = _as_dict(payload.get("artifact_refs")) if is_terminal else {}
    proof_ref = str(artifact_refs.get("proof") or "")
    return {
        "run_dir": str(run_dir),
        "final_report_file": str(report) if is_terminal else "",
        "run_status": str(payload.get("run_status") or "") if is_terminal else "",
        "result": str(payload.get("result") or "") if is_terminal else "",
        "termination_owner": (
            str(payload.get("termination_owner") or "") if is_terminal else ""
        ),
        "operational_failure_owner": (
            str(payload.get("operational_failure_owner") or "")
            if is_terminal
            else ""
        ),
        "artifact_refs": artifact_refs,
        "proof_file": str(run_dir / proof_ref) if proof_ref else "",
    }


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_terminal_report(value: dict[str, Any]) -> bool:
    try:
        FinalReportEnvelope.validate(value)
    except FinalReportEnvelopeError:
        return False
    return True
