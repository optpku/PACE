from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.contracts.verification import missing_kernel_evidence
from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.output.results import FINDINGS_INDEX_REF, FINDINGS_CATALOG_REF, finding_entries, validate_publication
from pace.runtime.output.workspace import work_ref
from pace.runtime.prompts import (
    load_prompt_template,
    prompt_root,
    render_prompt_template,
)
from pace.agents.roles import SemanticRoleSpec
from pace.runtime.schema import open_object, validate_json_instance
from pace.runtime.semantic import (
    SemanticCallResult,
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    representation_failure_payload,
    semantic_call,
    SemanticCallSpec,
    StructuredSemanticResult,
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
    ) + "\n\n" + load_prompt_template("output/finding_publication.md", workdir=workdir)
    return _checked_publication_call(
        llm, prompt, workdir=workdir,
        call_name=f"finding_verifier_{round_index:04d}_{Path(draft_ref).stem}",
        spec=SemanticRoleSpec(
            role="finding_verifier", schema=_finding_receipt_schema,
            host_schema_version="finding_verification",
            authority="MathVerifierKernel", fatal_scope="CALL",
            validator=_validate_finding_receipt,
        ).call_spec(),
        cache_ref=str(Path(draft_ref).with_suffix(".checked.json")),
        manuscript_ref=draft_ref,
        input_refs=input_refs,
        host_metadata={
            "agent_id": "finding_verifier", "round_index": round_index,
            "draft_ref": draft_ref, "target_contract_id": target_contract_id,
        },
    )


