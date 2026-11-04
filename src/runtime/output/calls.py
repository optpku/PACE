"""Reference-only records for logical calls and their transport attempts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pace.runtime.output.workspace import work_ref

from .workspace import _write_json_atomic, _write_text_atomic, stage_ref


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
    directory = Path(workdir) / owner / "calls" / call_id
    directory.mkdir(parents=True, exist_ok=False)
    prompt_ref = str(meta.get("prompt_ref") or "")
    if not prompt_ref:
        prompt_ref = (directory / "prompt.md").relative_to(workdir).as_posix()
        _write_text_atomic(Path(workdir) / prompt_ref, prompt)
    record_ref = (directory / "record.json").relative_to(workdir).as_posix()
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
            "prompt_ref": prompt_ref,
            "input_refs": list(dict.fromkeys(str(ref) for ref in input_refs)),
            "response_ref": "",
            "deliverable_ref": "",
            "invocation_ids": [],
        },
    )
    return record_ref


def call_directory(workdir: Path, record_ref: str) -> Path:
    return (Path(workdir) / record_ref).parent


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


def _delivery_input_bytes(root: Path, ref: str) -> bytes:
    path = root / ref
    content = path.read_bytes()
    if ref.endswith("/final_report.json") and ref == work_ref(root, "final_report.json"):
        # Delivery progress may change while the mathematical verdict stays
        # fixed. It must not invalidate a completed writing task on resume.
        report = json.loads(content)
        content = json.dumps(
            {key: report.get(key) for key in (
                "problem_id", "result", "answer_mode", "terminal_kind",
                "execution_mode", "target_contract_id", "target_contract_unavailable",
                "proof_binding", "reason_code", "termination_owner",
            )}, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
    return content


def delivery_commit_metadata(root: Path, *, inputs, outputs, result) -> dict:
    """Bind one terminal editorial task to its actual inputs and output bytes."""
    outputs = tuple(dict.fromkeys(outputs))
    folder = Path(outputs[0]).parent / "committed_files"
    metadata = {"delivery_result": result}
    for kind, refs in (("inputs", inputs), ("outputs", outputs)):
        snapshots = {}
        for index, ref in enumerate(dict.fromkeys(refs)):
            if kind == "inputs" and not (root / ref).is_file():
                continue
            snapshot = folder / kind / f"{index:04d}"
            (root / snapshot).parent.mkdir(parents=True, exist_ok=True)
            content = (_delivery_input_bytes(root, ref) if kind == "inputs"
                       else (root / ref).read_bytes())
            (root / snapshot).write_bytes(content)
            snapshots[ref] = snapshot.as_posix()
        metadata[f"delivery_{kind}"] = snapshots
    return metadata


def checkpoint_artifact_call(root: Path, attempt: str, call_ref: str) -> None:
    _write_json_atomic(root / attempt / "artifact_call.json", {"call_ref": call_ref})


def completed_delivery_call(root: Path, attempt: str) -> dict | None:
    """Read an explicitly checkpointed owner commit, never infer from a draft.

    This is local terminal-delivery recovery, not a mathematical proof cache.
    """
    if not attempt:
        return None
    checkpoint = root / attempt / "artifact_call.json"
    if not checkpoint.is_file():
        return None
    record = json.loads((root / json.loads(checkpoint.read_text())["call_ref"]).read_text())
    metadata = record.get("terminal_metadata", {})
    if record["status"] != "SUCCEEDED" or not metadata.get("delivery_outputs"):
        return None
    for ref, snapshot in metadata["delivery_inputs"].items():
        if not (root / ref).is_file() or _delivery_input_bytes(root, ref) != (root / snapshot).read_bytes():
            raise ValueError(f"Completed delivery input changed: {ref}; preserve the old task")
    for ref, snapshot in metadata["delivery_outputs"].items():
        if (not (root / ref).is_file()
                or (root / ref).read_bytes() != (root / snapshot).read_bytes()):
            raise ValueError(f"Completed delivery output changed: {ref}; preserve the old task")
    return metadata


def update_agent_call_deliverable(
    workdir: Path,
    *,
    call_id: str,
    deliverable_ref: str,
) -> bool:
    """Attach a file committed after the structured response was accepted."""
    records = list(Path(workdir).glob(f"{work_ref(workdir)}/stages/*/calls/{call_id}/record.json"))
    if not records:
        return False
    (path,) = records
    record = json.loads(path.read_text(encoding="utf-8"))
    record["deliverable_ref"] = str(deliverable_ref)
    _write_json_atomic(path, record)
    return True
