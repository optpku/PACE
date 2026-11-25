from __future__ import annotations

import copy
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pace.agents.auditor import (
    attach_proof_contract_audit_metadata,
)
from pace.agents.kernel import (
    attach_math_verifier_metadata,
    run_math_verifier_kernel,
)
from pace.artifacts.calls import update_agent_call_deliverable
from pace.artifacts.events import publish_progress
from pace.artifacts.workspace import (
    is_call_path,
    path_has_symlink_component,
    work_ref,
)
from pace.contracts.problem.target import target_contract_text
from pace.contracts.research.memory import digest
from pace.contracts.verification import decision as verification_decision
from pace.runtime.json_values import (
    as_dict as _as_dict,
)
from pace.runtime.json_values import (
    one_or_many_dict_rows as _rows,
)

VERIFICATION_AUTHORITY_SCHEMA_VERSION = "verification_authority/v1"


@dataclass(frozen=True)
class VerificationContext:
    proof_md: str
    workdir: Path
    contract_audit_runner: Callable[[Callable[[dict], None]], dict[str, Any]]
    math_kernel_llm: Any
    canonical_target: dict[str, Any]
    proof_id: str = ""
    previous_math_verifier_kernel: dict[str, Any] = field(default_factory=dict)
    previous_proof_md: str = ""
    previous_proof_id: str = ""
    proof_search_state: dict[str, Any] = field(default_factory=dict)
    round_index: int = 1
    proof_artifact_ref: str = ""
    plan_artifact_ref: str = "proof_plan.json"
    authority_artifact_ref: str = "verification_authority.json"
    auditor_artifact_ref: str = ""
    kernel_artifact_ref: str = ""
    report_artifact_ref: str = ""
    search_context_refs: dict[str, str] = field(default_factory=dict)
    agent_call_ids: dict[str, str] = field(default_factory=dict)


