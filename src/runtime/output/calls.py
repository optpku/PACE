"""Reference-only records for logical calls and their transport attempts."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pace.runtime.output.workspace import work_ref

from .workspace import _write_json_atomic, _write_text_atomic, stage_ref


def artifact_owner_metadata(root: Path, ref: str) -> dict[str, Any]:
    """Attribute an editorial/control call to its saved artifact, not orientation."""
    path = Path(ref)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Artifact owner must be run-relative")
    parts = path.parts
    if len(parts) >= 5 and parts[0] == "work" and parts[2] == "stages":
        owner = Path(*parts[:4]).as_posix()
        round_index = int(parts[3]) if parts[3].isdigit() else 0
    else:
        owner, round_index = path.parent.as_posix(), 0
    target = (root / Path(*parts[:2]) / "input/target_contract.json"
              if len(parts) >= 2 and parts[0] == "work" else None)
    contract = json.loads(target.read_text()) if target and target.is_file() else {}
    return {"stage_ref": owner, "round_index": round_index,
            "target_contract_id": contract.get("contract_id", ""),
            "task_id": ref}


def completed_native_response(record_path: Path) -> tuple[Path, str] | None:
    """Read a completed native response whose protected inputs still match.

    This proves transport completion only. The caller must reapply its current
    receipt/file validators and Host commit; it is not mathematical acceptance.
    Use only under the run execution lock, after orphan workers have stopped.
    """
    attempts = sorted((record_path.parent / "attempts").glob("*"))
    if not attempts:
        return None
    attempt = attempts[-1]
    protection = record_path.parent / "protected_inputs/manifest.json"
    try:
        transport = attempt / "transport_result.json"
        if transport.is_file() and json.loads(transport.read_text()).get("status") != "SUCCESS":
            return None
        response = (attempt / "response.txt").read_text(encoding="utf-8")
        events = [json.loads(line) for line in (attempt / "stdout.jsonl").read_text().splitlines() if line.strip()]
        messages = [e["item"]["text"] for e in events if e.get("type") == "item.completed"
                    and e.get("item", {}).get("type") == "agent_message"]
        if (not events or events[-1].get("type") != "turn.completed"
                or not messages or not response.strip() or messages[-1].strip() != response.strip()):
            return None
        protected = json.loads(protection.read_text())
        if protected.get("restored_paths") or any(
            not Path(row["path"]).is_file() or Path(row["path"]).is_symlink()
            or Path(row["path"]).read_bytes() != (protection.parent / row["snapshot"]).read_bytes()
            for row in protected["entries"]
        ):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return attempt, response


def pending_native_call(root: Path, *, role: str, stage: str, prompt: str,
                        input_refs: Iterable[str], metadata: Mapping[str, Any]) -> tuple[dict, str, Path, str] | None:
    """Find the exact unfinished request, never reuse a different task's response."""
    owner = metadata.get("stage_ref") or stage_ref(int(metadata.get("round_index", 0) or 0), workdir=root)
    for path in sorted((root / owner / "calls").glob("*/record.json"), reverse=True):
        record = json.loads(path.read_text())
        if (record.get("status") != "STARTED" or record.get("role") != role
                or record.get("stage") != stage
                or record.get("input_refs") != list(dict.fromkeys(input_refs))
                or any(record.get(key, "") != str(metadata.get(key) or "")
                       for key in ("target_contract_id", "task_id", "output_ref"))):
            continue
        if (record.get("prompt_sha256") and record["prompt_sha256"] != hashlib.sha256(prompt.encode()).hexdigest()
                or (root / record["prompt_ref"]).read_text() != prompt):
            continue
        recovered = completed_native_response(path)
        if recovered:
            return record, path.relative_to(root).as_posix(), *recovered
    return None


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
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "input_refs": list(dict.fromkeys(str(ref) for ref in input_refs)),
            "output_ref": str(meta.get("output_ref") or ""),
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
