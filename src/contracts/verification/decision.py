from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from pace.contracts.research.memory import digest
from pace.runtime.json_values import (
    as_dict as _as_dict,
)
from pace.runtime.json_values import (
    one_or_many_dict_rows as _rows,
)

COMPACT_KERNEL_VERSION = "math_verifier_kernel/v18"


def missing_kernel_evidence(kernel: Mapping[str, Any], *, compact=None) -> list[str]:
    """Check evidence delivery while leaving its mathematical content open."""

    if compact is None:
        compact = kernel.get("schema_version") == COMPACT_KERNEL_VERSION
    if compact:
        return [
            field
            for field in ("summary", "proof_location")
            if not isinstance(kernel.get(field), str) or not kernel[field].strip()
        ]
    return [
        field
        for field in ("relation_checks", "target_clause_checks", "final_assembly")
        if not isinstance(kernel.get(field), (dict, list))
        or not _has_evidence_content(kernel.get(field))
    ]


def _has_evidence_content(value: Any) -> bool:
    """Reject empty nested placeholders without prescribing mathematical fields."""

    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_evidence_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_evidence_content(item) for item in value)
    # Numeric and boolean values are supplied content. Interpreting their
    # sufficiency would turn this delivery check into a mathematical schema.
    return value is not None


def kernel_publication_eligible(kernel: Mapping[str, Any]) -> bool:
    """Read the Kernel's explicit approval and the minimum publication inputs."""

    return (
        str(kernel.get("stage_status") or "").upper() == "PASS"
        and kernel.get("publication_eligible") is True
        and not missing_kernel_evidence(kernel)
        and not any(
            kernel.get(field)
            for field in ("canonical_blocker", "canonical_blockers", "fatal_blockers")
        )
    )


def requires_scope_repair(auditor: Mapping[str, Any]) -> bool:
    """An explicit, evidenced coverage failure can be returned to the author."""
    violations = auditor.get("blocking_violations")
    return (
        auditor.get("contract_status") == "FAIL"
        and auditor.get("return_for_scope_repair") is True
        and not auditor.get("kernel_hints")
        and isinstance(violations, list)
        and any(
            isinstance(row, dict)
            and all(
                isinstance(row.get(key), str) and row[key].strip()
                for key in ("requirement", "candidate_evidence")
            )
            for row in violations
        )
    )


