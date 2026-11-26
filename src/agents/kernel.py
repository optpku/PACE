from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import render_role_task
from pace.agents.roles import SemanticRoleSpec, run_semantic_role
from pace.artifacts.events import append_event
from pace.artifacts.results import (
    FINDINGS_ARCHIVE_REF,
    FINDINGS_CATALOG_REF,
    FINDINGS_INDEX_REF,
    finding_entries,
)
from pace.artifacts.workspace import (
    is_rci_critic_material,
    work_ref,
)
from pace.contracts.research.memory import digest
from pace.contracts.verification.decision import (
    COMPACT_KERNEL_VERSION,
    missing_kernel_evidence,
    validate_compact_kernel,
    validate_publication,
    without_invalid_compact_exports,
)
from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.prompts import (
    standard_workflow,
    prompt_root,
    render_prompt_template,
)
from pace.runtime.schema import (
    STRING,
    array_of,
    closed_object,
    open_object,
    validate_json_instance,
)
from pace.runtime.semantic import (
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    StructuredSemanticResult,
    representation_failure_payload,
    recover_rejected_receipt,
    semantic_call,
)

SCHEMA_VERSION = "math_verifier_kernel/v17"
CALL_SCHEMA_VERSION = "math_verifier_kernel_call/v2"


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
    on_accept: Callable[[dict], None] | None = None,
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
    for key, ref in (
        ("publication_overview", FINDINGS_INDEX_REF),
        ("publication_catalog", FINDINGS_CATALOG_REF),
        ("publication_archive", FINDINGS_ARCHIVE_REF),
    ):
        if (workdir / ref).is_file():
            refs[key] = ref
    compact = standard_workflow(workdir)
    if compact:
        refs.setdefault("proof_file", binding["proof_ref"])
        refs.setdefault("problem_file", work_ref(workdir, "input/problem.md"))
        refs.setdefault(
            "target_contract_file", work_ref(workdir, "input/target_contract.json")
        )
    input_refs = tuple(
        dict.fromkeys(
            ref for ref in (*refs.values(), *_kernel_method_refs(workdir)) if ref
        )
    )
    archives = ()
    if compact:
        required = tuple(
            refs[key] for key in ("problem_file", "target_contract_file", "proof_file")
        )
        archives = tuple(
            ref
            for ref in input_refs
            if ref not in required
            and (workdir / ref).is_file()
            and not is_rci_critic_material(ref)
        )
        input_refs = required
    audit_bytes = None
    current_finding_refs = None
    if compact:
        audit_ref = refs.get("auditor_file")
        audit_bytes = (workdir / audit_ref).read_bytes() if audit_ref else b"{}"
        current_finding_refs = {row["ref"] for row in finding_entries(workdir)}
    prompt = build_math_verifier_kernel_prompt(
        artifact_refs=refs, workdir=workdir, audit_bytes=audit_bytes
    )
    role = replace(_MATH_VERIFIER_KERNEL_ROLE, on_accept=on_accept)
    if compact:
        role = replace(
            role,
            schema=compact_kernel_schema,
            host_schema_version=COMPACT_KERNEL_VERSION,
            retain_schema_rejected_payload=True,
            validator=lambda report: validate_compact_kernel(
                report, current_finding_refs=current_finding_refs
            ),
        )
        if not current_finding_refs:
            prompt += (
                "\n\nThere are no current findings to update. Choose CREATE or NONE."
            )
    metadata = {
        "agent_id": "math_verifier_kernel",
        **({"archive_refs": archives} if compact else {}),
        "task_id": f"round-{int(round_index):04d}-kernel",
        "parent_task_id": f"round-{int(round_index):04d}",
        "round_index": int(round_index),
        "output_ref": str(output_ref or ""),
        "receipt_correction_owner": "KERNEL",
        "output_refs": [
            (Path(proof_file_ref).parent / "verification/memory_note.md").as_posix()
        ]
        if proof_file_ref
        else [],
        "output_dirs": [(Path(proof_file_ref).parent / "findings").as_posix()]
        if proof_file_ref
        else [],
        **binding,
    }
    call_name = f"math_verifier_kernel_round_{round_index}"
    result = None
    if compact and output_ref:
        # Recover the completed first evaluation even when interrupted before
        # its correction starts. Semantic calls already persist the decoded reply.
        metadata["audit_digest"] = (
            digest(audit_bytes) if refs.get("auditor_file") else ""
        )
        result = recover_rejected_receipt(
            workdir,
            call_name=call_name,
            role=role.role,
            metadata=metadata,
            binding_fields=(
                "proof_id",
                "proof_ref",
                "target_contract_id",
                "audit_digest",
                "output_ref",
            ),
        )
    if result is None:
        result = run_semantic_role(
            relation_llm,
            prompt,
            workdir=workdir,
            call_name=call_name,
            input_refs=input_refs,
            semantic_call_fn=semantic_call,
            role=role,
            host_metadata=metadata,
        )
    from pace.runtime.sessions import budget_exhausted

    if (
        compact
        and isinstance(result, SemanticRepresentationFailure)
        and not any(row.get("stage") == "acceptance" for row in result.diagnostics)
    ):
        original = result.payload
        if original.get("stage_status") in ("PASS", "MATH_FAIL"):
            cleaned = without_invalid_compact_exports(original)
            remaining = validate_json_instance(cleaned, compact_kernel_schema())
            semantic_errors = validate_compact_kernel(
                cleaned, current_finding_refs=current_finding_refs
            )
            diagnosed = {row["json_pointer"] for row in remaining}
            remaining.extend(
                row for row in semantic_errors if row["json_pointer"] not in diagnosed
            )
            if cleaned != original:
                append_event(
                    workdir,
                    "KERNEL_RECEIPT_NORMALIZED",
                    call_id=result.host["call_id"],
                    response_ref=result.host["response_ref"],
                    removed_export_rows=(
                        len(original["relation_checks"])
                        - len(cleaned.get("relation_checks", []))
                        if isinstance(original.get("relation_checks"), list)
                        else None
                    ),
                    discarded_publication_on_failure=any(
                        key in original and key not in cleaned
                        for key in ("publication", "publication_eligible")
                    ),
                )
            # Local exports are optional. Their malformed rows cannot invalidate
            # an otherwise complete independent verdict on the whole proof.
            if not remaining:
                if on_accept is not None:
                    on_accept(cleaned)
                result = StructuredSemanticResult(cleaned, result.host)
            else:
                result = SemanticRepresentationFailure(
                    cleaned, result.host, tuple(remaining)
                )

    corrections = 0
    if (
        isinstance(result, SemanticRepresentationFailure)
        and not budget_exhausted(workdir)
        and not any(row.get("stage") == "acceptance" for row in result.diagnostics)
    ):
        corrections = 1
        response_ref = str(result.host["response_ref"])
        original = result.payload
        fields = (
            _compact_correction_fields(original, current_finding_refs)
            if compact
            else {}
        )
        needs_evidence = bool({"summary", "proof_location"}.intersection(fields))
        controls_only = bool(fields) and not needs_evidence
        correction_role = role
        if fields:
            correction_role = replace(
                role,
                schema=lambda: closed_object(fields),
                host_schema_version=(
                    "kernel_publication_patch/v1"
                    if controls_only
                    else "kernel_receipt_patch/v1"
                ),
                validator=lambda patch: validate_compact_kernel(
                    {**original, **patch}, current_finding_refs=current_finding_refs
                ),
                on_accept=(lambda patch: on_accept({**original, **patch}))
                if on_accept
                else None,
            )
        correction = render_prompt_template(
            "kernel/receipt_correction.md",
            workdir=workdir,
            value_1=(
                "Return only the requested publication controls. The accepted judgment "
                "and manuscript location are frozen."
                if controls_only
                else (
                    prompt
                    + "\nReturn only the missing evidence and controls in the patch schema. "
                    "The saved verdict and previously valid evidence are fixed."
                    if fields
                    else prompt
                )
            ),
            value_2=response_ref,
            value_3=json.dumps(list(result.diagnostics), ensure_ascii=False),
        )
        result = run_semantic_role(
            relation_llm,
            correction,
            workdir=workdir,
            call_name=call_name + "_delivery_correction",
            input_refs=(response_ref,)
            if controls_only
            else (*input_refs, response_ref),
            semantic_call_fn=semantic_call,
            role=correction_role,
            host_metadata={
                **metadata,
                "parent_call_id": result.host["call_id"],
                "representation_correction_of": result.host["call_id"],
                "receipt_corrections": 1,
                **(
                    {
                        "read_scope": "declared",
                        "archive_refs": [
                            r
                            for r in archives
                            if r
                            in (
                                FINDINGS_CATALOG_REF,
                                FINDINGS_INDEX_REF,
                                FINDINGS_ARCHIVE_REF,
                            )
                        ],
                    }
                    if controls_only
                    else {}
                ),
                "output_refs": [],
                "output_dirs": [],
            },
        )
        if fields and isinstance(result, StructuredSemanticResult):
            result = StructuredSemanticResult(
                {**original, **result.payload}, result.host
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
            result,
            stage="MATH_VERIFIER_KERNEL",
        )
        return report
    return result.payload


