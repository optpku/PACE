from __future__ import annotations

import fcntl
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from pace.runtime.output.workspace import work_ref

from .workspace import _write_json_atomic, stage_ref, utc_now

logger = logging.getLogger(__name__)
MODEL_REPRESENTATION_RETRY_CLASS = "MODEL_REPRESENTATION"
TERMINAL_OUTCOMES = {
    "SUCCEEDED",
    "REPRESENTATION_FAILED",
    "DOMAIN_REJECTED",
    "INTERFACE_FAILED",
    "ABORTED_BY_DEPENDENCY",
    "ABORTED_BY_USER",
}
_READ_CACHE: dict[str, dict[str, Any]] = {}


def initialize_events(workdir: Path) -> None:
    path = Path(workdir) / work_ref(workdir, 'events.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def append_event(workdir: Path, event_kind: str, **fields: Any) -> dict[str, Any]:
    """Serialize writers across threads/processes and assign one delivery cursor."""
    path = Path(workdir) / work_ref(workdir, 'events.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    event_id = str(fields.pop("event_id", "") or "EV-" + uuid.uuid4().hex)
    key = str(
        fields.pop("idempotency_key", "")
        or (_idempotency_key(event_kind, fields) if fields.get("call_id") else event_id)
    )
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        # A killed writer may leave an incomplete final line. Keep committed
        # lines and let the next event occupy the next complete cursor.
        if content and not content.endswith("\n"):
            content = content.rsplit("\n", 1)[0] + "\n" if "\n" in content else ""
            stream.seek(0)
            stream.truncate()
            stream.write(content)
        rows = _decode_rows(content)
        previous = next((row for row in rows if row["idempotency_key"] == key), None)
        if previous is not None:
            return previous
        event = {
            "event_id": event_id,
            "sequence": rows[-1]["sequence"] + 1 if rows else 1,
            "event_kind": str(event_kind),
            "timestamp": utc_now(),
            "idempotency_key": key,
            **fields,
        }
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        return event


def _decode_rows(content: str) -> list[dict[str, Any]]:
    lines = content.splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith("\n") and line.strip()]


def read_events(workdir: Path, after: int = 0, *, turn_id: str | None = None) -> list[dict[str, Any]]:
    path = Path(workdir) / work_ref(workdir, 'events.jsonl', turn_id=turn_id)
    try:
        with path.open(encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            stat = path.stat()
            key = str(path.resolve())
            saved = _READ_CACHE.get(key, {})
            if (saved.get("inode") != stat.st_ino
                    or saved.get("offset", 0) > stat.st_size
                    or (saved.get("size") == stat.st_size
                        and saved.get("mtime") != stat.st_mtime_ns)):
                saved = {}
            stream.seek(saved.get("offset", 0))
            content = stream.read()
            complete = content[:content.rfind("\n") + 1] if "\n" in content else ""
            rows = [*saved.get("rows", ()), *_decode_rows(complete)]
            _READ_CACHE[key] = {
                "inode": stat.st_ino, "size": stat.st_size, "mtime": stat.st_mtime_ns,
                "offset": saved.get("offset", 0) + len(complete.encode("utf-8")),
                "rows": rows,
            }
    except FileNotFoundError:
        return []
    return [row for row in rows if row["sequence"] > after]


def read_updates(workdir: Path, after: int = 0) -> list[dict[str, Any]]:
    return [
        row for row in read_events(workdir, after) if row["event_kind"] == "PROGRESS"
    ]




def publish_progress(
    workdir: Path | None, *, task_id: str, stage: str, status: str,
    round_index: int = 0, parent_task_id: str = "", notice: str = "",
    task_count: int = 0, artifact_refs=(), publication_refs=None,
    final_report: dict | None = None, final_report_ref: str = "",
    execution_mode: str = "", mode_source: str = "", mode_reason: str = "",
    proof_loops_used: int = 0, max_proof_loops: int = 0,
) -> dict | None:
    """Persist an observed task transition after its scientific files commit."""
    if workdir is None:
        return None
    root = Path(workdir)
    terminal = final_report is not None or bool(final_report_ref) or status in {"cancelled", "stopped"}
    fields = dict(task_id=task_id, parent_task_id=parent_task_id, stage=stage,
        stage_ref=stage_ref(round_index, final=terminal or stage == "reporter", workdir=root),
        round_index=round_index, status=status, notice=notice, task_count=task_count,
        artifact_refs=list(artifact_refs), publication_refs=dict(publication_refs or {}),
        final_report_ref=final_report_ref or (work_ref(root, "final_report.json") if final_report is not None else ""),
        terminal=terminal, execution_mode=execution_mode, mode_source=mode_source,
        mode_reason=mode_reason, proof_loops_used=proof_loops_used, max_proof_loops=max_proof_loops)
    previous = [row for row in read_updates(root) if row["task_id"] == task_id and row["status"] == status]
    if previous and all(previous[-1].get(key) == value for key,value in fields.items()):
        return previous[-1]
    event = append_event(root, "PROGRESS", idempotency_key=f"progress:{task_id}:{status}:{len(previous)}", **fields)
    from .navigation import refresh_navigation
    refresh_navigation(root, stage=fields["stage_ref"])
    return event


def summarize_calls(events: Any) -> dict[str, Any]:
    rows = [
        row
        for row in (events if isinstance(events, list) else [])
        if isinstance(row, dict)
    ]
    started_order: list[str] = []
    started_rows: dict[str, dict[str, Any]] = {}
    terminal_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        call_id = str(row.get("call_id") or "")
        if not call_id:
            continue
        if row.get("event_kind") == "SEMANTIC_CALL_STARTED":
            if call_id not in started_rows:
                started_order.append(call_id)
                started_rows[call_id] = row
            continue
        if row.get("event_kind") in {
            "SEMANTIC_CALL_SUCCEEDED",
            "SEMANTIC_CALL_FAILED",
            "SEMANTIC_CALL_ABORTED",
        } and not bool(row.get("suppressed")):
            terminal_rows.setdefault(call_id, []).append(row)

    call_states: dict[str, dict[str, Any]] = {}
    counts = {outcome: 0 for outcome in TERMINAL_OUTCOMES}
    dangling_call_ids: list[str] = []
    multi_terminal_call_ids: list[str] = []
    for call_id in started_order:
        terminals = terminal_rows.get(call_id, [])
        outcomes = [_terminal_outcome(row) for row in terminals]
        if not terminals:
            state = "STARTED"
            dangling_call_ids.append(call_id)
        elif len(terminals) != 1 or len(set(outcomes)) != 1:
            state = "HOST_CLASSIFICATION_CONFLICT"
            multi_terminal_call_ids.append(call_id)
        else:
            state = outcomes[0]
            counts[state] += 1
        call_states[call_id] = {
            "call_id": call_id,
            "role": str(started_rows[call_id].get("role") or ""),
            "stage": str(started_rows[call_id].get("stage") or ""),
            "state": state,
            "terminal_event_ids": [str(row.get("event_id") or "") for row in terminals],
        }

    unbound_terminal_call_ids = sorted(set(terminal_rows).difference(started_rows))
    succeeded = counts["SUCCEEDED"]
    representation_failed = counts["REPRESENTATION_FAILED"]
    domain_rejected = counts["DOMAIN_REJECTED"]
    interface_failed = counts["INTERFACE_FAILED"]
    aborted = counts["ABORTED_BY_DEPENDENCY"] + counts["ABORTED_BY_USER"]
    suppressed = {
        str(row.get("call_id") or row.get("event_id") or "")
        for row in rows
        if row.get("event_kind") == "CALL_SUPPRESSED_BY_CIRCUIT"
    }
    invocations = {
        str(row.get("invocation_id") or "")
        for row in rows
        if row.get("event_kind") == "TRANSPORT_ATTEMPT_FINISHED"
        and str(row.get("invocation_id") or "")
    }
    attempts = {
        (str(row.get("invocation_id") or ""), int(row.get("attempt_index") or 0))
        for row in rows
        if row.get("event_kind") == "TRANSPORT_ATTEMPT_FINISHED"
        and str(row.get("invocation_id") or "")
        and int(row.get("attempt_index") or 0) > 0
    }
    warnings = [
        row for row in rows if row.get("event_kind") == "TRANSPORT_WARNING_OBSERVED"
    ]
    recovered = {
        str(row.get("invocation_id") or row.get("event_id") or "")
        for row in rows
        if row.get("event_kind") == "TRANSPORT_RECOVERED"
    }
    recovered_representation_call_ids = sorted(
        {
            str(row.get("call_id") or "")
            for row in rows
            if row.get("event_kind") == "RECOVERED_REPRESENTATION_FAILURE"
            and str(row.get("call_id") or "")
        }
    )
    explicit_conflicts = [
        row for row in rows if row.get("event_kind") == "HOST_CLASSIFICATION_CONFLICT"
    ]
    semantic_failures = representation_failed + domain_rejected + interface_failed
    conflict_count = len(explicit_conflicts) + len(multi_terminal_call_ids)
    equation_valid = len(started_order) == (
        succeeded
        + semantic_failures
        + aborted
        + len(dangling_call_ids)
        + len(multi_terminal_call_ids)
    )
    return {
        "semantic_call_count": len(started_order),
        "semantic_success_count": succeeded,
        "semantic_failure_count": semantic_failures,
        "semantic_aborted_count": aborted,
        "user_interrupted_call_count": counts["ABORTED_BY_USER"],
        "transport_invocation_count": len(invocations),
        "transport_attempt_count": len(attempts),
        "interface_event_count": interface_failed,
        "transport_interface_event_count": interface_failed,
        "suppressed_call_count": len(suppressed),
        "transport_warning_count": len(warnings),
        "recovered_transport_warning_count": len(recovered),
        "recovered_representation_failure_count": len(
            recovered_representation_call_ids
        ),
        "recovered_representation_call_ids": recovered_representation_call_ids,
        "classification_conflict_count": conflict_count,
        "representation_failure_count": representation_failed,
        "domain_rejection_count": domain_rejected,
        "dangling_call_count": len(dangling_call_ids),
        "dangling_call_ids": dangling_call_ids,
        "multi_terminal_call_count": len(multi_terminal_call_ids),
        "multi_terminal_call_ids": multi_terminal_call_ids,
        "unbound_terminal_call_ids": unbound_terminal_call_ids,
        "summary_equation_valid": equation_valid,
        "call_states": call_states,
        "event_count": len(rows),
    }


def write_call_summary(workdir: Path) -> dict[str, Any]:
    root = Path(workdir)
    events = read_events(root)
    summary = {
        **summarize_calls(events),
        "event_position": event_position(root),
    }
    _write_json_atomic(root / work_ref(workdir, 'state/call_summary.json'), summary)
    _write_json_atomic(
        root / work_ref(workdir, 'state/call_consistency.json'),
        {
            "status": (
                "PASS"
                if bool(summary.get("summary_equation_valid"))
                and int(summary.get("dangling_call_count") or 0) == 0
                and int(summary.get("multi_terminal_call_count") or 0) == 0
                and not summary.get("unbound_terminal_call_ids")
                and int(summary.get("classification_conflict_count") or 0) == 0
                else "FAIL"
            ),
            "event_position": summary["event_position"],
            "summary_equation_valid": bool(summary.get("summary_equation_valid")),
            "dangling_call_ids": list(summary.get("dangling_call_ids") or []),
            "multi_terminal_call_ids": list(
                summary.get("multi_terminal_call_ids") or []
            ),
            "unbound_terminal_call_ids": list(
                summary.get("unbound_terminal_call_ids") or []
            ),
            "classification_conflict_count": int(
                summary.get("classification_conflict_count") or 0
            ),
        },
    )
    return summary


def _terminal_outcome(row: dict[str, Any]) -> str:
    explicit = str(row.get("terminal_outcome") or "").upper()
    if explicit in TERMINAL_OUTCOMES:
        return explicit
    kind = str(row.get("event_kind") or "")
    if kind == "SEMANTIC_CALL_SUCCEEDED":
        return "SUCCEEDED"
    if kind == "SEMANTIC_CALL_ABORTED":
        return "ABORTED_BY_DEPENDENCY"
    failure_class = str(row.get("failure_class") or "").upper()
    retry_class = str(row.get("retry_class") or "").upper()
    if (
        failure_class in {"REPRESENTATION", "REPRESENTATION_FAILED"}
        or retry_class == MODEL_REPRESENTATION_RETRY_CLASS
    ):
        return "REPRESENTATION_FAILED"
    if failure_class in {"DOMAIN", "DOMAIN_REJECTED"}:
        return "DOMAIN_REJECTED"
    if failure_class in {"ABORTED", "ABORTED_BY_DEPENDENCY"}:
        return "ABORTED_BY_DEPENDENCY"
    return "INTERFACE_FAILED"


def event_position(workdir: Path) -> str:
    events = read_events(workdir)
    return str(events[-1].get("event_id") or "") if events else ""


def record_transport_attempt_started(
    workdir: Path,
    *,
    call_id: str,
    call_name: str,
    role: str,
    stage: str,
    invocation_id: str,
    attempt_index: int,
    artifact_dir: Path | None = None,
) -> None:
    append_event(
        workdir,
        "TRANSPORT_ATTEMPT_STARTED",
        call_id=call_id,
        call_name=call_name,
        role=role,
        stage=stage,
        invocation_id=invocation_id,
        attempt_index=int(attempt_index),
        status="STARTED",
        suppressed=False,
        artifact_refs=_transport_result_ref(workdir, artifact_dir),
    )


def record_transport_attempt_finished(
    workdir: Path,
    result: Any,
    *,
    call_id: str,
    role: str,
    stage: str,
    attempt_index: int,
) -> None:
    invocation_id = str(getattr(result, "invocation_id", "") or "")
    status = ("ABORTED" if getattr(result, "error_code", "") == "interrupted"
              else "FAILED" if hasattr(result, "error_code") else "SUCCESS")
    artifact_ref = _transport_result_ref(workdir, getattr(result, "artifact_dir", None))
    append_event(
        workdir,
        "TRANSPORT_ATTEMPT_FINISHED",
        call_id=call_id,
        call_name=str(getattr(result, "call_name", "") or ""),
        role=role,
        stage=stage,
        invocation_id=invocation_id,
        attempt_index=int(attempt_index),
        status=status,
        terminal_event_type=str(getattr(result, "terminal_event_type", "") or ""),
        error_code=str(getattr(result, "error_code", "") or ""),
        retry_class=str(getattr(result, "retry_class", "NONE") or "NONE"),
        retryable=bool(getattr(result, "retryable", False)),
        suppressed=bool(getattr(result, "suppressed", False)),
        artifact_refs=artifact_ref,
    )
    warnings = tuple(getattr(result, "warnings", ()) or ())
    for warning_index, warning in enumerate(warnings, start=1):
        append_event(
            workdir,
            "TRANSPORT_WARNING_OBSERVED",
            call_id=call_id,
            call_name=str(getattr(result, "call_name", "") or ""),
            role=role,
            stage=stage,
            invocation_id=invocation_id,
            warning_index=warning_index,
            warning=dict(warning)
            if isinstance(warning, dict)
            else {"message": str(warning)},
            authority_evidence=dict(getattr(result, "authority_evidence", {}) or {}),
            artifact_refs=artifact_ref,
        )
    if warnings and status == "SUCCESS":
        append_event(
            workdir,
            "TRANSPORT_RECOVERED",
            call_id=call_id,
            call_name=str(getattr(result, "call_name", "") or ""),
            role=role,
            stage=stage,
            invocation_id=invocation_id,
            warning_count=len(warnings),
            terminal_event_type=str(getattr(result, "terminal_event_type", "") or ""),
            output_schema_valid=getattr(result, "output_schema_valid", None),
            artifact_refs=artifact_ref,
        )


def _transport_result_ref(workdir: Path, artifact_dir: Any) -> dict[str, str]:
    if not artifact_dir:
        return {"transport_result": ""}
    path = Path(artifact_dir) / "transport_result.json"
    try:
        relative = str(path.relative_to(Path(workdir)))
    except ValueError:
        relative = str(path)
    return {"transport_result": relative}


def _idempotency_key(event_kind: str, fields: dict[str, Any]) -> str:
    call_id = str(fields.get("call_id") or "")
    invocation_id = str(fields.get("invocation_id") or "")
    attempt_index = int(fields.get("attempt_index") or 0)
    suffix = ""
    if event_kind == "TRANSPORT_WARNING_OBSERVED":
        suffix = f":warning:{int(fields.get('warning_index') or 0)}"
    return f"{call_id}:{event_kind}:{invocation_id}:{attempt_index}{suffix}"
