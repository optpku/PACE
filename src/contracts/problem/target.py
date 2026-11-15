from __future__ import annotations

from typing import Any

from pace.runtime.json_values import dict_rows as _rows

SCHEMA_VERSION = "target_contract/v5"


def validate_target_contract(value: Any, *, problem_text: str = "") -> tuple[dict[str, Any], list[str]]:
    """Check only the identity and literal seed needed to run a proof loop.

    Clause structure, line locations, and clause identifiers are model-owned
    context.  They are deliberately not a Host schema gate: the canonical
    problem and target text remain available to every mathematical stage.
    """

    del problem_text
    contract = value if isinstance(value, dict) else {}
    errors: list[str] = []
    clauses = _rows(contract.get("required_clauses"))
    if not clauses and not str(contract.get("literal_target") or "").strip():
        errors.append("literal_target")

    core = _contract_core(contract, clauses=clauses)
    if not str(core["problem_statement_id"] or "").strip():
        errors.append("problem_statement_id")
    elif str(contract.get("contract_id") or "") != target_contract_id(core):
        errors.append("contract_id")
    return contract, list(dict.fromkeys(errors))


def target_contract_text(value: Any) -> str:
    contract = value if isinstance(value, dict) else {}
    text = _literal_target_text(_rows(contract.get("required_clauses")))
    return text or _normalize_newlines(
        str(
            contract.get("literal_target")
            or contract.get("target_statement")
            or contract.get("statement")
            or ""
        ).strip()
    )


def target_contract_id(core: dict[str, Any]) -> str:
    """Return the run-local identity for one problem statement."""

    problem_id = str(core.get("problem_statement_id") or "").strip()
    if not problem_id:
        raise ValueError("target contract requires problem_statement_id")
    return f"TARGET-{problem_id}"


def _contract_core(contract: dict[str, Any], *, clauses: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "problem_statement_id": str(contract.get("problem_statement_id") or ""),
        "required_clauses": clauses,
    }


def _normalize_newlines(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _literal_target_text(clauses: list[dict[str, Any]]) -> str:
    spans = [
        _normalize_newlines(str(row.get("literal_span") or "").strip())
        for row in clauses
        if str(row.get("literal_span") or "").strip()
    ]
    return "\n\n".join(dict.fromkeys(spans))