def decide_final_result(
    *, auditor: dict[str, Any], kernel: dict[str, Any]
) -> dict[str, Any]:
    """Combine explicit verdicts without rewriting either mathematical report."""

    audit_status = str(auditor.get("contract_status") or "").upper()
    kernel_status = str(kernel.get("stage_status") or "").upper()
    interface_failures = []
    for stage, report, field, allowed, blocker_fields in (
        (
            "ProofContractAuditor",
            auditor,
            "contract_status",
            {"PASS", "FAIL"},
            ("blocking_violations", "canonical_blockers"),
        ),
        (
            "MathVerifierKernel",
            kernel,
            "stage_status",
            {"PASS", "MATH_FAIL"},
            ("canonical_blocker", "canonical_blockers", "fatal_blockers"),
        ),
    ):
        status = str(report.get(field) or "").upper()
        failure = {}
        if (
            field == "stage_status"
            and status == "NOT_RUN"
            and kernel.get("authority") == "Host"
            and requires_scope_repair(auditor)
        ):
            continue
        if status not in allowed:
            failure = _as_dict(report.get("interface_failure")) or {
                "reason": "verifier_interface_error"
                if status == "VERIFIER_ERROR"
                else "invalid_verifier_status",
                "status_field": field,
                "reported_status": report.get(field),
            }
        elif (
            field == "stage_status"
            and kernel.get("schema_version") == COMPACT_KERNEL_VERSION
            and (problems := validate_compact_kernel(kernel))
        ):
            failure = {"reason": problems[0]["reason_code"], "diagnostics": problems}
        elif status == "PASS" and any(report.get(name) for name in blocker_fields):
            failure = {"reason": "verifier_report_inconsistent", "status_field": field}
        elif field == "stage_status" and status == "PASS":
            missing = missing_kernel_evidence(kernel)
            if missing:
                failure = {
                    "reason": "kernel_publication_evidence_missing",
                    "diagnostics": [
                        {
                            "reason_code": "kernel_publication_evidence_missing",
                            "missing": missing,
                        }
                    ],
                }
            elif kernel.get("publication_eligible") is not True:
                failure = {"reason": "kernel_publication_not_authorized"}
        if failure:
            interface_failures.append(
                {
                    "stage": stage,
                    "interface_failure": failure,
                    "semantic_failure": _as_dict(report.get("semantic_failure")),
                }
            )

    # Blockers are optional explanations of explicit rejection. Keep every
    # supplied row as written; the report refs also retain unstructured evidence.
    blockers = []
    if kernel_status == "MATH_FAIL":
        blockers.extend(
            _rows(kernel.get("canonical_blocker"))
            + _rows(kernel.get("canonical_blockers"))
            + _rows(kernel.get("fatal_blockers"))
        )
    if audit_status == "FAIL":
        blockers.extend(
            _rows(auditor.get("canonical_blockers"))
            + _rows(auditor.get("blocking_violations"))
        )
    mathematical_failure = audit_status == "FAIL" or kernel_status == "MATH_FAIL"
    passed = (
        audit_status == "PASS"
        and kernel_publication_eligible(kernel)
        and not interface_failures
    )
    if passed:
        owner = ""
        mathematical_status = "VERIFIED"
    elif mathematical_failure:
        owner = "PROOF"
        mathematical_status = "MATH_FAIL"
    else:
        owner = "INTERFACE"
        mathematical_status = "UNAVAILABLE"
    result = {
        "schema_version": "final_result/v3",
        "result": "PASS" if passed else "FAILED",
        "mathematical_status": mathematical_status,
        "publication_status": "PUBLISHABLE" if passed else "DRAFT",
        "failure_owner": owner,
        "canonical_blockers": _dedupe_result_rows(blockers),
        "proof_contract_status": audit_status,
        "kernel_status": kernel_status,
        "verification_interface_failures": interface_failures,
        "answer_mode": str(auditor.get("answer_mode") or "UNCLEAR"),
    }
    if owner == "INTERFACE":
        result["interface_failure"] = interface_failures[0]["interface_failure"]
    return result


def verified_relations(
    kernel: dict[str, Any], *, proof_id: str
) -> list[dict[str, Any]]:
    # Relation bodies are Kernel-owned evidence.  The loop only needs to know
    # which rows the Kernel marked PASS; it must not require statement text,
    # proof spans, source status, or a closed dependency graph here.
    compact = kernel.get("schema_version") == COMPACT_KERNEL_VERSION
    rows = []
    for row in _rows(kernel.get("relation_checks")):
        if str(row.get("status") or "").upper() != "PASS":
            continue
        if compact:
            if _invalid_relation_fields(row):
                continue
            row = {**row, "relation_id": compact_relation_id(row)}
        rows.append({**row, "proof_id": str(row.get("proof_id") or proof_id or "")})
    return rows


def compact_relation_id(row: dict[str, Any]) -> str:
    """Identify the scoped statement independently of its location in a draft."""
    components = {value.strip() for value in row["allowed_success_contract_components"]}
    return digest(
        {
            "statement": row["statement"].strip(),
            "conditions": row["conditions"].strip(),
            "components": sorted(components),
        }
    )


def _dedupe_result_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in rows:
        if row not in values:
            values.append(row)
    return values