def _finding_receipt_schema():
    schema = open_object({
        "stage_status": {"type": "string", "enum": ["PASS", "MATH_FAIL", "VERIFIER_ERROR"]},
        "reuse_authorized": {"type": "boolean"},
        "publication": open_object({"action": {"type": "string"}, "finding_ref": {"type": "string"}}),
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


def _recoverable_check(llm, prompt, *, workdir, call_name, spec, input_refs, host_metadata):
    """Reuse a committed, identically bound response before its caller checkpoint.

    In-flight native completions still use semantic_call's transport recovery.
    This covers the later boundary, after the semantic receipt has committed.
    """
    import hashlib
    from pace.runtime.output.workspace import stage_ref
    owner = host_metadata.get("stage_ref") or stage_ref(host_metadata.get("round_index", 0), workdir=workdir)
    for record_path in sorted((workdir / owner / "calls").glob("*/record.json"), reverse=True):
        record = json.loads(record_path.read_text())
        if (record.get("status") not in {"SUCCEEDED", "REPRESENTATION_FAILED"}
                or record.get("role") != spec.role or record.get("stage") != call_name
                or record.get("prompt_sha256") != hashlib.sha256(prompt.encode()).hexdigest()
                or record.get("input_refs") != list(dict.fromkeys(input_refs))):
            continue
        trace = json.loads(record_path.with_name("interface_result.json").read_text())
        host = trace["host"]
        metadata = host.get("host_metadata", {})
        if (metadata.get("verification_identity") != host_metadata.get("verification_identity")
                or metadata.get("transport", {}).get("status") != "SUCCESS"
                or any(row.get("stage") == "acceptance" for row in trace.get("diagnostics", []))):
            continue
        try:
            payload = json.loads((workdir / host["response_ref"]).read_text())
        except (OSError, ValueError):
            continue
        errors = validate_json_instance(payload, spec.semantic_schema)
        if not errors and spec.validator:
            errors = spec.validator(payload)
        # Only reconstruct the committed response; never claim it passed before
        # the current owner applies its publication and mathematical validators.
        if errors:
            return SemanticRepresentationFailure(payload, host, tuple(errors))
        return StructuredSemanticResult(payload, host)
    return semantic_call(llm, prompt, workdir=workdir, call_name=call_name, spec=spec,
                         input_refs=input_refs, host_metadata=host_metadata)


def _checked_publication_call(llm, prompt, *, workdir, spec, cache_ref, manuscript_ref,
                              call_name, input_refs, host_metadata):
    """Save the independent check; repair only a rejected publication directive."""
    import hashlib
    from dataclasses import replace
    from pace.runtime.output.workspace import _write_json_atomic
    root = Path(workdir)
    path = root / cache_ref
    identity = {"manuscript_sha256": hashlib.sha256((root / manuscript_ref).read_bytes()).hexdigest(),
                "target_contract_id": host_metadata.get("target_contract_id", ""), "role": spec.role,
                "inputs": {ref: hashlib.sha256((root / ref).read_bytes()).hexdigest()
                           for ref in input_refs if (root / ref).is_file()
                           and ref not in {FINDINGS_INDEX_REF, FINDINGS_CATALOG_REF}}}
    host_metadata = {**host_metadata, "verification_identity": identity}
    mathematical_schema = {**spec.semantic_schema, "properties": {
        key: value for key, value in spec.semantic_schema.get("properties", {}).items() if key != "publication"}}

    saved = json.loads(path.read_text()) if path.is_file() else {}
    if saved and saved["identity"] != identity:
        raise ValueError("Saved independent check belongs to another manuscript or target")

    def publication_errors(payload):
        if payload.get("stage_status") != "PASS":
            return []
        errors = validate_publication(root, payload.get("publication"))
        decision = payload.get("publication")
        if (spec.role == "finding_verifier" and payload.get("reuse_authorized") is not True
                and isinstance(decision, dict) and decision.get("action") in ("CREATE", "UPDATE")):
            errors.append({"stage": "finding_publication", "message": "Without reuse authorization choose NONE."})
        return errors

    if not saved:
        result = _recoverable_check(llm, prompt, workdir=root, call_name=call_name,
            spec=replace(spec, validator=lambda value: [*(spec.validator(value) if spec.validator else []),
                                                       *publication_errors(value)]),
            input_refs=input_refs, host_metadata=host_metadata)
        if not isinstance(result, (StructuredSemanticResult, SemanticRepresentationFailure)):
            return result
        if (not isinstance(result.payload, dict)
                or validate_json_instance(result.payload, mathematical_schema)
                or (spec.validator and spec.validator(result.payload))):
            return result
        # A malformed non-PASS response must not be upgraded to an accepted check.
        if isinstance(result, SemanticRepresentationFailure) and result.payload.get("stage_status") != "PASS":
            return result
        saved = {"identity": identity, "payload": result.payload, "host": result.host}
        _write_json_atomic(path, saved)
    payload = saved["payload"]
    if not saved.get("complete") and publication_errors(payload):
        response = cache_ref
        inputs = (response, *((FINDINGS_CATALOG_REF,) if (root / FINDINGS_CATALOG_REF).is_file() else ()))
        corrected = _recoverable_check(llm,
            f"Correct only the publication object in the saved independent check `{response}`. "
            "The mathematical verdict, evidence and manuscript are immutable. Do not reverify, rewrite, "
            "research or retrieve sources. Choose CREATE, UPDATE with an existing catalog finding_ref, "
            "or NONE; retain uncertainty as NONE. Return only {\"publication\":{...}}. "
            + json.dumps(publication_errors(payload), ensure_ascii=False),
            workdir=root, call_name=call_name + "_publication_correction", input_refs=inputs,
            spec=SemanticCallSpec(role="PUBLICATION_CORRECTION",
                semantic_schema={"type":"object", "properties":{"publication":open_object({"action":{"type":"string"}, "finding_ref":{"type":"string"}})},
                                 "required":["publication"], "additionalProperties":False},
                host_schema_version="publication_correction/v1", authority="PUBLICATION_ONLY",
                validator=lambda value: publication_errors({**payload, "publication": value.get("publication")})),
            host_metadata={**host_metadata, "protected_input_refs": inputs,
                           "task_id": cache_ref + "-publication", "receipt_correction_owner": "PUBLICATION"})
        if not isinstance(corrected, StructuredSemanticResult):
            return corrected
        payload = {**payload, "publication": corrected.payload["publication"]}
        saved["publication_correction_host"] = corrected.host
    if payload.get("stage_status") == "PASS" and payload["publication"]["action"] == "UPDATE" and not saved.get("complete"):
        row = next(row for row in finding_entries(root) if row["ref"] == payload["publication"]["finding_ref"])
        payload["publication"]["expected_source_ref"] = row["source_ref"]
    saved.update(payload=payload, complete=True)
    _write_json_atomic(path, saved)
    return StructuredSemanticResult(payload=payload, host=saved["host"])


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
    if (workdir / FINDINGS_CATALOG_REF).is_file():
        refs["publication_catalog"] = FINDINGS_CATALOG_REF
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
    result = _checked_publication_call(
        relation_llm,
        prompt,
        workdir=workdir,
        call_name=call_name,
        input_refs=input_refs,
        spec=_MATH_VERIFIER_KERNEL_ROLE.call_spec(),
        cache_ref=str(Path(output_ref or proof_ref).with_name("kernel_checked.json")),
        manuscript_ref=proof_ref,
        host_metadata=metadata,
    )
    from pace.runtime.sessions import budget_exhausted

    corrections = 0
    if isinstance(result, SemanticRepresentationFailure) and not (workdir / str(Path(output_ref or proof_ref).with_name("kernel_checked.json"))).is_file() and not budget_exhausted(workdir) and not any(
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
        result = _checked_publication_call(
            relation_llm, correction, workdir=workdir,
            call_name=call_name + "_delivery_correction",
            input_refs=(*input_refs, response_ref),
            spec=_MATH_VERIFIER_KERNEL_ROLE.call_spec(),
            cache_ref=str(Path(output_ref or proof_ref).with_name("kernel_checked.json")),
            manuscript_ref=proof_ref,
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
        ("current findings for publication decisions", artifact_refs.get("publication_catalog", "")),
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
    prompt += "\n\n" + load_prompt_template("output/finding_publication.md", workdir=workdir)
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
            "publication": open_object({"action": {"type": "string"}, "finding_ref": {"type": "string"}}),
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