def _compact_correction_fields(report, current_finding_refs):
    """Find all replaceable fields while retaining a coherent explicit verdict."""
    status = report.get("stage_status")
    if status not in ("PASS", "MATH_FAIL") or (
        status == "PASS" and report.get("publication_eligible") is False
    ):
        return {}
    schema = compact_kernel_schema()
    props = schema["properties"]
    fields = {key: props[key] for key in missing_kernel_evidence(report, compact=True)}
    if status == "PASS":
        if validate_publication(
            report, current_finding_refs=current_finding_refs
        ) or validate_json_instance(report.get("publication"), props["publication"]):
            fields["publication"] = props["publication"]
        if report.get("publication_eligible") is not True:
            fields["publication_eligible"] = props["publication_eligible"]
    preserved = {key: value for key, value in report.items() if key not in fields}
    preserved_schema = {
        **schema,
        "required": [key for key in schema["required"] if key not in fields],
    }
    return {} if validate_json_instance(preserved, preserved_schema) else fields


def attach_math_verifier_metadata(
    report: Any,
    *,
    compact: bool = False,
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
        **(
            {
                "canonical_blocker": {
                    "reason": report["summary"],
                    "proof_location": report["proof_location"],
                }
            }
            if compact
            and report.get("stage_status") == "MATH_FAIL"
            and not missing_kernel_evidence(report, compact=True)
            else {}
        ),
        "schema_version": COMPACT_KERNEL_VERSION if compact else SCHEMA_VERSION,
        **binding,
        "authority": "MathVerifierKernel",
    }