def validate_publication(
    payload: dict[str, Any], *, current_finding_refs: Collection[str] | None = None
) -> list[dict[str, Any]]:
    if payload.get("stage_status") != "PASS":
        return []
    decision = payload.get("publication")
    if (
        not isinstance(decision, dict)
        or decision.get("action") not in ("CREATE", "UPDATE", "NONE")
        or (
            decision["action"] == "UPDATE"
            and not isinstance(decision.get("finding_ref"), str)
        )
        or (decision["action"] == "UPDATE" and not decision["finding_ref"].strip())
    ):
        return [
            {
                "stage": "finding_publication",
                "reason_code": "finding_publication_decision_missing",
                "json_pointer": "/publication",
                "message": "Explicitly choose CREATE, UPDATE with a current finding_ref, or NONE independently of mathematical PASS.",
            }
        ]
    if (
        decision["action"] == "UPDATE"
        and current_finding_refs is not None
        and decision["finding_ref"] not in current_finding_refs
    ):
        return [
            {
                "stage": "finding_publication",
                "reason_code": "finding_publication_unknown_update",
                "json_pointer": "/publication/finding_ref",
                "message": (
                    "UPDATE requires an exact current finding_ref from the findings catalogue."
                    if current_finding_refs
                    else "No current findings can be updated. Choose CREATE or NONE."
                ),
            }
        ]
    return []


def validate_compact_kernel(
    report: dict[str, Any], *, current_finding_refs: Collection[str] | None = None
) -> list[dict[str, Any]]:
    missing = missing_kernel_evidence(report, compact=True)
    errors = [
        {"json_pointer": "/" + key, "reason_code": "missing_kernel_judgment"}
        for key in missing
    ]
    if report.get("stage_status") == "PASS":
        errors.extend(
            validate_publication(report, current_finding_refs=current_finding_refs)
        )
        if report.get("publication_eligible") is not True:
            errors.append(
                {
                    "json_pointer": "/publication_eligible",
                    "reason_code": "kernel_publication_not_authorized",
                }
            )
    elif report.get("publication_eligible") or _as_dict(report.get("publication")).get(
        "action"
    ) in {"CREATE", "UPDATE"}:
        errors.append(
            {
                "json_pointer": "/publication",
                "reason_code": "failed_candidate_cannot_publish",
            }
        )
    if "relation_checks" in report:
        relations = report["relation_checks"]
        if not isinstance(relations, list):
            errors.append(
                {
                    "json_pointer": "/relation_checks",
                    "reason_code": "invalid_reusable_relation",
                    "message": "Use a list of scoped reusable relations or omit the field.",
                }
            )
        else:
            for index, row in enumerate(relations):
                fields = _invalid_relation_fields(row) if isinstance(row, dict) else [""]
                for field in fields:
                    pointer = f"/relation_checks/{index}" + (f"/{field}" if field else "")
                    errors.append(
                        {
                            "json_pointer": pointer,
                            "reason_code": "invalid_reusable_relation",
                            "message": "A reusable relation requires nonempty scoped evidence and explicit authorization.",
                        }
                    )
    return errors


def _invalid_relation_fields(row: dict[str, Any]) -> list[str]:
    def nonblank(value):
        return isinstance(value, str) and bool(value.strip())

    invalid = [
        field for field in ("statement", "conditions") if not nonblank(row.get(field))
    ]
    for field in ("supports", "allowed_success_contract_components"):
        values = row.get(field)
        if not isinstance(values, list) or not values or not all(map(nonblank, values)):
            invalid.append(field)
    if row.get("status") != "PASS":
        invalid.append("status")
    if row.get("reuse_authorized") is not True:
        invalid.append("reuse_authorized")
    return invalid


def without_invalid_compact_exports(report: dict[str, Any]) -> dict[str, Any]:
    """Keep the whole-proof judgment and only explicitly authorized local exports."""
    cleaned = dict(report)
    if "relation_checks" in report:
        relations = report["relation_checks"]
        cleaned["relation_checks"] = (
            [
                row
                for row in relations
                if isinstance(row, dict) and not _invalid_relation_fields(row)
            ]
            if isinstance(relations, list)
            else []
        )
    if report.get("stage_status") == "MATH_FAIL":
        cleaned.pop("publication", None)
        cleaned.pop("publication_eligible", None)
    return cleaned
