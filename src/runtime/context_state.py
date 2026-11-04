"""Thin persistence helpers for model-owned proof context.

These helpers deliberately do not define a mathematical schema.  The Host
only records file references, target identity needed for lifecycle binding,
and raw payloads returned by model or verifier stages.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from pace.runtime.json_values import one_or_many_dict_rows
from pace.runtime.output.workspace import RunWorkspace


def build_context_manifest(
    *,
    target_contract: dict[str, Any],
    problem_ref: str,
    target_contract_ref: str,
    problem_brief_ref: str,
    orientation_ref: str,
) -> dict[str, Any]:
    """Create a refs-only index for Prompt Composer inputs."""

    target_id = str(target_contract.get("contract_id") or "").strip()
    if not target_id:
        raise ValueError("context manifest requires the canonical target identity")
    return {
        "target_contract_id": target_id,
        "problem_ref": str(problem_ref),
        "target_contract_ref": str(target_contract_ref),
        "orientation_ref": orientation_ref,
        **({"problem_brief_ref": str(problem_brief_ref)} if problem_brief_ref else {}),
    }


def empty_search_progress(*, target_contract_id: str, target_contract_ref: str) -> dict[str, Any]:
    """Create the current scope's container for raw verifier evidence."""

    return {
        "target_contract_id": str(target_contract_id or ""),
        "target_contract_ref": target_contract_ref,
        "verified_relations": [],
        "rejected_relations": [],
        "current_blocker": {},
        "current_blockers": [],
    }


def snapshot_verified_progress(workdir: Path, *, snapshot_ref: str) -> str:
    """Freeze the parent's committed progress before its specialist tasks run."""

    root = Path(workdir)
    snapshot = root / snapshot_ref
    if snapshot.is_file():
        return snapshot_ref
    current = root / "memory/verified_progress.json"
    if not current.is_file():
        return ""
    workspace = RunWorkspace(root=root, problem_id="director-led")
    workspace.write_json(snapshot_ref, json.loads(current.read_text(encoding="utf-8")))
    return snapshot_ref


def stage_verified_progress(
    workdir: Path,
    *,
    output_ref: str,
    binding: Mapping[str, Any],
    kernel: Mapping[str, Any],
    final_result: Mapping[str, Any],
) -> str:
    """Persist one bound Kernel's raw local results without changing shared state."""

    if str(kernel.get("stage_status") or "").upper() not in {"PASS", "MATH_FAIL"}:
        return ""
    verified = []
    rejected = []
    for index, raw in enumerate(one_or_many_dict_rows(kernel.get("relation_checks"))):
        row = deepcopy(raw)
        row.update(
            proof_id=binding["proof_id"],
            target_contract_id=binding["target_contract_id"],
            target_contract_ref=binding["target_contract_ref"],
            verification_ref=binding["verification_ref"],
        )
        if str(row.get("status") or "").upper() == "PASS":
            row["verified_dependency"] = {
                "proof_id": binding["proof_id"],
                "proof_ref": binding["proof_ref"],
                "target_contract_id": binding["target_contract_id"],
                "target_contract_ref": binding["target_contract_ref"],
                "verification_ref": binding["verification_ref"],
                "kernel_ref": binding["kernel_ref"],
                "relation_index": index,
            }
            verified.append(row)
        else:
            rejected.append(row)
    blockers = deepcopy(final_result.get("canonical_blockers", []))
    payload = {
        "target_contract_id": binding["target_contract_id"],
        "target_contract_ref": binding["target_contract_ref"],
        "verified_relations": verified,
        "rejected_relations": rejected,
        "current_blockers": blockers,
        "current_blocker": deepcopy(blockers[0]) if blockers else {},
        "last_verification_ref": binding["verification_ref"],
        "last_proof_id": binding["proof_id"],
        "last_proof_ref": binding["proof_ref"],
    }
    RunWorkspace(root=Path(workdir), problem_id="director-led").write_json(
        output_ref, payload,
    )
    return output_ref


def commit_verified_progress(
    workdir: Path, verification_values: Iterable[Mapping[str, Any]], *,
    current_progress_ref: str = "",
) -> str:
    """Merge every task's evidence and project the selected candidate's state."""

    root = Path(workdir)
    current = root / "memory/verified_progress.json"
    local_refs = [value["verified_progress_ref"] for value in verification_values
                  if value.get("verified_progress_ref")]
    if not local_refs:
        return "memory/verified_progress.json" if current.is_file() else ""
    progress = json.loads(current.read_text(encoding="utf-8"))
    current_local = None
    for local_ref in local_refs:
        local = json.loads((root / local_ref).read_text(encoding="utf-8"))
        target_id = local["target_contract_id"]
        if (progress["target_contract_id"] != target_id
                or progress["target_contract_ref"] != local["target_contract_ref"]):
            raise ValueError("local verified progress targets a different scope")
        for key in ("verified_relations", "rejected_relations"):
            rows = progress[key]
            for row in local[key]:
                if row not in rows:
                    rows.append(deepcopy(row))
        if not current_progress_ref or local_ref == current_progress_ref:
            current_local = local
    if current_local is not None:
        for key in ("current_blockers", "current_blocker", "last_verification_ref", "last_proof_id", "last_proof_ref"):
            progress[key] = deepcopy(current_local[key])
    RunWorkspace(root=root, problem_id="director-led").write_json(
        "memory/verified_progress.json", progress,
    )
    return "memory/verified_progress.json"


__all__ = [
    "build_context_manifest",
    "empty_search_progress",
    "commit_verified_progress",
    "snapshot_verified_progress",
    "stage_verified_progress",
]
