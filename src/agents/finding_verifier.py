from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import render_role_task
from pace.agents.roles import SemanticRoleSpec, run_semantic_role
from pace.contracts.verification.decision import validate_publication
from pace.runtime.prompts import render_prompt_template
from pace.runtime.schema import array_of, open_object
from pace.runtime.semantic import (
    SemanticCallResult,
    semantic_call,
)


def verify_finding(
    *,
    llm: Any,
    workdir: Path,
    draft_ref: str,
    input_refs: tuple[str, ...],
    round_index: int,
    target_contract_id: str,
    application_request: dict | None = None,
    on_accept=None,
) -> SemanticCallResult:
    """Check one standalone result without granting whole-target acceptance."""

    prompt = render_role_task(
        "finding_verifier/role.md",
        workdir=workdir,
        draft_ref=draft_ref,
        target_contract_id=target_contract_id,
        input_files="\n".join(f"- `{ref}`" for ref in input_refs),
    )
    request = application_request
    if request:
        prompt += render_prompt_template(
            "finding_verifier/application_request.md",
            workdir=workdir,
            value_1=json.dumps(request, ensure_ascii=False),
        )
        prompt += render_prompt_template(
            "finding_verifier/application_task.md", workdir=workdir
        )
    return run_semantic_role(
        llm,
        prompt,
        workdir=workdir,
        call_name=f"finding_verifier_{round_index:04d}_{Path(draft_ref).stem}",
        role=SemanticRoleSpec(
            role="finding_verifier",
            schema=lambda: _finding_receipt_schema(request),
            host_schema_version="finding_verification",
            authority="MathVerifierKernel",
            fatal_scope="CALL",
            validator=lambda payload: validate_finding_application(
                payload, request, workdir
            ),
            on_accept=on_accept,
        ),
        input_refs=input_refs,
        semantic_call_fn=semantic_call,
        host_metadata={
            "agent_id": "finding_verifier",
            "round_index": round_index,
            "draft_ref": draft_ref,
            "target_contract_id": target_contract_id,
            "output_refs": [request["review_ref"]] if request else [],
            **(
                {
                    "purpose": "APPLICATION",
                    "application_keys": [
                        item["application_key"] for item in request["applications"]
                    ],
                }
                if request
                else {}
            ),
        },
    )


def _finding_receipt_schema(request=None):
    check = open_object(
        {
            "application_key": {"type": "string"},
            "obligation_id": {"type": "string"},
            "obligation_version": {"type": "string"},
            "status": {"type": "string", "enum": ["APPLICABLE", "NOT_ESTABLISHED"]},
            "review_ref": {"type": "string"},
        }
    )
    check["required"] = list(check["properties"])
    checks = array_of(check)
    if request:
        count = len(request["applications"])
        checks.update(minItems=count, maxItems=count)
    schema = open_object(
        {
            "stage_status": {
                "type": "string",
                "enum": ["PASS", "MATH_FAIL", "VERIFIER_ERROR"],
            },
            "reuse_authorized": {"type": "boolean"},
            "publication": open_object(
                {
                    "action": {"type": "string", "enum": ["CREATE", "UPDATE", "NONE"]},
                    "finding_ref": {"type": "string"},
                }
            ),
            "application_checks": checks,
        }
    )
    schema["required"] = ["stage_status", *(["application_checks"] if request else [])]
    return schema


def validate_finding_application(payload, request, workdir):
    """Apply identical authorization checks to new and recovered receipts."""
    errors = _validate_finding_receipt(payload)
    if not request:
        return errors
    checks = payload.get("application_checks", [])
    expected = {item["application_key"]: item for item in request["applications"]}
    seen = set()
    valid = isinstance(checks, list)
    for check in checks if valid else ():
        if not isinstance(check, dict):
            valid = False
            break
        key = check.get("application_key")
        item = expected.get(key)
        if (
            item is None
            or key in seen
            or check.get("obligation_id") != item["obligation_id"]
            or check.get("obligation_version") != item["obligation_version"]
            or check.get("status") not in {"APPLICABLE", "NOT_ESTABLISHED"}
            or check.get("review_ref") != request["review_ref"]
            or (
                check.get("status") == "APPLICABLE"
                and (
                    payload.get("stage_status") != "PASS"
                    or payload.get("reuse_authorized") is not True
                )
            )
        ):
            valid = False
        seen.add(key)
    if not valid or seen != set(expected):
        errors.append(
            {
                "reason_code": "invalid_application_review",
                "json_pointer": "/application_checks",
            }
        )
    if (
        not request["publication_requested"]
        and _as_dict(payload.get("publication")).get("action") != "NONE"
    ):
        errors.append(
            {
                "reason_code": "application_only_publication",
                "json_pointer": "/publication",
            }
        )
    from pace.contracts.research.memory import public_file

    try:
        if not public_file(workdir, request["review_ref"]).read_text().strip():
            raise ValueError("empty review")
    except (ValueError, OSError):
        errors.append(
            {
                "reason_code": "missing_application_review",
                "json_pointer": "/application_checks",
            }
        )
    return errors


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    from pace.agents.prompt_composer import compose_instructions

    return compose_instructions(
        workdir=workdir,
        role="FINDING_VERIFIER",
        resources=resources,
        refs=("finding_verifier/role.md", "kernel/publication.md"),
    )


def _validate_finding_receipt(payload):
    if (
        payload.get("stage_status") == "PASS"
        and type(payload.get("reuse_authorized")) is not bool
    ):
        return [
            {
                "stage": "finding_receipt",
                "json_pointer": "/reuse_authorized",
                "reason_code": "finding_authorization_missing",
                "message": "Explicitly decide whether this independently checked finding is authorized for reuse.",
            }
        ]
    diagnostics = validate_publication(payload)
    if (
        payload.get("stage_status") == "PASS"
        and payload.get("reuse_authorized") is not True
        and _as_dict(payload.get("publication")).get("action") in ("CREATE", "UPDATE")
    ):
        diagnostics.append(
            {
                "stage": "finding_receipt",
                "reason_code": "finding_reuse_not_authorized",
                "json_pointer": "/publication/action",
            }
        )
    return diagnostics


from pace.runtime.json_values import as_dict as _as_dict
