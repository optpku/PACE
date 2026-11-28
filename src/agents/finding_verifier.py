from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import render_role_task
from pace.agents.roles import SemanticRoleSpec, run_semantic_role
from pace.artifacts.results import FINDINGS_CATALOG_REF
from pace.artifacts.workspace import _write_text_atomic, is_rci_critic_material
from pace.contracts.verification.decision import validate_publication
from pace.runtime.prompts import (
    standard_workflow,
    render_prompt_template,
)
from pace.runtime.schema import array_of, closed_object, open_object
from pace.runtime.semantic import (
    SemanticCallResult,
    StructuredSemanticResult,
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
    compact=False,
    archive_refs=(),
    checked_premises=None,
) -> SemanticCallResult:
    """Check one standalone result without granting whole-target acceptance."""

    if is_rci_critic_material(draft_ref):
        raise ValueError("independent review cannot use RCI Critic material")
    request = application_request
    if compact or (request and request.get("compact_review")):
        return _verify_compact(
            llm=llm,
            workdir=workdir,
            draft_ref=draft_ref,
            refs=input_refs,
            round_index=round_index,
            target_id=target_contract_id,
            request=request,
            archive_refs=request.get("archive_refs", ()) if request else archive_refs,
            checked=(request or {}).get("checked_premises", checked_premises) or {},
            on_accept=on_accept,
        )
    prompt = render_role_task(
        "finding_verifier/role.md",
        workdir=workdir,
        draft_ref=draft_ref,
        target_contract_id=target_contract_id,
        input_files="\n".join(f"- `{ref}`" for ref in input_refs),
    )
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
        normalizer=_normalize_finding_publication,
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


