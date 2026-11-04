from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.agents.roles import run_semantic_role
from pace.contracts.verification.decision import missing_kernel_evidence
from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.output.results import FINDINGS_INDEX_REF
from pace.runtime.output.workspace import work_ref
from pace.runtime.prompts import (
    load_prompt_template,
    prompt_root,
    render_prompt_template,
)
from pace.runtime.roles import SemanticRoleSpec
from pace.runtime.schema import open_object
from pace.runtime.semantic import (
    SemanticCallResult,
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    representation_failure_payload,
    semantic_call,
)

SCHEMA_VERSION = "math_verifier_kernel/v17"
CALL_SCHEMA_VERSION = "math_verifier_kernel_call/v2"


def verify_finding(
    *, llm: Any, workdir: Path, draft_ref: str,
    input_refs: tuple[str, ...], round_index: int, target_contract_id: str,
) -> SemanticCallResult:
    """Check one standalone result without granting whole-target acceptance."""

    prompt = render_prompt_template(
        "roles/finding_verifier.md", workdir=workdir,
        draft_ref=draft_ref,
        target_contract_id=target_contract_id,
        input_files="\n".join(f"- `{ref}`" for ref in input_refs),
    )
    return run_semantic_role(
        llm, prompt, workdir=workdir,
        call_name=f"finding_verifier_{round_index:04d}_{Path(draft_ref).stem}",
        role=SemanticRoleSpec(
            role="finding_verifier", schema=_finding_receipt_schema,
            host_schema_version="finding_verification",
            authority="MathVerifierKernel", fatal_scope="CALL",
            validator=_validate_finding_receipt,
        ),
        input_refs=input_refs,
        semantic_call_fn=semantic_call,
        host_metadata={
            "agent_id": "finding_verifier", "round_index": round_index,
            "draft_ref": draft_ref, "target_contract_id": target_contract_id,
        },
    )


def _finding_receipt_schema():
    schema = open_object({
        "stage_status": {"type": "string", "enum": ["PASS", "MATH_FAIL", "VERIFIER_ERROR"]},
        "reuse_authorized": {"type": "boolean"},
    })
    schema["required"] = ["stage_status"]
    return schema


def _validate_finding_receipt(payload):
    if payload.get("stage_status") == "PASS" and type(payload.get("reuse_authorized")) is not bool:
        return [{
            "stage": "finding_receipt", "json_pointer": "/reuse_authorized",
            "reason_code": "finding_authorization_missing",
            "message": "Explicitly decide whether this independently checked finding is authorized for reuse.",
        }]
    return []


