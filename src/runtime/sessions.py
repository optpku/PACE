"""Record owned Host sessions and enforce soft budgets at dispatch boundaries."""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pace.artifacts.workspace import _write_json_atomic, work_ref

_ACTIVE = {}
_MODEL_FAILURES = {}


def model_failure(root: Path):
    return _MODEL_FAILURES.get(str(root.resolve()))


def record_model_failure(root: Path, error) -> None:
    """A terminal service failure ends this Host session's model dispatches."""
    key = str(root.resolve())
    if key in _ACTIVE:
        _MODEL_FAILURES.setdefault(key, error)
        from pace.runtime.processes import cancel_model_calls

        cancel_model_calls(root)


def saved_execution_config(root: Path, writing_request_ref: str = "") -> dict:
    """Read the last execution settings for this turn or this writing request."""
    path = root / work_ref(root, "state/sessions.json")
    if path.is_file():
        for row in reversed(json.loads(path.read_text())):
            if row.get(
                "writing_request_ref", writing_request_ref
            ) == writing_request_ref and row.get("effective_config"):
                return row["effective_config"]
    path = root / work_ref(root, "config.json")
    return json.loads(path.read_text()) if path.is_file() else {}


@contextmanager
def run_session(root: Path, *, config=None, writing_request_ref: str = ""):
    root = root.resolve()
    ref = root / work_ref(root, "state/sessions.json")
    rows = json.loads(ref.read_text()) if ref.is_file() else []
    for previous in rows:
        if previous.get("status") == "running" and not previous.get("ended_at"):
            previous.update(
                status="end_unobserved",
                measurement_gap="Previous Host session ended without a closing record; elapsed time and OS exit code are unknown.",
            )
    row = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "writing_request_ref": writing_request_ref,
        "elapsed_seconds": 0,
        "os_exit_code": None,
        "exit_code_note": "Host session record; OS exit code is not observed in-process.",
    }
    if config is not None:
        from dataclasses import asdict

        row["effective_config"] = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(config).items()
        }
    for name, key in (
        ("state/research_execution_policy.json", "policy"),
        ("resources/manifest.json", "resources"),
    ):
        path = root / work_ref(root, name)
        if path.is_file():
            row[key] = json.loads(path.read_text(encoding="utf-8"))
    if rows and rows[-1].get("ended_at"):
        row["gap_since_previous_session_seconds"] = max(
            0,
            (
                datetime.fromisoformat(row["started_at"])
                - datetime.fromisoformat(rows[-1]["ended_at"])
            ).total_seconds(),
        )
    rows.append(row)
    _write_json_atomic(ref, rows)
    started = time.monotonic()
    previous_math_time = sum(
        x.get("elapsed_seconds", 0)
        for x in rows[:-1]
        if not x.get("writing_request_ref")
    )
    _ACTIVE[str(root)] = (started, previous_math_time)
    try:
        yield
        failure = model_failure(root)
        if failure is not None:
            raise failure
        row["status"] = "completed"
    except BaseException:
        row["status"] = "interrupted"
        failure = model_failure(root)
        if failure is not None:
            row["model_failure"] = failure.to_json()
            raise failure from None
        raise
    finally:
        for name, key in (
            ("state/research_execution_policy.json", "policy"),
            ("resources/manifest.json", "resources"),
        ):
            path = root / work_ref(root, name)
            if path.is_file():
                row[key] = json.loads(path.read_text(encoding="utf-8"))
        row.update(
            ended_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=time.monotonic() - started,
        )
        _write_json_atomic(ref, rows)
        _ACTIVE.pop(str(root), None)
        _MODEL_FAILURES.pop(str(root), None)


def budget_exhausted(root: Path) -> str:
    root = root.resolve()
    policy_path = root / work_ref(root, "state/research_execution_policy.json")
    if not policy_path.is_file():
        return ""
    policy = json.loads(policy_path.read_text())
    if policy.get("max_calls"):
        from pace.artifacts.events import read_events

        writing_calls = {
            "research_report_outline",
            "research_report_writer",
            "theory_paper_outline",
            "theory_paper_writer",
        }
        if (
            sum(
                event["event_kind"] == "SEMANTIC_CALL_STARTED"
                and event.get("call_name") not in writing_calls
                for event in read_events(root)
            )
            >= policy["max_calls"]
        ):
            return "TOTAL_CALL_BUDGET_EXHAUSTED"
    if policy.get("max_seconds") and str(root) in _ACTIVE:
        started, previous = _ACTIVE[str(root)]
        if previous + time.monotonic() - started >= policy["max_seconds"]:
            return "ACTIVE_TIME_BUDGET_EXHAUSTED"
    return ""