def _verify_compact(
    *,
    llm,
    workdir,
    draft_ref,
    refs,
    round_index,
    target_id,
    request,
    archive_refs,
    checked,
    on_accept,
):
    items = (request or {}).get("applications", [])
    numbered = dict(enumerate(items, 1))
    publication = request is None or request["publication_requested"]
    checked_statement = (request or {}).get("checked_statement", "")
    application_view = [
        {
            "application": number,
            "obligation": item["obligation_id"],
            "scope": list(
                dict.fromkeys(
                    row["ref"] + row.get("locator", "")
                    for row in item["bindings"]
                    if row.get("locator")
                    or (
                        row["ref"] != draft_ref
                        and row["ref"] not in item["premise_refs"]
                    )
                )
            ),
            "premises": item["premise_refs"],
        }
        for number, item in numbered.items()
    ]
    host_publication = not publication and standard_workflow(workdir)
    task = (
        "Review the complete submitted manuscript and decide publication."
        if publication
        else "Check the requested applications. Return publication.action=NONE."
    )
    if host_publication:
        task = "Check each numbered application. The Host records publication NONE; omit the publication decision."
    if checked_statement:
        task += (
            "\nThe same manuscript has an accepted review. Use its checked statement and "
            "conditions below. Read only the new application argument and relevant evidence. "
            "Open other manuscript sections if a specific issue requires it.\n"
            f"Prior checked statement: {checked_statement}"
        )
    if publication and archive_refs:
        task += (
            "\nFor publication, consult the findings catalogue and a related checked original "
            "only if needed to decide CREATE, UPDATE or NONE."
        )
    if checked:
        scope_files = {
            row["ref"]
            for item in items
            for row in item["bindings"]
            if row.get("locator") or row["ref"] not in item["premise_refs"]
        }
        optional = {
            ref for source, (receipt, _) in checked.items() for ref in (source, receipt)
        } - scope_files
        refs = tuple(ref for ref in refs if ref not in optional)
        archive_refs = tuple(dict.fromkeys((*archive_refs, *sorted(optional))))
        task += (
            "\nPreviously checked premises (original and receipt available on demand):"
        )
        for source, (receipt, summary) in checked.items():
            task += f"\n- `{source}` (review `{receipt}`): {summary}"
    prompt = render_role_task(
        "finding_verifier/compact_task.md",
        workdir=workdir,
        draft_ref=draft_ref,
        target_contract_id=target_id,
        task=task,
        input_files="\n".join(f"- `{ref}`" for ref in refs if ref != draft_ref),
        archive_files=(
            f"`{FINDINGS_CATALOG_REF}` and its linked current findings, original sources and reviews."
            if FINDINGS_CATALOG_REF in archive_refs
            else ""
        ),
        applications=json.dumps(application_view, ensure_ascii=False),
    )
    schema = _finding_receipt_schema()
    schema["properties"]["summary"] = {"type": "string"}
    schema["required"].append("summary")
    if host_publication:
        schema["properties"]["publication"]["properties"]["action"]["enum"] = ["NONE"]
    else:
        schema["required"].append("publication")
    schema["properties"].pop("application_checks")
    if numbered:
        schema["properties"]["application_checks"] = array_of(
            closed_object(
                {
                    "application": {"type": "integer", "enum": list(numbered)},
                    "status": {
                        "type": "string",
                        "enum": ["APPLICABLE", "NOT_ESTABLISHED", "VERIFIER_ERROR"],
                    },
                    "reason": {"type": "string"},
                }
            )
        )
        schema["properties"]["application_checks"].update(
            minItems=len(numbered), maxItems=len(numbered)
        )
        schema["required"].append("application_checks")

    def validate(payload):
        errors = _validate_finding_receipt(
            {"publication": {"action": "NONE"}, **payload}
            if host_publication
            else payload
        )
        checks = payload.get("application_checks", [])
        if not payload["summary"].strip() or any(
            not row["reason"].strip() for row in checks
        ):
            errors.append(
                {
                    "reason_code": "missing_mathematical_reason",
                    "json_pointer": "/summary",
                }
            )
        if len(checks) != len(numbered) or {
            row["application"] for row in checks
        } != set(numbered):
            errors.append(
                {
                    "reason_code": "invalid_application_review",
                    "json_pointer": "/application_checks",
                }
            )
        if (
            not publication
            and payload.get("publication", {"action": "NONE"}).get("action") != "NONE"
        ):
            errors.append(
                {
                    "reason_code": "application_only_publication",
                    "json_pointer": "/publication",
                }
            )
        return errors

    def normalize(payload):
        if host_publication:
            payload = {**payload, "publication": {"action": "NONE"}}
        if not numbered:
            return payload
        checks = []
        for row in payload["application_checks"]:
            item = numbered[row["application"]]
            checks.append(
                {
                    **{
                        key: item[key]
                        for key in (
                            "application_key",
                            "obligation_id",
                            "obligation_version",
                        )
                    },
                    "status": row["status"],
                    "reuse_authorized": row["status"] == "APPLICABLE",
                    "review_ref": request["review_ref"],
                    "reason": row["reason"].strip(),
                }
            )
        return {**payload, "application_checks": checks}

    def accept(payload):
        verdict = normalize(payload)
        if numbered:
            from pace.artifacts.workspace import path_has_symlink_component

            review_path = workdir / request["review_ref"]
            if path_has_symlink_component(workdir, review_path):
                raise ValueError("unsafe application review path")
            summary = verdict["summary"].strip()
            decisions = [
                f"- **{row['obligation_id']} ({row['status']})**. {row['reason']}"
                for row in verdict["application_checks"]
            ]
            _write_text_atomic(
                review_path,
                "\n\n".join(["# Independent review", summary, "\n".join(decisions)])
                + "\n",
            )
            if validate_finding_application(verdict, request, workdir):
                raise ValueError(
                    "could not bind the compact review to its application scopes"
                )
        if on_accept:
            on_accept(verdict)

    # Limit read scope to the fixed evidence and related publication sources.
    result = run_semantic_role(
        llm,
        prompt,
        workdir=workdir,
        call_name=f"finding_verifier_{round_index:04d}_{Path(draft_ref).stem}",
        role=SemanticRoleSpec(
            role="finding_verifier",
            schema=lambda: schema,
            host_schema_version="finding_verification",
            authority="MathVerifierKernel",
            fatal_scope="CALL",
            validator=validate,
            on_accept=accept,
        ),
        input_refs=tuple(dict.fromkeys((draft_ref, *refs))),
        semantic_call_fn=semantic_call,
        normalizer=_normalize_finding_publication,
        host_metadata={
            "agent_id": "finding_verifier",
            "round_index": round_index,
            "draft_ref": draft_ref,
            "target_contract_id": target_id,
            "read_scope": "declared",
            "archive_refs": archive_refs,
            "purpose": "APPLICATION" if numbered else "PUBLICATION",
            **(
                {"application_keys": [item["application_key"] for item in items]}
                if numbered
                else {}
            ),
        },
    )
    if isinstance(result, StructuredSemanticResult):
        return StructuredSemanticResult(normalize(result.payload), result.host)
    return result


def _finding_receipt_schema(request=None):
    check = open_object(
        {
            "application_key": {"type": "string"},
            "obligation_id": {"type": "string"},
            "obligation_version": {"type": "string"},
            "status": {"type": "string", "enum": ["APPLICABLE", "NOT_ESTABLISHED", "VERIFIER_ERROR"]},
            "reuse_authorized": {"type": "boolean"},
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
            "reuse_authorized": {
                "type": "boolean",
                "description": (
                    "Optional manuscript-level authorization. Only true permits "
                    "standalone publication and reuse; omission leaves both unauthorized."
                ),
            },
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
            or check.get("status") not in {"APPLICABLE", "NOT_ESTABLISHED", "VERIFIER_ERROR"}
            or not isinstance(check.get("reuse_authorized"), bool)
            or check["reuse_authorized"] != (check["status"] == "APPLICABLE")
            or check.get("review_ref") != request["review_ref"]
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


def _normalize_finding_publication(payload):
    """Preserve a mathematical verdict that has no standalone authorization."""
    if (
        payload.get("stage_status") == "PASS"
        and "reuse_authorized" not in payload
        and _as_dict(payload.get("publication")).get("action") in ("CREATE", "UPDATE")
    ):
        return {
            **payload,
            "publication": {
                "action": "NONE",
                "reason": "No explicit manuscript-level reuse authorization.",
            },
        }
    return payload


def _validate_finding_receipt(payload):
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