def build_math_verifier_kernel_prompt(
    *,
    artifact_refs: dict[str, str],
    workdir: Path,
    audit_bytes: bytes | None = None,
) -> str:
    if standard_workflow(workdir):
        audit_ref = artifact_refs.get("auditor_file")
        if audit_bytes is None:
            audit_bytes = (workdir / audit_ref).read_bytes() if audit_ref else b"{}"
        audit = json.loads(audit_bytes)
        hints = {
            k: audit[k] for k in ("kernel_hints", "blocking_violations") if audit.get(k)
        }
        files = [
            f"{key}: `{artifact_refs[key]}`"
            for key in ("problem_file", "target_contract_file", "proof_file")
            if artifact_refs.get(key)
        ]
        files.extend(
            f"On demand ({key}): `{ref}`"
            for key, ref in artifact_refs.items()
            if key not in {"problem_file", "target_contract_file", "proof_file"}
            and ref
            and (workdir / ref).is_file()
            and not is_rci_critic_material(ref)
        )
        files.append(f"Optional method index: `{_kernel_method_refs(workdir)[0]}`")
        if hints:
            files.append(
                "Questions from this candidate's Auditor: "
                + json.dumps(hints, ensure_ascii=False)
            )
        return render_role_task(
            "kernel/role.md", workdir=workdir, artifact_refs="\n".join(files)
        )
    files = [
        (
            "read completely: literal problem",
            artifact_refs.get("problem_file", work_ref(workdir, "input/problem.md")),
        ),
        (
            "target contract",
            artifact_refs.get(
                "target_contract_file", work_ref(workdir, "input/target_contract.json")
            ),
        ),
        ("problem orientation", artifact_refs.get("problem_brief_file", "")),
        ("read completely: current proof", artifact_refs.get("proof_file", "")),
        ("on-demand Plan context", artifact_refs.get("plan_file", "")),
        (
            "verification authority projection (selected external sources and inherited verified results)",
            artifact_refs.get("source_file", "verification_authority.json"),
        ),
        (
            "current-candidate Auditor report and hints",
            artifact_refs.get("auditor_file", ""),
        ),
        (
            "publication-only navigation after independent verification",
            artifact_refs.get("publication_overview", ""),
        ),
        (
            "current stable finding entries",
            artifact_refs.get("publication_catalog", ""),
        ),
        (
            "immutable checked manuscripts and receipts",
            artifact_refs.get("publication_archive", ""),
        ),
    ]
    files.extend(
        (
            "read first: verification method core"
            if index == 0
            else "on-demand verification method",
            ref,
        )
        for index, ref in enumerate(_kernel_method_refs(workdir))
    )
    files.extend(
        ("on-demand mathematical context (not verified authority)", ref)
        for name, ref in artifact_refs.items()
        if name.startswith("search_context_")
    )
    unique_files = {}
    for purpose, path in files:
        if path:
            unique_files.setdefault(path, purpose)
    file_lines = "\n".join(
        f"- {purpose}: `{path}`" for path, purpose in unique_files.items()
    )
    prompt = render_role_task(
        "kernel/role.md",
        workdir=workdir,
        artifact_refs=file_lines,
    )
    proof_ref = artifact_refs.get("proof_file")
    if proof_ref:
        draft_dir = (Path(proof_ref).parent / "findings").as_posix()
        prompt += render_prompt_template(
            "kernel/finding_output.md", workdir=workdir, value_1=draft_dir
        )
        memory_ref = (Path(proof_ref).parent / "verification/memory_note.md").as_posix()
        prompt += render_prompt_template(
            "kernel/memory_output.md", workdir=workdir, value_1=memory_ref
        )
    return prompt


