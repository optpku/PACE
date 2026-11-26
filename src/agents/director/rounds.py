"""Check the exact assignment, prompt and verifier receipts for one round."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pace.agents.director.state import (
    DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
    DIRECTOR_LED_ROUND_SCHEMA_VERSION,
    DIRECTOR_LED_VERIFICATION_FIELDS,
    DIRECTOR_LED_VERIFIER_REQUEST_FIELDS,
    DirectorLedEngineError,
    _unsafe_ref_syntax,
)
from pace.agents.prompt_composer import PromptComposerResult
from pace.artifacts.workspace import is_call_path
from pace.contracts.research.director_protocol import (
    DIRECTOR_FIELDS,
    DIRECTOR_PROTOCOL_SCHEMA_VERSION,
    DirectorAction,
    DirectorDecision,
    DirectorProtocolError,
    parse_director_decision,
    specialist_spec,
)


def _coerce_decision(value: Any) -> DirectorDecision:
    if isinstance(value, DirectorDecision):
        decision = value
        # Re-run Host checks against the current public snapshot even when a
        # collaborator constructed a dataclass directly.
        from pace.contracts.research.director_protocol import (
            validate_director_decision,
        )

        validate_director_decision(decision)
        return decision
    if hasattr(value, "payload") and isinstance(value.payload, Mapping):
        value = value.payload
    if isinstance(value, Mapping) and isinstance(value.get("decision"), Mapping):
        value = value["decision"]
    if not isinstance(value, Mapping):
        raise DirectorProtocolError("Director handler must return a decision object")
    return parse_director_decision(value)


def _coerce_persisted_decision(
    value: Any,
    *,
    expected_round_index: int | None = None,
) -> DirectorDecision:
    """Read a Host-persisted decision envelope without weakening the protocol.

    ``director_decision.json`` carries the protocol fields alongside the
    Host-owned schema and round metadata.  The semantic protocol itself is a
    closed object, so those envelope fields must be removed explicitly before
    calling :func:`parse_director_decision`.  This helper is deliberately kept
    separate from ``_coerce_decision``: a Director response containing random
    extra fields must still be rejected rather than silently projected.
    """

    if not isinstance(value, Mapping):
        raise DirectorProtocolError("persisted director decision must be a JSON object")
    from pace.contracts.research.director_protocol import (
        MEMORY_DIRECTOR_FIELDS,
        MEMORY_DIRECTOR_SCHEMA_VERSION,
    )

    version = value.get("schema_version")
    fields = set(DIRECTOR_FIELDS) | (
        MEMORY_DIRECTOR_FIELDS if version == MEMORY_DIRECTOR_SCHEMA_VERSION else set()
    )
    if "stop_reason" in value:
        fields.add("stop_reason")
    expected_fields = fields | {"schema_version", "round_index"}
    actual_fields = set(value)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields.difference(actual_fields))
        extra = sorted(actual_fields.difference(expected_fields))
        details = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorProtocolError(
            "persisted director decision envelope is closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )
    if version not in {
        DIRECTOR_PROTOCOL_SCHEMA_VERSION,
        MEMORY_DIRECTOR_SCHEMA_VERSION,
    }:
        raise DirectorProtocolError(
            "persisted director decision has an unsupported schema_version"
        )
    round_index = value.get("round_index")
    if isinstance(round_index, bool) or not isinstance(round_index, int):
        raise DirectorProtocolError(
            "persisted director decision round_index must be an integer"
        )
    if expected_round_index is not None and round_index != int(expected_round_index):
        raise DirectorProtocolError(
            "persisted director decision belongs to a different round"
        )
    payload = {field: value[field] for field in fields}
    return parse_director_decision(payload)


def _validate_assignment(
    value: Any,
    *,
    decision: DirectorDecision,
    assignment_ref: str,
    directive_ref: str,
    round_index: int,
    expected_output_ref: str,
    expected_input_refs: tuple[str, ...],
) -> None:
    """Validate the Host-owned dispatch checkpoint against its decision.

    An assignment is a transport binding, not a second mathematical
    protocol.  The Host therefore checks only the fields needed to resume the
    exact dispatch: schema/round identity, the selected specialist, selected
    reading refs plus Host input bindings, the directive reference, and output.
    Keeping
    this envelope closed prevents a hand-edited checkpoint from changing the
    dispatch while leaving the Director's semantic brief opaque.
    """

    if not isinstance(value, Mapping):
        raise DirectorLedEngineError("Director assignment must be a JSON object")

    fields = {
        "schema_version",
        "round_index",
        "agent_id",
        "prompt_brief",
        "input_refs",
        "output_ref",
        "director_decision_ref",
    }
    actual_fields = set(value)
    if not fields.issubset(actual_fields) or actual_fields - fields - {
        "assembly_source",
        "context_binding",
    }:
        missing = sorted(fields.difference(actual_fields))
        extra = sorted(actual_fields.difference(fields))
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorLedEngineError(
            "Director assignment fields are closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )

    if value.get("schema_version") != DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION:
        raise DirectorLedEngineError("unsupported Director assignment schema")
    persisted_round = value.get("round_index")
    if isinstance(persisted_round, bool) or not isinstance(persisted_round, int):
        raise DirectorLedEngineError(
            "Director assignment round_index must be an integer"
        )
    if persisted_round != int(round_index):
        raise DirectorLedEngineError("Director assignment belongs to a different round")

    if (
        not isinstance(decision, DirectorDecision)
        or decision.action is not DirectorAction.DISPATCH
    ):
        raise DirectorLedEngineError(
            "assignment validation requires a DISPATCH decision"
        )

    agent_id = value.get("agent_id")
    if not isinstance(agent_id, str) or agent_id != decision.agent_id:
        raise DirectorLedEngineError(
            "Director assignment agent_id differs from decision"
        )
    # Resolve the registry entry here as well as when the decision is parsed;
    # this keeps a persisted assignment independently actionable on resume.
    specialist_spec(agent_id)

    prompt_brief = value.get("prompt_brief")
    if not isinstance(prompt_brief, str) or prompt_brief != decision.prompt_brief:
        raise DirectorLedEngineError(
            "Director assignment prompt_brief differs from decision"
        )

    for field_name, expected in (("input_refs", expected_input_refs),):
        persisted = value.get(field_name)
        if (
            not isinstance(persisted, list)
            or any(not isinstance(item, str) or not item.strip() for item in persisted)
            or len(set(persisted)) != len(persisted)
            or tuple(persisted) != expected
        ):
            raise DirectorLedEngineError(
                f"Director assignment {field_name} differs from Host binding"
            )

    output_ref = value.get("output_ref")
    if not isinstance(output_ref, str) or not output_ref.strip():
        raise DirectorLedEngineError("Director assignment output_ref must be non-empty")
    if not isinstance(expected_output_ref, str) or not expected_output_ref.strip():
        raise DirectorLedEngineError("Host has no expected assignment output_ref")
    if output_ref != expected_output_ref:
        raise DirectorLedEngineError(
            "Director assignment output_ref differs from Host binding"
        )

    persisted_directive_ref = value.get("director_decision_ref")
    if (
        not isinstance(persisted_directive_ref, str)
        or persisted_directive_ref != directive_ref
    ):
        raise DirectorLedEngineError(
            "Director assignment director_decision_ref differs from committed directive"
        )

    # Both checkpoint references must identify the same round-local files.
    # This also rejects an absolute or parent-traversing state reference before
    # it can be used as a continuation binding.
    for ref, field_name in (
        (assignment_ref, "assignment_ref"),
        (directive_ref, "director_decision_ref"),
        (output_ref, "output_ref"),
    ):
        raw = str(ref or "").replace("\\", "/").strip()
        path = Path(raw)
        if (
            _unsafe_ref_syntax(raw)
            or is_call_path(path)
            or path.name.startswith("private_")
        ):
            raise DirectorLedEngineError(
                f"Director assignment {field_name} must be a public relative ref"
            )

    assignment_path = Path(str(assignment_ref).replace("\\", "/"))
    directive_path = Path(str(directive_ref).replace("\\", "/"))
    expected_assignment = directive_path.parent / "assignment" / "task.json"
    if assignment_path.as_posix() != expected_assignment.as_posix():
        raise DirectorLedEngineError(
            "Director assignment ref is not bound to its directive round"
        )


def _validate_verification_checkpoint(
    value: Any,
    *,
    expected_round_index: int,
    expected_specialist_result_ref: str,
) -> Mapping[str, Any]:
    """Validate the Host-owned envelope around one verifier handoff.

    The reference-only verifier receipt and surrounding checkpoint are
    machine-owned continuation bindings.  Its
    identity must therefore be closed and tied to the exact Prover result that
    entered this round; otherwise a recovered process could pair a valid
    verdict with a different specialist artifact.
    """

    if not isinstance(value, Mapping):
        raise DirectorLedEngineError(
            "Director-led verification checkpoint must be a JSON object"
        )
    actual_fields = set(value)
    if actual_fields != DIRECTOR_LED_VERIFICATION_FIELDS:
        missing = sorted(DIRECTOR_LED_VERIFICATION_FIELDS.difference(actual_fields))
        extra = sorted(actual_fields.difference(DIRECTOR_LED_VERIFICATION_FIELDS))
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorLedEngineError(
            "Director-led verification checkpoint fields are closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )
    if value.get("schema_version") != DIRECTOR_LED_ROUND_SCHEMA_VERSION:
        raise DirectorLedEngineError(
            "unsupported Director-led verification checkpoint schema"
        )
    persisted_round = value.get("round_index")
    if isinstance(persisted_round, bool) or not isinstance(persisted_round, int):
        raise DirectorLedEngineError(
            "Director-led verification checkpoint round_index must be an integer"
        )
    if persisted_round != int(expected_round_index):
        raise DirectorLedEngineError(
            "Director-led verification checkpoint belongs to a different round"
        )
    expected_result_ref = str(expected_specialist_result_ref or "").strip()
    persisted_result_ref = value.get("specialist_result_ref")
    if not expected_result_ref:
        raise DirectorLedEngineError(
            "Director-led verification checkpoint has no committed specialist result"
        )
    if (
        not isinstance(persisted_result_ref, str)
        or not persisted_result_ref.strip()
        or persisted_result_ref != expected_result_ref
    ):
        raise DirectorLedEngineError(
            "verification checkpoint is bound to a different specialist result"
        )
    verification = value.get("verification")
    if not isinstance(verification, Mapping):
        raise DirectorLedEngineError(
            "Director-led verification checkpoint has no object verifier handoff"
        )
    return verification


def _validate_verifier_request(
    value: Any,
    *,
    expected_round_index: int,
    expected_specialist_result_ref: str,
    expected_candidate_ref: str,
    expected_directive_ref: str,
    expected_assignment_ref: str,
) -> None:
    """Validate the Host marker that authorizes verification recovery.

    The marker is written only after a successful Prover handoff and immediately
    before invoking the verifier.  It is not mathematical evidence; it merely
    distinguishes a verifier checkpoint produced by this Host transaction from
    a file a specialist may have placed at the canonical path prematurely.
    """

    if not isinstance(value, Mapping):
        raise DirectorLedEngineError(
            "Director-led verifier request must be a JSON object"
        )
    actual_fields = set(value)
    if actual_fields != DIRECTOR_LED_VERIFIER_REQUEST_FIELDS:
        missing = sorted(DIRECTOR_LED_VERIFIER_REQUEST_FIELDS.difference(actual_fields))
        extra = sorted(actual_fields.difference(DIRECTOR_LED_VERIFIER_REQUEST_FIELDS))
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorLedEngineError(
            "Director-led verifier request fields are closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )
    if value.get("schema_version") != DIRECTOR_LED_ROUND_SCHEMA_VERSION:
        raise DirectorLedEngineError("unsupported Director-led verifier request schema")
    persisted_round = value.get("round_index")
    if isinstance(persisted_round, bool) or not isinstance(persisted_round, int):
        raise DirectorLedEngineError(
            "Director-led verifier request round_index must be an integer"
        )
    if persisted_round != int(expected_round_index):
        raise DirectorLedEngineError(
            "Director-led verifier request belongs to a different round"
        )
    expected_values = {
        "specialist_result_ref": expected_specialist_result_ref,
        "candidate_ref": expected_candidate_ref,
        "directive_ref": expected_directive_ref,
        "assignment_ref": expected_assignment_ref,
    }
    for field_name, expected in expected_values.items():
        observed = value.get(field_name)
        if (
            not isinstance(observed, str)
            or not observed.strip()
            or observed != str(expected or "")
        ):
            raise DirectorLedEngineError(
                f"verifier request {field_name} differs from the committed dispatch"
            )


def _validate_composer_manifest(
    value: Mapping[str, Any],
    *,
    decision: DirectorDecision,
    round_index: int,
    prompt_ref: str,
    output_ref: str,
    directive_ref: str,
) -> None:
    """Check that a recovered Composer manifest names this exact dispatch."""

    fields = {
        "schema_version",
        "round_index",
        "agent_id",
        "final_prompt_ref",
        "director_decision_ref",
        "output_artifact_ref",
    }
    actual_fields = set(value)
    optional = {"assembly_source", "composer_call_id"}
    if not fields.issubset(actual_fields) or actual_fields - fields - optional:
        missing = sorted(fields.difference(actual_fields))
        extra = sorted(actual_fields - fields - optional)
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected fields: {extra}")
        raise DirectorLedEngineError(
            "Composer manifest fields are closed"
            + (" (" + "; ".join(details) + ")" if details else "")
        )
    if value.get("schema_version") != DIRECTOR_LED_ROUND_SCHEMA_VERSION:
        raise DirectorLedEngineError("unsupported Composer manifest schema")
    persisted_round = value.get("round_index")
    if isinstance(persisted_round, bool) or not isinstance(persisted_round, int):
        raise DirectorLedEngineError("Composer manifest round_index must be an integer")
    if persisted_round != int(round_index):
        raise DirectorLedEngineError("Composer manifest belongs to a different round")
    if value.get("agent_id") != decision.agent_id:
        raise DirectorLedEngineError("Composer manifest agent_id differs from decision")
    if value.get("final_prompt_ref") != prompt_ref:
        raise DirectorLedEngineError("Composer manifest names a different final prompt")
    if value.get("director_decision_ref") != directive_ref:
        raise DirectorLedEngineError("Composer manifest names a different directive")
    if value.get("output_artifact_ref") != output_ref:
        raise DirectorLedEngineError(
            "Composer manifest names a different output artifact"
        )


def _validate_composer_prompt(value: Any, *, source: str) -> str:
    """Require usable prompt bytes without interpreting their contents."""

    if not isinstance(value, str) or not value.strip():
        raise DirectorLedEngineError(f"{source} prompt is empty")
    return value


def _coerce_composer(value: Any) -> PromptComposerResult:
    if not isinstance(value, PromptComposerResult):
        raise DirectorLedEngineError("Composer must return PromptComposerResult")
    return value


def _specialist_handoff(value: Any, agent_id: str) -> tuple[dict[str, Any], str]:
    """Validate the current handoff before committing it or scheduling verification."""
    if not isinstance(value, Mapping):
        raise DirectorLedEngineError("specialist handoff must be a JSON object")
    _validate_specialist_result_agent_id(value, expected_agent_id=agent_id)
    if "proof_ref" in value or "candidate_proof_ref" in value:
        raise DirectorLedEngineError(
            "specialist handoff must use deliverable_ref for its artifact"
        )
    raw_status = value.get("status")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise DirectorLedEngineError("specialist handoff requires an explicit status")
    status = raw_status.strip().upper()
    record = {**value, "status": status}
    if status != "SUCCEEDED":
        return record, ""
    ref = record.get("deliverable_ref")
    if not isinstance(ref, str) or not ref.strip():
        raise DirectorLedEngineError(
            "successful specialist handoff requires an explicit deliverable_ref"
        )
    return record, ref.strip()


def _is_success_status(value: Any) -> bool:
    """Return whether a specialist handoff explicitly completed successfully."""

    return str(value or "").strip().upper() == "SUCCEEDED"


def _invoke(handler: Callable[..., Any] | None, kwargs: Mapping[str, Any]) -> Any:
    if handler is None:
        raise DirectorLedEngineError("callback is not configured")
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(**dict(kwargs))
    parameters = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return handler(**dict(kwargs))
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return handler(**accepted)


def _is_private_transport_path(path: Path) -> bool:
    """Return whether a canonical relative path names hidden transport data."""

    return is_call_path(path) or path.name.startswith("private_")


def _validate_specialist_result_agent_id(
    result_record: Mapping[str, Any],
    *,
    expected_agent_id: str,
) -> None:
    """Require the handoff to identify the specialist that owns the assignment."""
    if result_record.get("agent_id") != expected_agent_id:
        raise DirectorLedEngineError(
            "specialist result agent_id differs from dispatched agent"
        )


def _unique_strings(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value
