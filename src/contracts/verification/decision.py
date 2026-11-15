from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pace.runtime.json_values import (
    as_dict as _as_dict,
)
from pace.runtime.json_values import (
    one_or_many_dict_rows as _rows,
)


def missing_kernel_evidence(kernel: Mapping[str, Any]) -> list[str]:
    """Check evidence delivery while leaving its mathematical content open."""

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


def decide_final_result(
    *, auditor: dict[str, Any], kernel: dict[str, Any]
) -> dict[str, Any]:
    """Combine explicit verdicts without rewriting either mathematical report."""

    audit_status = str(auditor.get("contract_status") or "").upper()
    kernel_status = str(kernel.get("stage_status") or "").upper()
    interface_failures = []
    for stage, report, field, allowed, blocker_fields in (
        (
            "ProofContractAuditor", auditor, "contract_status", {"PASS", "FAIL"},
            ("blocking_violations", "canonical_blockers"),
        ),
        (
            "MathVerifierKernel", kernel, "stage_status", {"PASS", "MATH_FAIL"},
            ("canonical_blocker", "canonical_blockers", "fatal_blockers"),
        ),
    ):
        status = str(report.get(field) or "").upper()
        failure = {}
        if status not in allowed:
            failure = _as_dict(report.get("interface_failure")) or {
                "reason": "verifier_interface_error" if status == "VERIFIER_ERROR" else "invalid_verifier_status",
                "status_field": field,
                "reported_status": report.get(field),
            }
        elif status == "PASS" and any(report.get(name) for name in blocker_fields):
            failure = {"reason": "verifier_report_inconsistent", "status_field": field}
        elif field == "stage_status" and status == "PASS":
            missing = missing_kernel_evidence(kernel)
            if missing:
                failure = {
                    "reason": "kernel_publication_evidence_missing",
                    "diagnostics": [{
                        "reason_code": "kernel_publication_evidence_missing", "missing": missing,
                    }],
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
    return [
        {
            **dict(row),
            "proof_id": str(row.get("proof_id") or proof_id or ""),
        }
        for row in _rows(kernel.get("relation_checks"))
        if isinstance(row, dict)
        and str(row.get("status") or "").upper() == "PASS"
    ]


def _dedupe_result_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in rows:
        if row not in values:
            values.append(row)
    return values