def _kernel_method_refs(workdir: Path) -> tuple[str, ...]:
    root = Path(workdir).resolve()
    method_root = prompt_root(root) / "kernel"
    return (
        (method_root / "verification_methods.md").relative_to(root).as_posix(),
        *(
            path.relative_to(root).as_posix()
            for path in sorted((method_root / "verification_cards").glob("*.md"))
        ),
    )


def math_verifier_kernel_output_schema() -> dict[str, Any]:
    # The conditional PASS delivery check belongs to the role validator.
    # Mathematical evidence retains its prompt-defined, open layout.
    return {
        "type": "object",
        "properties": {
            "stage_status": {"type": "string", "enum": ["PASS", "MATH_FAIL"]},
            "publication_eligible": {"type": "boolean"},
            "publication": open_object(
                {
                    "action": {"type": "string", "enum": ["CREATE", "UPDATE", "NONE"]},
                    "finding_ref": {"type": "string"},
                }
            ),
        },
        "required": ["stage_status"],
        "additionalProperties": True,
    }


def compact_kernel_schema() -> dict[str, Any]:
    fields = math_verifier_kernel_output_schema()["properties"]
    # Reuse is opt-in. Only exporting a separate lemma requires its scoped evidence.
    text = {**STRING, "minLength": 1}
    lemma = open_object(
        {
            "statement": text,
            "conditions": text,
            "supports": array_of(text, min_items=1),
            "allowed_success_contract_components": array_of(text, min_items=1),
            "status": {**STRING, "enum": ["PASS"]},
            "reuse_authorized": {"type": "boolean", "enum": [True]},
        }
    )
    lemma["required"] = list(lemma["properties"])
    fields.update(summary=text, proof_location=text, relation_checks=array_of(lemma))
    return closed_object(fields, required=("stage_status", "summary", "proof_location"))


def _validate_kernel_publication_delivery(
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    if report.get("stage_status") != "PASS":
        return []
    diagnostics = validate_publication(report)
    missing = missing_kernel_evidence(report, compact=False)
    if missing:
        diagnostics.append(
            {
                "stage": "kernel_publication_delivery",
                "reason_code": "kernel_publication_evidence_missing",
                "missing": missing,
            }
        )
    if report.get("publication_eligible") is not True:
        diagnostics.append(
            {
                "stage": "kernel_publication_delivery",
                "reason_code": "kernel_publication_not_authorized",
                "json_pointer": "/publication_eligible",
            }
        )
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


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    from pace.agents.prompt_composer import compose_instructions

    return compose_instructions(
        workdir=workdir,
        role="MATH_VERIFIER_KERNEL",
        resources=resources,
        refs=("kernel/role.md", "kernel/publication.md")
        if standard_workflow(workdir)
        else (
            "kernel/role.md",
            "shared/output/structured.md",
            "kernel/publication.md",
            "shared/output/findings.md",
            "shared/output/memory.md",
        ),
    )
