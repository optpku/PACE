from __future__ import annotations

from pathlib import Path
from typing import Any

from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.output.workspace import work_ref
from pace.runtime.prompts import load_prompt_template, render_prompt_template
from pace.agents.roles import SemanticRoleSpec
from pace.runtime.semantic import (
    SemanticInterfaceFailure,
    SemanticRepresentationFailure,
    representation_failure_payload,
    semantic_call,
)

SCHEMA_VERSION = "proof_contract_audit/v6"


def run_proof_contract_auditor(
    *,
    llm: Any,
    workdir: Path,
    artifact_refs: dict[str, str],
    call_name: str,
    round_index: int = 0,
    output_ref: str = "",
    target_contract_id: str = "",
    call_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute the model-facing Auditor role behind the agents boundary."""

    input_refs = tuple(
        ref for ref in artifact_refs.values() if str(ref or "")
    )
    result = semantic_call(
        llm,
        build_proof_contract_auditor_prompt(artifact_refs=artifact_refs, workdir=workdir),
        workdir=workdir,
        call_name=call_name,
        input_refs=input_refs,
        spec=_PROOF_CONTRACT_AUDITOR_ROLE.call_spec(),
        host_metadata={
            "agent_id": "proof_contract_auditor",
            "task_id": f"round-{int(round_index or 0):04d}-auditor",
            "parent_task_id": f"round-{int(round_index or 0):04d}",
            "round_index": int(round_index or 0),
            "output_ref": str(output_ref or ""),
            "target_contract_id": str(target_contract_id or ""),
        },
    )
    if call_metadata is not None:
        call_id = str(result.host.get("call_id") or "")
        call_metadata["call_id"] = call_id
        call_metadata["auditor"] = call_id
    if isinstance(result, SemanticInterfaceFailure):
        return {
            "contract_status": "VERIFIER_ERROR",
            "interface_failure": {
                "host": result.host,
                "diagnostics": list(result.diagnostics),
            },
        }
    if isinstance(result, SemanticRepresentationFailure):
        return {
            "contract_status": "VERIFIER_ERROR",
            "reported_output": result.payload,
            "semantic_failure": representation_failure_payload(
                result,
                stage="PROOF_CONTRACT_AUDITOR",
            ),
        }
    return result.payload


def attach_proof_contract_audit_metadata(
    report: Any,
    *,
    proof_id: str,
    proof_ref: str,
    target_contract_id: str,
    literal_target: str = "",
    target_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach candidate identity without rewriting the Auditor's evidence."""

    if not isinstance(report, dict):
        report = {
            "contract_status": "VERIFIER_ERROR",
            "interface_failure": {"reason": "auditor_output_is_not_object"},
            "reported_output": report,
        }
    raw = dict(report)
    current_proof_id = str(proof_id or "").strip()
    current_proof_ref = str(proof_ref or "").strip()
    current_target_contract_id = str(target_contract_id or "").strip()
    missing_bindings = [
        field
        for field, value in (
            ("proof_id", current_proof_id),
            ("proof_ref", current_proof_ref),
            ("target_contract_id", current_target_contract_id),
        )
        if not value
    ]
    if missing_bindings:
        raise ValueError(
            "proof contract audit metadata requires non-empty bindings: "
            + ", ".join(missing_bindings)
        )
    semantic_identity = _as_dict(target_identity) or _as_dict(raw.get("target_identity"))
    raw.update(
        {
            "schema_version": SCHEMA_VERSION,
            "literal_target": str(literal_target or raw.get("literal_target") or ""),
            "target_identity": {
                **semantic_identity,
                "target_contract_id": current_target_contract_id,
                "proof_id": current_proof_id,
            },
            "artifact_identity": {
                "proof_id": current_proof_id,
                "proof_ref": current_proof_ref,
            },
            "authority": "ProofContractAuditor",
        }
    )
    return raw


def build_proof_contract_auditor_prompt(*, artifact_refs: dict[str, str], workdir: Path) -> str:
    files = [
        ("read completely: literal problem", artifact_refs.get("problem_file", work_ref(workdir, 'input/problem.md'))),
        ("target contract", artifact_refs.get("target_contract_file", work_ref(workdir, 'input/target_contract.json'))),
        ("problem orientation", artifact_refs.get("problem_brief_file", "")),
        ("read completely: current proof", artifact_refs.get("proof_file", "")),
        ("current source authority bundle", artifact_refs.get("source_file", "memory/plan_source_bundle.json")),
        ("on-demand Plan context", artifact_refs.get("plan_file", "")),
        (
            "current verified mathematical progress",
            artifact_refs.get("verified_progress_file", ""),
        ),
    ]
    files.extend(("on-demand mathematical context (not verified authority)", ref)
                 for name, ref in artifact_refs.items() if name.startswith("search_context_"))
    unique_files = {}
    for purpose, path in files:
        if path:
            unique_files.setdefault(path, purpose)
    file_lines = "\n".join(f"- {purpose}: `{path}`" for path, purpose in unique_files.items())
    return render_prompt_template(
        "roles/proof_contract_auditor.md", workdir=workdir,
        artifact_refs=file_lines,
    ) + "\n\n" + load_prompt_template("output/structured.md", workdir=workdir)


def proof_contract_auditor_output_schema() -> dict[str, Any]:
    # The Auditor owns mathematical evidence.  The Host only needs the verdict
    # to route the loop; every other field is an open JSON payload.
    return {
        "type": "object",
        "properties": {
            "contract_status": {"type": "string", "enum": ["PASS", "FAIL"]},
        },
        "required": ["contract_status"],
        "additionalProperties": True,
    }


_PROOF_CONTRACT_AUDITOR_ROLE = SemanticRoleSpec(
    role="proof_contract_auditor",
    schema=proof_contract_auditor_output_schema,
    host_schema_version=SCHEMA_VERSION,
    authority="ProofContractAuditor",
)
