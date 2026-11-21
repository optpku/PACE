"""Reference-only records for logical calls and their transport attempts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pace.runtime.output.workspace import work_ref

from .workspace import _write_json_atomic, _write_text_atomic, artifact_attempt, stage_ref


def begin_call(
    workdir: Path,
    *,
    call_id: str,
    role: str,
    stage: str,
    prompt: str,
    input_refs: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Persist the request before execution; the caller supplies stage ownership."""
    meta = dict(metadata or {})
    owner = str(
        meta.get("stage_ref") or stage_ref(int(meta.get("round_index", 0) or 0), workdir=workdir)
    )
    # Bind nested RCI/verification calls to their outer assignment without exposing private logs.
    context_path = Path(workdir) / owner / 'assignment/context.json'
    if not context_path.is_file():
        context_path = Path(workdir) / stage_ref(int(meta.get('round_index', 0) or 0), workdir=workdir) / 'assignment/context.json'
    context = json.loads(context_path.read_text()) if context_path.is_file() else {}
    directory = Path(workdir) / owner / "calls" / call_id
    directory.mkdir(parents=True, exist_ok=False)
    # Metadata may be inherited from an author by a correction/review call.
    # Bind execution to the actual prompt argument, never that inherited path.
    source_prompt_ref = str(meta.get("prompt_ref") or "")
    prompt_ref = (directory / "prompt.md").relative_to(workdir).as_posix()
    _write_text_atomic(Path(workdir) / prompt_ref, prompt)
    record_ref = (directory / "record.json").relative_to(workdir).as_posix()
    from pace.runtime.agent_environment import role_home

    home = meta.get("agent_home") or role_home(workdir, role)
    _write_json_atomic(
        Path(workdir) / record_ref,
        {
            "call_id": call_id,
            "agent_id": str(meta.get("agent_id") or role),
            "role": role,
            "stage": stage,
            "stage_ref": owner,
            "target_contract_id": str(meta.get("target_contract_id") or ""),
            "round_index": int(meta.get("round_index", 0) or 0),
            "parent_call_id": str(meta.get("parent_call_id") or ""),
            "task_id": str(meta.get("task_id") or ""),
            "parent_task_id": str(meta.get("parent_task_id") or ""),
            "status": "STARTED",
            "branch_id": context.get("branch_id", ""),
            "obligation_ids": context.get("obligation_ids", []),
            "assignment_ref": context.get("assignment_ref", ""),
            "attempt_id": str(meta.get("attempt_id") or ""),
            "prompt_ref": prompt_ref,
            "source_prompt_ref": source_prompt_ref,
            "input_refs": list(dict.fromkeys(str(ref) for ref in input_refs)),
            "archive_refs": list(dict.fromkeys(str(ref) for ref in meta.get("archive_refs", ()))),
            "response_ref": "",
            "deliverable_ref": "",
            "invocation_ids": [],
            "agent_home": str(home) if home else "",
            "thread_id": "",
        },
    )
    return record_ref


def call_directory(workdir: Path, record_ref: str) -> Path:
    return (Path(workdir) / record_ref).parent


def select_reader_attempts(
    workdir: Path, records: Iterable[dict[str, Any]], *, publication_refs: Iterable[str] = (),
) -> tuple[dict[str, dict[str, Any]], dict[Path, Path]]:
    """Select ACP/Web reading attempts; mathematical iterations keep their own task IDs."""
    root = Path(workdir)
    records = tuple(records)
    attempt_tasks = {(row["stage_ref"], row["agent_id"], row["task_id"])
                     for row in records if row["task_id"] and row["deliverable_ref"]
                     and artifact_attempt(root / row["deliverable_ref"]) is not None}

    def key(row):
        task = row["task_id"]
        name = re.sub(r"-attempt-\d+$", "", task)
        name = "/".join(part for part in Path(name).parts if not re.fullmatch(r"attempt[-_]\d+", part))
        return ((row["stage_ref"], row["agent_id"], name)
                if name != task or (row["stage_ref"], row["agent_id"], task) in attempt_tasks
                else row["call_id"])

    chosen = {}
    for row in records:
        identity = key(row)
        previous = chosen.get(identity)
        if previous is None or row["status"] == "SUCCEEDED" or previous["status"] != "SUCCEEDED":
            chosen[identity] = row
    aliases = {row["call_id"]: chosen[key(row)] for row in records}
    attempts = {}
    refs = [row["deliverable_ref"] for row in records
            if aliases[row["call_id"]] is row and row["status"] == "SUCCEEDED" and row["deliverable_ref"]]
    for ref in (*refs, *publication_refs):
        attempt = artifact_attempt(root / ref)
        if attempt is not None:
            attempts[attempt.parent] = attempt
    return aliases, attempts


def attempt_directory(workdir: Path, record_ref: str, attempt_index: int) -> Path:
    directory = (
        call_directory(workdir, record_ref) / "attempts" / f"{attempt_index:04d}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def finish_call(
    workdir: Path,
    record_ref: str,
    *,
    status: str,
    response_ref: str = "",
    deliverable_ref: str = "",
    invocation_ids: Iterable[str] = (),
    error: Mapping[str, Any] | None = None,
    terminal_metadata: Mapping[str, Any] | None = None,
) -> None:
    path = Path(workdir) / record_ref
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(
        status=status,
        response_ref=response_ref,
        deliverable_ref=deliverable_ref,
        invocation_ids=list(dict.fromkeys(item for item in invocation_ids if item)),
        error=dict(error or {}),
    )
    if terminal_metadata is not None:
        record["terminal_metadata"] = dict(terminal_metadata)
    _write_json_atomic(path, record)


def update_agent_call_deliverable(
    workdir: Path,
    *,
    call_id: str,
    deliverable_ref: str,
) -> bool:
    """Attach a file committed after the structured response was accepted."""
    records = list(Path(workdir).glob(f"{work_ref(workdir)}/*/calls/{call_id}/record.json"))
    if not records:
        return False
    (path,) = records
    record = json.loads(path.read_text(encoding="utf-8"))
    record["deliverable_ref"] = str(deliverable_ref)
    _write_json_atomic(path, record)
    return True
