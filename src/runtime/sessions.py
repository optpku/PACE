"""Record owned Host sessions and enforce soft budgets at dispatch boundaries."""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pace.runtime.output.workspace import work_ref, _write_json_atomic

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


@contextmanager
def run_session(root: Path, *, config=None):
    root = root.resolve()
    ref = root / work_ref(root, "state/sessions.json")
    rows = json.loads(ref.read_text()) if ref.is_file() else []
    for previous in rows:
        if previous.get("status") == "running" and not previous.get("ended_at"):
            previous.update(status="end_unobserved", measurement_gap="Previous Host session ended without a closing record; elapsed time and OS exit code are unknown.")
    row = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
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
    for name, key in (("config.json", "config"), ("state/research_execution_policy.json", "policy"),
                      ("resources/manifest.json", "resources")):
        path = root / work_ref(root, name)
        if path.is_file():
            row[key] = json.loads(path.read_text(encoding="utf-8"))
    if rows and rows[-1].get("ended_at"):
        row["gap_since_previous_session_seconds"] = max(0, (
            datetime.fromisoformat(row["started_at"])
            - datetime.fromisoformat(rows[-1]["ended_at"])
        ).total_seconds())
    rows.append(row)
    _write_json_atomic(ref, rows)
    started = time.monotonic()
    _ACTIVE[str(root)] = (started, sum(x.get("elapsed_seconds", 0) for x in rows[:-1]))
    try:
        yield
        if model_failure(root) is not None:
            raise model_failure(root)
        row["status"] = "completed"
    except BaseException:
        row["status"] = "interrupted"
        if model_failure(root) is not None:
            row["model_failure"] = model_failure(root).to_json()
            raise model_failure(root) from None
        raise
    finally:
        for name, key in (("state/research_execution_policy.json", "policy"),
                          ("resources/manifest.json", "resources")):
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
    policy_path = root / work_ref(root, "state/research_execution_policy.json")
    if not policy_path.is_file():
        return ""
    policy = json.loads(policy_path.read_text())
    if policy.get("max_calls"):
        from pace.runtime.output.events import read_events

        if (
            sum(x["event_kind"] == "SEMANTIC_CALL_STARTED" for x in read_events(root))
            >= policy["max_calls"]
        ):
            return "TOTAL_CALL_BUDGET_EXHAUSTED"
    if policy.get("max_seconds") and str(root) in _ACTIVE:
        started, previous = _ACTIVE[str(root)]
        if previous + time.monotonic() - started >= policy["max_seconds"]:
            return "ACTIVE_TIME_BUDGET_EXHAUSTED"
    return ""