def run_verification_round(ctx: VerificationContext) -> dict[str, Any]:
    """Run one structural-first verification pass for the current proof."""

    proof_id = _require_binding_value(ctx.proof_id, "proof_id")
    proof_ref = _require_binding_value(ctx.proof_artifact_ref, "proof_ref")
    canonical_target = ctx.canonical_target
    target_contract_id = canonical_target["contract_id"]
    report_ref = _require_binding_value(ctx.report_artifact_ref, "report_artifact_ref")
    prefix = Path(report_ref).parent.as_posix()
    authority_projection = _source_catalog(ctx, target_contract_id=target_contract_id)
    if ctx.authority_artifact_ref:
        _write_workspace_json(
            Path(ctx.workdir),
            ctx.authority_artifact_ref,
            authority_projection,
            "source_authority_ref",
        )

    # Persist the exact current candidate's verification inputs before either role.
    # This only resumes a stage of this candidate; it is not a cross-proof cache.
    input_ref = f"{prefix}/verification_inputs.json"
    snapshot = {
        "proof_id": proof_id,
        "proof_ref": proof_ref,
        "proof_text": ctx.proof_md,
        "target": canonical_target,
        "authority": authority_projection,
    }
    input_path = Path(ctx.workdir) / input_ref
    if input_path.is_file():
        if json.loads(input_path.read_text(encoding="utf-8")) != snapshot:
            raise ValueError("verification inputs changed for the committed candidate")
    else:
        _write_workspace_json(
            Path(ctx.workdir), input_ref, snapshot, "verification inputs"
        )

    def saved_report(ref, field, allowed):
        path = Path(ctx.workdir) / ref if ref else None
        if path is not None and path.is_file():
            report = json.loads(path.read_text(encoding="utf-8"))
            identity = (
                report["artifact_identity"] if field == "contract_status" else report
            )
            target_identity = (
                report["target_identity"] if field == "contract_status" else report
            )
            if (
                report.get(field) in allowed
                and identity.get("proof_id") == proof_id
                and identity.get("proof_ref") == proof_ref
                and target_identity.get("target_contract_id") == target_contract_id
            ):
                return report
        return None

    progress_context = {
        "round_index": ctx.round_index,
        "parent_task_id": f"round-{ctx.round_index:04d}",
    }
    audit_task = f"round-{ctx.round_index:04d}-auditor"
    kernel_task = f"round-{ctx.round_index:04d}-kernel"
    publish_progress(
        ctx.workdir,
        **progress_context,
        task_id=audit_task,
        stage="auditor",
        status="started",
    )

    auditor = None

    def accept_audit(payload):
        nonlocal auditor
        report = attach_proof_contract_audit_metadata(
            payload,
            proof_id=proof_id,
            proof_ref=proof_ref,
            literal_target=target_contract_text(canonical_target),
            target_contract_id=target_contract_id,
        )
        if ctx.auditor_artifact_ref:
            _write_workspace_json(
                ctx.workdir, ctx.auditor_artifact_ref, report, "auditor_artifact_ref"
            )
        auditor = report

    cached_audit = saved_report(
        ctx.auditor_artifact_ref, "contract_status", {"PASS", "FAIL"}
    )
    auditor = cached_audit
    if auditor is None:
        response = ctx.contract_audit_runner(accept_audit)
        if auditor is None:
            accept_audit(response)
    audit_status = str(auditor.get("contract_status") or "VERIFIER_ERROR").upper()
    if ctx.auditor_artifact_ref:
        _update_agent_call_output(
            ctx,
            role_key="auditor",
            artifact_ref=ctx.auditor_artifact_ref,
        )
        publish_progress(
            ctx.workdir,
            **progress_context,
            task_id=audit_task,
            stage="auditor",
            status="completed" if audit_status == "PASS" else "needs_work",
        )
    publish_progress(
        ctx.workdir,
        **progress_context,
        task_id=kernel_task,
        stage="kernel",
        status="started",
    )
    audit_digest = digest(auditor)

    kernel = None

    def accept_kernel(payload):
        nonlocal kernel
        report = attach_math_verifier_metadata(
            payload,
            proof_id=proof_id,
            proof_ref=proof_ref,
            target_contract_id=target_contract_id,
        )
        report["auditor_digest"] = audit_digest
        if ctx.kernel_artifact_ref:
            _write_workspace_json(
                ctx.workdir, ctx.kernel_artifact_ref, report, "kernel_artifact_ref"
            )
        kernel = report

    kernel = saved_report(
        ctx.kernel_artifact_ref, "stage_status", {"PASS", "MATH_FAIL"}
    )
    if audit_status not in {"PASS", "FAIL"}:
        accept_kernel(
            {
                "stage_status": "VERIFIER_ERROR",
                "reason": "awaiting a completed structural audit",
            }
        )
    elif (
        kernel is None
        or kernel.get("auditor_digest", audit_digest if cached_audit else None)
        != audit_digest
    ):
        kernel = None
        response = _run_kernel(
            ctx, target_contract_id=target_contract_id, on_accept=accept_kernel
        )
        if kernel is None:
            accept_kernel(response)
    if ctx.kernel_artifact_ref:
        _update_agent_call_output(
            ctx,
            role_key="kernel",
            artifact_ref=ctx.kernel_artifact_ref,
        )
        publish_progress(
            ctx.workdir,
            **progress_context,
            task_id=kernel_task,
            stage="kernel",
            status="completed"
            if kernel.get("stage_status") == "PASS"
            else "needs_work",
        )

    final_result = verification_decision.decide_final_result(
        auditor=auditor,
        kernel=kernel,
    )
    repair_progress = build_repair_progress(
        current_proof_md=ctx.proof_md,
        current_proof_id=proof_id,
        current_kernel=kernel,
        final_result=final_result,
        previous_proof_md=ctx.previous_proof_md,
        previous_proof_id=ctx.previous_proof_id,
        previous_kernel=ctx.previous_math_verifier_kernel,
        source_delta=_as_dict(ctx.proof_search_state.get("survey_evidence_delta")),
        same_mechanism_stall_count=int(
            ctx.proof_search_state.get("same_mechanism_stall_count") or 0
        ),
    )
    index = {
        "proof_id": proof_id,
        "proof_ref": proof_ref,
        "target_contract_id": target_contract_id,
        "source_authority_ref": ctx.authority_artifact_ref,
        "auditor_ref": ctx.auditor_artifact_ref,
        "kernel_ref": ctx.kernel_artifact_ref,
        "final_result_ref": f"{prefix}/final_result.json",
        "repair_progress_ref": f"{prefix}/repair_progress.json",
    }
    for reference, value in (
        (index["final_result_ref"], final_result),
        (index["repair_progress_ref"], repair_progress),
        (report_ref, index),
    ):
        _write_workspace_json(
            Path(ctx.workdir), reference, value, "verification artifact"
        )
    return index


def _update_agent_call_output(
    ctx: VerificationContext,
    *,
    role_key: str,
    artifact_ref: str,
) -> None:
    call_id = str(ctx.agent_call_ids.get(role_key) or "").strip()
    if not call_id:
        return
    update_agent_call_deliverable(
        Path(ctx.workdir),
        call_id=call_id,
        deliverable_ref=artifact_ref,
    )


def _run_kernel(
    ctx: VerificationContext,
    *,
    target_contract_id: str,
    on_accept,
) -> dict[str, Any]:
    brief_ref = work_ref(ctx.workdir, "input/problem_brief.md")
    return run_math_verifier_kernel(
        proof_id=ctx.proof_id,
        proof_ref=ctx.proof_artifact_ref,
        target_contract_id=target_contract_id,
        relation_llm=ctx.math_kernel_llm,
        workdir=ctx.workdir,
        round_index=ctx.round_index,
        artifact_refs={
            **ctx.search_context_refs,
            "problem_file": work_ref(ctx.workdir, "input/problem.md"),
            "target_contract_file": work_ref(ctx.workdir, "input/target_contract.json"),
            "problem_brief_file": brief_ref
            if (Path(ctx.workdir) / brief_ref).is_file()
            else "",
            "proof_file": ctx.proof_artifact_ref,
            "plan_file": ctx.plan_artifact_ref,
            "source_file": ctx.authority_artifact_ref,
            "auditor_file": ctx.auditor_artifact_ref,
        },
        output_ref=ctx.kernel_artifact_ref,
        call_metadata=ctx.agent_call_ids,
        on_accept=on_accept,
    )


