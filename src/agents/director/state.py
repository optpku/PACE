"""The Director's durable scheduling and continuation state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PureWindowsPath
from typing import Any

DIRECTOR_LED_STATE_SCHEMA_VERSION = "director_led_state/v2"


DIRECTOR_LED_ROUND_SCHEMA_VERSION = "director_led_round/v1"


DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION = "director_assignment/v1"


DIRECTOR_LED_VERIFICATION_FIELDS = frozenset(
    {
        "schema_version",
        "round_index",
        "specialist_result_ref",
        "verification",
    }
)


DIRECTOR_LED_VERIFIER_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "round_index",
        "specialist_result_ref",
        "candidate_ref",
        "directive_ref",
        "assignment_ref",
    }
)


DIRECTOR_LED_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "target_contract_id",
        "round_index",
        "max_proof_states",
        "proof_states_used",
        "phase",
        "pending_dispatch_ref",
        "pending_prompt_ref",
        "pending_assignment_ref",
        "pending_verifier_request_ref",
        "last_assignment_ref",
        "completed_result_refs",
        "last_directive_ref",
        "last_composer_manifest_ref",
        "last_specialist_result_ref",
        "last_verification_ref",
        "diagnostics_ref",
        "stop_reason",
        "terminal_result",
        "candidate_ref",
    }
)


DIRECTOR_LED_PHASES = frozenset(
    {
        "DIRECTOR",
        "PENDING_SPECIALIST",
        "PENDING_BATCH",
        "VERIFY",
        "FINALIZE",
        "STOPPED",
        "FAILED",
    }
)


class DirectorLedEngineError(RuntimeError):
    """Raised when the Host cannot continue a Director-led round."""

    def __init__(self, message: str, *, phase: str = "", round_index: int = 0) -> None:
        super().__init__(message)
        self.phase = str(phase or "")
        self.round_index = int(round_index or 0)


class DirectorBudgetExhausted(DirectorLedEngineError):
    """A scheduling refusal that the Director may resolve with another action."""


@dataclass(frozen=True, slots=True)
class DirectorLedState:
    """Minimal machine-owned continuation state.

    The state contains paths and scheduling fields only.  It intentionally
    does not contain a proof verdict or a semantic interpretation of any
    specialist output.
    """

    target_contract_id: str = ""
    round_index: int = 0
    max_proof_states: int = 18
    proof_states_used: int = 0
    phase: str = "DIRECTOR"
    pending_dispatch_ref: str = ""
    pending_prompt_ref: str = ""
    pending_assignment_ref: str = ""
    # Set only after the Host has committed the verifier request for the
    # current Prover handoff.  This is the recovery authorization marker for
    # an on-disk verification envelope.
    pending_verifier_request_ref: str = ""
    last_assignment_ref: str = ""
    completed_result_refs: tuple[str, ...] = ()
    last_directive_ref: str = ""
    last_composer_manifest_ref: str = ""
    last_specialist_result_ref: str = ""
    last_verification_ref: str = ""
    diagnostics_ref: str = ""
    stop_reason: str = ""
    terminal_result: str = ""
    candidate_ref: str = ""
    last_batch_result_ref: str = ""
    last_batch_result_sha256: str = ""

    def __post_init__(self) -> None:
        # Dataclass construction is also a machine-owned state boundary.  The
        # JSON loader validates these types before construction, but callers
        # can instantiate the frozen state directly (including finalization
        # and recovery adapters), so do not let Python coercion change an
        # identity or round invariant after the object has been accepted.
        if not isinstance(self.target_contract_id, str):
            raise ValueError("Director-led target_contract_id must be a string")
        if isinstance(self.round_index, bool) or not isinstance(self.round_index, int):
            raise ValueError("Director-led round_index must be an integer")
        if isinstance(self.max_proof_states, bool) or not isinstance(
            self.max_proof_states, int
        ):
            raise ValueError("Director-led max_proof_states must be an integer")
        string_fields = (
            "target_contract_id",
            "phase",
            "pending_dispatch_ref",
            "pending_prompt_ref",
            "pending_assignment_ref",
            "pending_verifier_request_ref",
            "last_assignment_ref",
            "last_directive_ref",
            "last_composer_manifest_ref",
            "last_specialist_result_ref",
            "last_verification_ref",
            "diagnostics_ref",
            "stop_reason",
            "terminal_result",
            "candidate_ref",
            "last_batch_result_ref",
            "last_batch_result_sha256",
        )
        for field_name in string_fields:
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"Director-led {field_name} must be a string")
            if value and not value.strip():
                raise ValueError(
                    f"Director-led {field_name} must not be whitespace-only"
                )
        for field_name in ("completed_result_refs",):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise ValueError(
                    f"Director-led {field_name} must be a tuple of non-empty strings"
                )
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
        if bool(self.last_batch_result_ref) != bool(self.last_batch_result_sha256):
            raise ValueError("batch result requires both path and digest")
        if self.phase not in DIRECTOR_LED_PHASES:
            raise ValueError(f"invalid Director-led phase: {self.phase}")
        if self.round_index < 0:
            raise ValueError("Director-led round_index must be non-negative")
        if self.max_proof_states < 1:
            raise ValueError("Director-led max_proof_states must be positive")
        if (
            type(self.proof_states_used) is not int
            or not 0 <= self.proof_states_used <= self.max_proof_states
        ):
            raise ValueError(
                "Director-led proof_states_used must be within the proof budget"
            )
        empty_final = (
            self.phase == "FINALIZE"
            and self.terminal_result == "FAILED"
            and self.stop_reason
            in {
                "USER_REQUESTED_FINALIZATION",
                "TOTAL_CALL_BUDGET_EXHAUSTED",
                "ACTIVE_TIME_BUDGET_EXHAUSTED",
            }
        )
        if self.round_index == 0 and self.phase != "DIRECTOR" and not empty_final:
            raise ValueError(
                "round zero requires a pristine Director checkpoint or an explicit pre-dispatch finalization"
            )
        if self.round_index == 0 and any(
            (
                self.pending_dispatch_ref,
                self.pending_prompt_ref,
                self.pending_assignment_ref,
                self.pending_verifier_request_ref,
                self.last_assignment_ref,
                self.completed_result_refs,
                self.last_batch_result_ref,
                self.last_directive_ref,
                self.last_composer_manifest_ref,
                self.last_specialist_result_ref,
                self.last_verification_ref,
                self.diagnostics_ref,
                self.stop_reason and not empty_final,
                self.terminal_result and not empty_final,
                self.candidate_ref,
            )
        ):
            raise ValueError(
                "Director-led round_index 0 must contain only the initial state"
            )
        if self.terminal_result not in {"", "PASS", "FAILED"}:
            raise ValueError("Director-led terminal_result must be PASS or FAILED")
        terminal_phases = {"FINALIZE", "STOPPED", "FAILED"}
        active_phases = {"DIRECTOR", "PENDING_SPECIALIST", "PENDING_BATCH", "VERIFY"}
        if self.phase in active_phases and self.terminal_result:
            raise ValueError("Director-led active state cannot carry a terminal_result")
        if self.phase == "FINALIZE" and not self.terminal_result:
            raise ValueError("Director-led FINALIZE state requires a terminal_result")
        if self.phase in {"STOPPED", "FAILED"} and self.terminal_result != "FAILED":
            raise ValueError(
                f"Director-led {self.phase} state must have terminal_result FAILED"
            )
        if self.terminal_result == "PASS" and self.phase != "FINALIZE":
            raise ValueError("Director-led PASS is only valid in the FINALIZE phase")
        if self.terminal_result == "FAILED" and self.phase not in terminal_phases:
            raise ValueError("Director-led FAILED result requires a terminal phase")

        if self.phase == "PENDING_BATCH" and not self.pending_assignment_ref:
            raise ValueError("PENDING_BATCH requires a committed batch manifest")
        if self.phase == "PENDING_SPECIALIST":
            if not self.pending_dispatch_ref or not self.pending_assignment_ref:
                raise ValueError(
                    "PENDING_SPECIALIST state requires dispatch and assignment refs"
                )
            if not self.last_directive_ref:
                raise ValueError("PENDING_SPECIALIST state requires last_directive_ref")
            if bool(self.pending_prompt_ref) != bool(self.last_composer_manifest_ref):
                raise ValueError(
                    "PENDING_SPECIALIST Composer refs must be committed together"
                )
            if self.pending_verifier_request_ref:
                raise ValueError(
                    "PENDING_SPECIALIST state cannot carry a verifier request ref"
                )
        if self.phase == "DIRECTOR" and any(
            (
                self.pending_dispatch_ref,
                self.pending_prompt_ref,
                self.pending_assignment_ref,
            )
        ):
            raise ValueError("DIRECTOR state cannot carry pending dispatch references")
        if self.phase == "VERIFY":
            missing_verify_refs = tuple(
                field
                for field, value in (
                    ("last_directive_ref", self.last_directive_ref),
                    ("last_assignment_ref", self.last_assignment_ref),
                    ("last_specialist_result_ref", self.last_specialist_result_ref),
                    ("candidate_ref", self.candidate_ref),
                )
                if not value
            )
            if missing_verify_refs:
                raise ValueError(
                    "VERIFY state requires committed refs: "
                    + ", ".join(missing_verify_refs)
                )
        if self.pending_verifier_request_ref and self.phase not in {"VERIFY", "FAILED"}:
            raise ValueError(
                "pending verifier request ref is only valid in VERIFY or FAILED phase"
            )
        if self.pending_verifier_request_ref and self.last_verification_ref:
            raise ValueError(
                "Director-led state cannot retain a pending verifier request "
                "after verification is committed"
            )
        if self.last_verification_ref and not self.last_specialist_result_ref:
            raise ValueError(
                "last verification ref requires a committed specialist result"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": DIRECTOR_LED_STATE_SCHEMA_VERSION,
            "target_contract_id": self.target_contract_id,
            "round_index": self.round_index,
            "max_proof_states": self.max_proof_states,
            "proof_states_used": self.proof_states_used,
            "phase": self.phase,
            "pending_dispatch_ref": self.pending_dispatch_ref,
            "pending_prompt_ref": self.pending_prompt_ref,
            "pending_assignment_ref": self.pending_assignment_ref,
            "pending_verifier_request_ref": self.pending_verifier_request_ref,
            "last_assignment_ref": self.last_assignment_ref,
            "completed_result_refs": list(self.completed_result_refs),
            "last_directive_ref": self.last_directive_ref,
            "last_composer_manifest_ref": self.last_composer_manifest_ref,
            "last_specialist_result_ref": self.last_specialist_result_ref,
            "last_verification_ref": self.last_verification_ref,
            "diagnostics_ref": self.diagnostics_ref,
            "stop_reason": self.stop_reason,
            "terminal_result": self.terminal_result,
            "candidate_ref": self.candidate_ref,
            **(
                {
                    "last_batch_result_ref": self.last_batch_result_ref,
                    "last_batch_result_sha256": self.last_batch_result_sha256,
                }
                if self.last_batch_result_ref
                else {}
            ),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "DirectorLedState":
        if not isinstance(value, Mapping):
            raise DirectorLedEngineError("Director-led state must be a JSON object")
        if (
            set(value).difference({"last_batch_result_ref", "last_batch_result_sha256"})
            != DIRECTOR_LED_STATE_FIELDS
        ):
            missing = sorted(DIRECTOR_LED_STATE_FIELDS.difference(value))
            extra = sorted(set(value).difference(DIRECTOR_LED_STATE_FIELDS))
            details: list[str] = []
            if missing:
                details.append(f"missing fields: {missing}")
            if extra:
                details.append(f"unexpected fields: {extra}")
            raise DirectorLedEngineError(
                "Director-led state fields are closed"
                + (" (" + "; ".join(details) + ")" if details else "")
            )
        if value.get("schema_version") != DIRECTOR_LED_STATE_SCHEMA_VERSION:
            raise DirectorLedEngineError("unsupported Director-led state schema")
        refs = _string_tuple(
            value.get("completed_result_refs"), "completed_result_refs", json_list=True
        )
        try:
            return cls(
                target_contract_id=_string_value(
                    value.get("target_contract_id", ""), "target_contract_id"
                ),
                round_index=_strict_int(value.get("round_index"), "round_index"),
                max_proof_states=_strict_int(
                    value.get("max_proof_states"), "max_proof_states"
                ),
                proof_states_used=_strict_int(
                    value.get("proof_states_used"), "proof_states_used"
                ),
                phase=_string_value(value.get("phase", "DIRECTOR"), "phase"),
                pending_dispatch_ref=_string_value(
                    value.get("pending_dispatch_ref", ""), "pending_dispatch_ref"
                ),
                pending_prompt_ref=_string_value(
                    value.get("pending_prompt_ref", ""), "pending_prompt_ref"
                ),
                pending_assignment_ref=_string_value(
                    value.get("pending_assignment_ref", ""), "pending_assignment_ref"
                ),
                pending_verifier_request_ref=_string_value(
                    value.get("pending_verifier_request_ref", ""),
                    "pending_verifier_request_ref",
                ),
                last_assignment_ref=_string_value(
                    value.get("last_assignment_ref", ""), "last_assignment_ref"
                ),
                completed_result_refs=refs,
                last_directive_ref=_string_value(
                    value.get("last_directive_ref", ""), "last_directive_ref"
                ),
                last_composer_manifest_ref=_string_value(
                    value.get("last_composer_manifest_ref", ""),
                    "last_composer_manifest_ref",
                ),
                last_specialist_result_ref=_string_value(
                    value.get("last_specialist_result_ref", ""),
                    "last_specialist_result_ref",
                ),
                last_verification_ref=_string_value(
                    value.get("last_verification_ref", ""), "last_verification_ref"
                ),
                diagnostics_ref=_string_value(
                    value.get("diagnostics_ref", ""), "diagnostics_ref"
                ),
                stop_reason=_string_value(value.get("stop_reason", ""), "stop_reason"),
                terminal_result=_string_value(
                    value.get("terminal_result", ""), "terminal_result"
                ),
                candidate_ref=_string_value(
                    value.get("candidate_ref", ""), "candidate_ref"
                ),
                last_batch_result_sha256=_string_value(
                    value.get("last_batch_result_sha256", ""),
                    "last_batch_result_sha256",
                ),
                last_batch_result_ref=_string_value(
                    value.get("last_batch_result_ref", ""), "last_batch_result_ref"
                ),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, DirectorLedEngineError):
                raise
            message = str(exc).strip() or "invalid Director-led state values"
            raise DirectorLedEngineError(message) from exc


@dataclass(frozen=True, slots=True)
class DirectorLedRunResult:
    """Return value for one engine invocation."""

    state: DirectorLedState
    state_ref: str
    completed_result_refs: tuple[str, ...] = dataclass_field(default_factory=tuple)

    @property
    def phase(self) -> str:
        return self.state.phase

    @property
    def result(self) -> str:
        return str(self.state.terminal_result or "")


def _string_value(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DirectorLedEngineError(f"state field {field} must be a string")
    if value and not value.strip():
        raise DirectorLedEngineError(f"state field {field} must not be whitespace-only")
    return value


def _string_tuple(
    value: Any,
    field: str,
    *,
    json_list: bool = False,
) -> tuple[str, ...]:
    allowed_types = (list,) if json_list else (list, tuple)
    if not isinstance(value, allowed_types) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DirectorLedEngineError(
            f"state field {field} must be a list of non-empty strings"
        )
    if len(set(value)) != len(value):
        raise DirectorLedEngineError(f"state field {field} must be unique")
    return tuple(value)


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DirectorLedEngineError(f"state field {field} must be an integer")
    return value


def _safe_relative_ref(value: str | None, *, default: str) -> str:
    raw = str(value or default).replace("\\", "/").strip()
    if _unsafe_ref_syntax(raw):
        raise ValueError("artifact references must be relative to the engine workdir")
    return "/".join(part for part in raw.split("/") if part) or default


def _unsafe_ref_syntax(value: str) -> bool:
    """Return whether a reference is non-canonical or not relative."""

    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return True
    return bool(
        Path(raw).is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or any(part in {".", ".."} for part in raw.split("/"))
    )