def run_math_verifier_kernel(
    *,
    proof_id: str,
    proof_ref: str,
    target_contract_id: str,
    relation_llm: Any | None = None,
    workdir: Path | None = None,
    round_index: int = 1,
    artifact_refs: dict[str, str] | None = None,
    output_ref: str = "",
    call_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    binding = _kernel_binding(
        proof_id=proof_id,
        proof_ref=proof_ref,
        target_contract_id=target_contract_id,
    )
    refs = dict(artifact_refs or {})
    proof_file_ref = str(refs.get("proof_file") or "").strip()
    if proof_file_ref and proof_file_ref != binding["proof_ref"]:
        raise ValueError("proof_ref does not match artifact_refs.proof_file")
    if relation_llm is None or workdir is None:
        return _verifier_error("math_verifier_kernel_llm_required", binding=binding)
    if (workdir / FINDINGS_INDEX_REF).is_file():
        refs["publication_overview"] = FINDINGS_INDEX_REF
    prompt = build_math_verifier_kernel_prompt(
        artifact_refs=refs,
        workdir=workdir,
    )
    input_refs = tuple(dict.fromkeys((*refs.values(), *_kernel_method_refs(workdir))))
    metadata = {
        "agent_id": "math_verifier_kernel",
        "task_id": f"round-{int(round_index):04d}-kernel",
        "parent_task_id": f"round-{int(round_index):04d}",
        "round_index": int(round_index),
        "output_ref": str(output_ref or ""),
        "receipt_correction_owner": "KERNEL",
        **binding,
    }
    call_name = f"math_verifier_kernel_round_{round_index}"
    result = run_semantic_role(
        relation_llm,
        prompt,
        workdir=workdir,
        call_name=call_name,
        input_refs=input_refs,
        semantic_call_fn=semantic_call,
        role=_MATH_VERIFIER_KERNEL_ROLE,
        host_metadata=metadata,
    )
    from pace.runtime.sessions import budget_exhausted

    corrections = 0
    if isinstance(result, SemanticRepresentationFailure) and not budget_exhausted(workdir) and not any(
        row.get("stage") == "acceptance" for row in result.diagnostics
    ):
        corrections = 1
        response_ref = str(result.host["response_ref"])
        correction = (
            prompt + "\n\n## Complete this Kernel report's missing delivery\n"
            f"Your own incomplete response is saved in `{response_ref}`. This same-task "
            "response is delivery context, not mathematical authority. Independently check "
            "the same literal target and candidate; do not rewrite the candidate or supply "
            "a missing mathematical argument. Return valid JSON with an explicit stage_status "
            "and the complete requested report. "
            "For PASS, include actual nonempty relation_checks, target_clause_checks and "
            "final_assembly evidence, plus explicit publication_eligible=true. The schema "
            "is the minimum routing interface, not the complete report. If the independent "
            "check finds an unresolved mathematical issue, return MATH_FAIL with the actual "
            "gap and any valid local results instead of fabricating PASS evidence. "
            "Do not use other historical verdicts as authority. This is one delivery "
            "correction for the same candidate, not a new proof task.\n"
            + json.dumps(list(result.diagnostics), ensure_ascii=False)
        )
        result = run_semantic_role(
            relation_llm, correction, workdir=workdir,
            call_name=call_name + "_delivery_correction",
            input_refs=(*input_refs, response_ref),
            semantic_call_fn=semantic_call, role=_MATH_VERIFIER_KERNEL_ROLE,
            host_metadata={
                **metadata, "parent_call_id": result.host["call_id"],
                "representation_correction_of": result.host["call_id"],
                "receipt_corrections": 1,
            },
        )
    if call_metadata is not None:
        call_id = str(result.host.get("call_id") or "")
        call_metadata["call_id"] = call_id
        call_metadata["kernel"] = call_id
    if isinstance(result, SemanticInterfaceFailure):
        host = _as_dict(result.host)
        metadata = _as_dict(host.get("host_metadata"))
        transport = _as_dict(metadata.get("transport"))
        error_code = str(transport.get("error_code") or "semantic_interface_failure")
        report = _verifier_error(
            f"kernel_transport_failure:{error_code}",
            binding=binding,
        )
        report["interface_failure"] = {
            "stage": "MATH_VERIFIER_KERNEL",
            "error_code": error_code,
            "retry_class": str(transport.get("retry_class") or ""),
            "retryable": bool(transport.get("retryable")),
            "fatal_scope": str(metadata.get("fatal_scope") or ""),
            "diagnostics": list(result.diagnostics),
        }
        return report
    if isinstance(result, SemanticRepresentationFailure):
        report = _verifier_error(
            "kernel_semantic_representation_invalid",
            binding=binding,
        )
        report["interface_failure"] = {
            "stage": "MATH_VERIFIER_KERNEL_OUTPUT",
            "error_code": "semantic_representation_invalid",
            "retry_class": "MODEL_REPRESENTATION",
            "retryable": False,
            "receipt_corrections": corrections,
            "diagnostics": list(result.diagnostics),
        }
        report["reported_output"] = result.payload
        report["semantic_failure"] = representation_failure_payload(
            result, stage="MATH_VERIFIER_KERNEL",
        )
        return report
    return result.payload


def attach_math_verifier_metadata(
    report: Any,
    *,
    proof_id: str,
    proof_ref: str,
    target_contract_id: str,
) -> dict[str, Any]:
    """Bind the untouched mathematical report to this candidate."""
    binding = _kernel_binding(
        proof_id=proof_id,
        proof_ref=proof_ref,
        target_contract_id=target_contract_id,
    )
    if not isinstance(report, dict):
        report = {
            **_verifier_error("kernel_output_is_not_object", binding=binding),
            "reported_output": report,
        }
    return {
        **report,
        "schema_version": SCHEMA_VERSION,
        **binding,
        "authority": "MathVerifierKernel",
    }


def build_math_verifier_kernel_prompt(
    *,
    artifact_refs: dict[str, str],
    workdir: Path,
) -> str:
    files = [
        ("read completely: literal problem", artifact_refs.get("problem_file", work_ref(workdir, 'input/problem.md'))),
        ("target contract", artifact_refs.get("target_contract_file", work_ref(workdir, 'input/target_contract.json'))),
        ("problem orientation", artifact_refs.get("problem_brief_file", "")),
        ("read completely: current proof", artifact_refs.get("proof_file", "")),
        ("on-demand Plan context", artifact_refs.get("plan_file", "")),
        (
            "verification authority projection (selected external sources and inherited verified results)",
            artifact_refs.get("source_file", "verification_authority.json"),
        ),
        ("current-candidate Auditor report and hints", artifact_refs.get("auditor_file", "")),
        ("publication-only navigation after independent verification", artifact_refs.get("publication_overview", "")),
    ]
    files.extend(
        ("read first: verification method core" if index == 0 else "on-demand verification method", ref)
        for index, ref in enumerate(_kernel_method_refs(workdir))
    )
    files.extend(("on-demand mathematical context (not verified authority)", ref)
                 for name, ref in artifact_refs.items() if name.startswith("search_context_"))
    unique_files = {}
    for purpose, path in files:
        if path:
            unique_files.setdefault(path, purpose)
    file_lines = "\n".join(f"- {purpose}: `{path}`" for path, purpose in unique_files.items())
    prompt = render_prompt_template(
        "roles/math_verifier_kernel.md",
        workdir=workdir,
        artifact_refs=file_lines,
    ) + "\n\n" + load_prompt_template("output/structured.md", workdir=workdir)
    proof_ref = artifact_refs.get("proof_file")
    if proof_ref:
        draft_dir = (Path(proof_ref).parent / "findings").as_posix()
        prompt += (
            "\n\n" + load_prompt_template("output/findings.md", workdir=workdir)
            + f"\nFinding draft directory for this candidate: `{draft_dir}`. "
            "Apply the shared finding guidance to useful checked local results, "
            "including when the complete target fails. Leave existing drafts "
            "unchanged. A separate independent check precedes publication."
        )
        memory_ref = (Path(proof_ref).parent / "verification/memory_note.md").as_posix()
        prompt += (
            "\n\n" + load_prompt_template("output/memory.md", workdir=workdir)
            + f"\nMemory draft path for this Kernel task: `{memory_ref}`. "
            "Record useful checked local implications, exact failed steps and "
            "conditions that remain open. Keep the structured verdict authoritative; "
            "the optional note does not change that verdict."
        )
    return prompt


def _kernel_method_refs(workdir: Path) -> tuple[str, ...]:
    root = Path(workdir).resolve()
    method_root = prompt_root(root) / "kernel"
    return (
        (method_root / "verification_methods.md").relative_to(root).as_posix(),
        (prompt_root(root) / "output/findings.md").relative_to(root).as_posix(),
        (prompt_root(root) / "output/memory.md").relative_to(root).as_posix(),
        *(path.relative_to(root).as_posix() for path in sorted((method_root / "verification_cards").glob("*.md"))),
    )


def math_verifier_kernel_output_schema() -> dict[str, Any]:
    # The conditional PASS delivery check belongs to the role validator.
    # Mathematical evidence retains its prompt-defined, open layout.
    return {
        "type": "object",
        "properties": {
            "stage_status": {"type": "string", "enum": ["PASS", "MATH_FAIL"]},
            "publication_eligible": {"type": "boolean"},
        },
        "required": ["stage_status"],
        "additionalProperties": True,
    }


def _validate_kernel_publication_delivery(report: dict[str, Any]) -> list[dict[str, Any]]:
    if report.get("stage_status") != "PASS":
        return []
    diagnostics = []
    missing = missing_kernel_evidence(report)
    if missing:
        diagnostics.append({
            "stage": "kernel_publication_delivery",
            "reason_code": "kernel_publication_evidence_missing", "missing": missing,
        })
    if report.get("publication_eligible") is not True:
        diagnostics.append({
            "stage": "kernel_publication_delivery",
            "reason_code": "kernel_publication_not_authorized",
            "json_pointer": "/publication_eligible",
        })
    return diagnostics


def _verifier_error(
    reason: str,
    *,
    binding: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stage_status": "VERIFIER_ERROR",
        **binding,
        "authority": "MathVerifierKernel",
        "interface_failure": {
            "reason": str(reason or "kernel_interface_error"),
        },
    }


def _kernel_binding(
    *,
    proof_id: str,
    proof_ref: str,
    target_contract_id: str,
) -> dict[str, str]:
    binding = {
        "proof_id": str(proof_id or "").strip(),
        "proof_ref": str(proof_ref or "").strip(),
        "target_contract_id": str(target_contract_id or "").strip(),
    }
    missing = [
        field
        for field in ("proof_id", "proof_ref", "target_contract_id")
        if not binding[field]
    ]
    if missing:
        raise ValueError(f"{', '.join(missing)} required for Kernel binding")
    return binding


_MATH_VERIFIER_KERNEL_ROLE = SemanticRoleSpec(
    role="math_verifier_kernel",
    schema=math_verifier_kernel_output_schema,
    host_schema_version=CALL_SCHEMA_VERSION,
    authority="MathVerifierKernel",
    validator=_validate_kernel_publication_delivery,
)