def _source_catalog(
    ctx: VerificationContext,
    *,
    target_contract_id: str,
) -> dict[str, Any]:
    search_state = _as_dict(ctx.proof_search_state)
    verified_progress = _as_dict(search_state.get("verified_progress"))
    inherited: list[dict[str, Any]] = []
    proof_bound: list[dict[str, Any]] = []
    current_target_ref = work_ref(ctx.workdir, "input/target_contract.json")
    for relation in _rows(verified_progress.get("verified_relations")):
        dependency = _as_dict(relation.get("verified_dependency"))
        if (
            str(relation.get("status") or "").upper() != "PASS"
            or dependency.get("target_contract_id") != target_contract_id
        ):
            continue
        # Preserve the Kernel's complete statement, conditions, supports and
        # scope. The Host transports its explicit reuse decision, never
        # derives authorization from a PASS label or fills mathematical gaps.
        row = copy.deepcopy(relation)
        if (
            relation.get("reuse_authorized") is True
            and dependency["target_contract_ref"] == current_target_ref
        ):
            inherited.append(row)
        else:
            row["usage_scope"] = "RECONSTRUCT_IN_CURRENT_PROOF"
            proof_bound.append(row)
    return {
        "schema_version": VERIFICATION_AUTHORITY_SCHEMA_VERSION,
        "source_snapshot_id": "",
        "target_contract_id": target_contract_id,
        "items": [],
        "inherited_authorities": inherited,
        "proof_bound_relations": proof_bound,
    }


def build_repair_progress(
    *,
    current_proof_md: str,
    current_proof_id: str,
    current_kernel: dict[str, Any],
    final_result: dict[str, Any],
    previous_proof_md: str = "",
    previous_proof_id: str = "",
    previous_kernel: dict[str, Any] | None = None,
    source_delta: dict[str, Any] | None = None,
    same_mechanism_stall_count: int = 0,
) -> dict[str, Any]:
    current_id = _require_binding_value(
        current_proof_id or current_kernel.get("proof_id"), "current_proof_id"
    )
    previous_id = str(
        previous_proof_id or _as_dict(previous_kernel).get("proof_id") or ""
    )
    verified = verification_decision.verified_relations(
        current_kernel,
        proof_id=current_id,
    )
    previous_verified = verification_decision.verified_relations(
        previous_kernel or {},
        proof_id=previous_id,
    )
    current_ids = {str(row.get("relation_id") or "") for row in verified}
    previous_ids = {str(row.get("relation_id") or "") for row in previous_verified}
    blockers = _rows(final_result.get("canonical_blockers"))
    blocker = blockers[0] if blockers else {}
    previous_blockers = _rows((previous_kernel or {}).get("canonical_blocker")) + _rows(
        (previous_kernel or {}).get("canonical_blockers")
    )
    previous_blocker = previous_blockers[0] if previous_blockers else {}
    proof_changed = not previous_id or current_proof_md != previous_proof_md
    blocker_changed = bool(previous_kernel) and blocker != previous_blocker
    new_ids = sorted(current_ids - previous_ids)
    lost_ids = sorted(previous_ids - current_ids)
    notes: list[str] = []
    if not proof_changed:
        notes.append("whole prover returned the parent proof unchanged")
    if blocker_changed:
        notes.append("the canonical mathematical blocker changed")
    if new_ids:
        notes.append("new proof-bound mathematical relations passed verification")
    if lost_ids:
        notes.append("previous proof-bound relations are absent from the current proof")
    return {
        "schema_version": "repair_progress/v1",
        "proof_changed": proof_changed,
        "blocker_changed": blocker_changed,
        "new_verified_lemma_ids": new_ids,
        "lost_proof_bound_lemma_ids": lost_ids,
        "new_source_delta": _as_dict(source_delta),
        "same_mechanism_stall_count": max(0, int(same_mechanism_stall_count or 0)),
        "advisory_notes": notes,
        "authority": "SEARCH_CONTEXT_ONLY",
    }


def _require_binding_value(value: Any, field_path: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_path} is required for verification binding")
    return normalized


def _write_workspace_json(
    root: Path,
    reference: str,
    payload: Any,
    field: str,
) -> None:
    """Atomically write a verifier artifact without following lexical aliases."""

    normalized = str(reference or "").strip().replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or is_call_path(path)
        or path.name.startswith("private_")
    ):
        raise ValueError(f"{field} must be a public relative workspace reference")

    root_path = Path(root).resolve()
    lexical = root_path / normalized
    if path_has_symlink_component(root_path, lexical):
        raise ValueError(f"{field} must not contain a symlink: {normalized}")
    try:
        target = lexical.resolve()
        target.relative_to(root_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{field} escapes the run workspace: {normalized}") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    if path_has_symlink_component(root_path, target):
        raise ValueError(f"{field} must not contain a symlink: {normalized}")
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=target.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
