"""Host-owned production loop for Director-led mathematical research.

A Director schedules bounded specialist work. Prompt Composer prepares ordinary
mathematical tasks, while SMI owns its internal phase prompts. The Host executes
those assignments and persists their artifacts and exact continuation state.
Mathematical agents own reasoning; Auditor and Kernel own independent verdicts.
History and method guides remain search context for subsequent rounds.

Injected callables keep scheduling independent of the concrete LLM transport.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field as dataclass_field, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.contracts.research.director_protocol import (
    DIRECTOR_FIELDS,
    DIRECTOR_PROTOCOL_SCHEMA_VERSION,
    DISPATCHABLE_AGENT_IDS,
    DirectorAction,
    DirectorDecision,
    DirectorProtocolError,
    parse_director_decision,
    specialist_spec,
)
from pace.contracts.proof.binding import resolve_verification
from pace.orchestrator.director_led_adapters import (
    build_specialist_prompt_assembly,
)
from pace.runtime.context_state import (
    commit_verified_progress,
    snapshot_verified_progress,
)
from pace.runtime.output.events import publish_progress
from pace.runtime.output.delivery_pause import DeliveryPaused
from pace.runtime.output.memory import commit_memory_notes
from pace.runtime.output.results import FINDINGS_INDEX_REF, stage_finding_review_refs
from pace.runtime.output.workspace import (
    _write_json_atomic as _write_json,
)
from pace.runtime.output.workspace import (
    _write_text_atomic as _write_text,
)
from pace.runtime.output.workspace import is_call_path, stage_ref, work_ref
from pace.runtime.prompt_composer import PromptComposerResult
from pace.runtime.prompt_policy import PromptAssembly, load_prompt_assembly


logger = logging.getLogger(__name__)


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
        if isinstance(self.max_proof_states, bool) or not isinstance(self.max_proof_states, int):
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
        if self.phase not in DIRECTOR_LED_PHASES:
            raise ValueError(f"invalid Director-led phase: {self.phase}")
        if self.round_index < 0:
            raise ValueError("Director-led round_index must be non-negative")
        if self.max_proof_states < 1:
            raise ValueError("Director-led max_proof_states must be positive")
        if type(self.proof_states_used) is not int or not 0 <= self.proof_states_used <= self.max_proof_states:
            raise ValueError("Director-led proof_states_used must be within the proof budget")
        if self.round_index == 0 and self.phase != "DIRECTOR":
            raise ValueError(
                "Director-led round_index 0 is valid only in the DIRECTOR phase"
            )
        if self.round_index == 0 and any(
            (
                self.pending_dispatch_ref,
                self.pending_prompt_ref,
                self.pending_assignment_ref,
                self.pending_verifier_request_ref,
                self.last_assignment_ref,
                self.completed_result_refs,
                self.last_directive_ref,
                self.last_composer_manifest_ref,
                self.last_specialist_result_ref,
                self.last_verification_ref,
                self.diagnostics_ref,
                self.stop_reason,
                self.terminal_result,
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
            raise ValueError(
                "Director-led active state cannot carry a terminal_result"
            )
        if self.phase == "FINALIZE" and not self.terminal_result:
            raise ValueError(
                "Director-led FINALIZE state requires a terminal_result"
            )
        if self.phase in {"STOPPED", "FAILED"} and self.terminal_result != "FAILED":
            raise ValueError(
                f"Director-led {self.phase} state must have terminal_result FAILED"
            )
        if self.terminal_result == "PASS" and self.phase != "FINALIZE":
            raise ValueError(
                "Director-led PASS is only valid in the FINALIZE phase"
            )
        if self.terminal_result == "FAILED" and self.phase not in terminal_phases:
            raise ValueError(
                "Director-led FAILED result requires a terminal phase"
            )

        if self.phase == "PENDING_BATCH" and not self.pending_assignment_ref:
            raise ValueError("PENDING_BATCH requires a committed batch manifest")
        if self.phase == "PENDING_SPECIALIST":
            if not self.pending_dispatch_ref or not self.pending_assignment_ref:
                raise ValueError(
                    "PENDING_SPECIALIST state requires dispatch and assignment refs"
                )
            if not self.last_directive_ref:
                raise ValueError(
                    "PENDING_SPECIALIST state requires last_directive_ref"
                )
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
            raise ValueError(
                "DIRECTOR state cannot carry pending dispatch references"
            )
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
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "DirectorLedState":
        if not isinstance(value, Mapping):
            raise DirectorLedEngineError("Director-led state must be a JSON object")
        if set(value) != DIRECTOR_LED_STATE_FIELDS:
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
                target_contract_id=_string_value(value.get("target_contract_id", ""), "target_contract_id"),
                round_index=_strict_int(value.get("round_index"), "round_index"),
                max_proof_states=_strict_int(value.get("max_proof_states"), "max_proof_states"),
                proof_states_used=_strict_int(value.get("proof_states_used"), "proof_states_used"),
                phase=_string_value(value.get("phase", "DIRECTOR"), "phase"),
                pending_dispatch_ref=_string_value(value.get("pending_dispatch_ref", ""), "pending_dispatch_ref"),
                pending_prompt_ref=_string_value(value.get("pending_prompt_ref", ""), "pending_prompt_ref"),
                pending_assignment_ref=_string_value(value.get("pending_assignment_ref", ""), "pending_assignment_ref"),
                pending_verifier_request_ref=_string_value(value.get("pending_verifier_request_ref", ""), "pending_verifier_request_ref"),
                last_assignment_ref=_string_value(value.get("last_assignment_ref", ""), "last_assignment_ref"),
                completed_result_refs=refs,
                last_directive_ref=_string_value(value.get("last_directive_ref", ""), "last_directive_ref"),
                last_composer_manifest_ref=_string_value(value.get("last_composer_manifest_ref", ""), "last_composer_manifest_ref"),
                last_specialist_result_ref=_string_value(value.get("last_specialist_result_ref", ""), "last_specialist_result_ref"),
                last_verification_ref=_string_value(value.get("last_verification_ref", ""), "last_verification_ref"),
                diagnostics_ref=_string_value(value.get("diagnostics_ref", ""), "diagnostics_ref"),
                stop_reason=_string_value(value.get("stop_reason", ""), "stop_reason"),
                terminal_result=_string_value(value.get("terminal_result", ""), "terminal_result"),
                candidate_ref=_string_value(value.get("candidate_ref", ""), "candidate_ref"),
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
    def stopped(self) -> bool:
        return self.state.phase == "STOPPED"

    @property
    def result(self) -> str:
        return str(self.state.terminal_result or "")


DirectorHandler = Callable[..., Any]
ComposerHandler = Callable[..., Any]
SpecialistHandler = Callable[..., Any]
VerifierHandler = Callable[..., Any]


class DirectorLedEngine:
    """Run one-specialist-per-round research under a Host-owned protocol.

    Parameters are intentionally callables instead of concrete agent classes:

    ``director`` receives the current public refs and committed result refs.
    ``composer`` receives validated decisions for roles that use the
    top-level Prompt Composer and must return a
    :class:`PromptComposerResult`; Survey/Research use their static role
    prompts directly and SMI owns its internal phase prompt graph.
    ``specialists`` maps protocol agent IDs to handlers.  A handler receives
    ``prompt``, ``decision``, ``context`` and the dispatch fields as keyword
    arguments.
    ``verifier`` is optional and is called after a specialist result.  Its
    return value is persisted verbatim as search context; this engine never
    interprets a verifier result as PASS or FAILED.
    ``on_round_committed`` is an optional Host callback invoked only after a committed
    round boundary.  Its display result is never added to search context.
    """

    def __init__(
        self,
        *,
        workdir: Path,
        director: DirectorHandler,
        composer: ComposerHandler,
        specialists: Mapping[str, SpecialistHandler],
        target_contract_id: str = "",
        max_proof_states: int = 18,
        max_rounds: int | None = None,
        state_ref: str | None = None,
        round_root: str | None = None,
        verifier: VerifierHandler | None = None,
        on_round_committed: Callable[..., Any] | None = None,
    ) -> None:
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not callable(director) or not callable(composer):
            raise TypeError("director and composer must be callable")
        if not isinstance(specialists, Mapping) or not specialists:
            raise ValueError("at least one specialist handler is required")
        self.director = director
        self.composer = composer
        self.specialists = dict(specialists)
        if not isinstance(target_contract_id, str):
            raise TypeError("target_contract_id must be a string")
        if target_contract_id and not target_contract_id.strip():
            raise ValueError("target_contract_id must not be whitespace-only")
        self.target_contract_id = target_contract_id
        # ``max_proof_states`` is persisted in the continuation state and therefore
        # is a machine-owned budget, not a user-facing convenience value.
        # Reject coercions (especially bool/float/string values) so a caller
        # cannot silently change the committed proof-state budget.
        if isinstance(max_proof_states, bool) or not isinstance(max_proof_states, int):
            raise TypeError("max_proof_states must be an integer")
        if max_proof_states < 1:
            raise ValueError("max_proof_states must be positive")
        self.max_proof_states = max_proof_states
        if max_rounds is not None and (type(max_rounds) is not int or max_rounds < 1):
            raise ValueError("max_rounds must be a positive integer")
        self.max_rounds = max_rounds
        normalized_state_ref = _safe_relative_ref(
            state_ref,
            default=work_ref(workdir, 'state/director_state.json'),
        )
        if normalized_state_ref != work_ref(workdir, 'state/director_state.json'):
            raise ValueError(
                f"Director-led state_ref must be {work_ref(workdir, 'state/director_state.json')}"
            )
        normalized_round_root = _safe_relative_ref(
            round_root,
            default=work_ref(workdir),
        )
        if normalized_round_root != work_ref(workdir):
            raise ValueError(
                f"Director-led round_root must be {work_ref(workdir)}"
            )
        self.state_ref = work_ref(workdir, 'state/director_state.json')
        self.round_root = work_ref(workdir)
        self.commit_progress = True
        self.verifier = verifier
        self.parent_task_id = ""
        self.on_round_committed = on_round_committed
        unknown = set(self.specialists).difference(
            DISPATCHABLE_AGENT_IDS
        )
        if unknown:
            raise ValueError(f"unknown specialist handlers: {sorted(unknown)}")

    @property
    def state_path(self) -> Path:
        return self.workdir / self.state_ref

    def run(
        self,
        *,
        control_context: str = "",
        resume: bool = False,
    ) -> DirectorLedRunResult:
        """Run until a verdict, STOP, an applicable budget, or a Host interface failure.

        ``resume=True`` opens the explicit state file.  No directory scan is
        used to infer progress.  A pending dispatch is resumed from its
        persisted directive and Composer output, which avoids asking the
        Director to make a different decision after a process interruption.
        """

        if resume:
            state = self._load_state()
            # The committed state is the authority on resume.  An explicitly
            # supplied engine target may confirm that identity, but it may not
            # fill an absent state target (that would rebind a checkpoint to a
            # caller-owned value).  Omitting the constructor target remains
            # valid when the state already carries one.
            if self.target_contract_id and not state.target_contract_id:
                raise DirectorLedEngineError(
                    "committed Director-led state has no target contract identity"
                )
            if (
                state.target_contract_id
                and self.target_contract_id
                and state.target_contract_id != self.target_contract_id
            ):
                raise DirectorLedEngineError(
                    "target contract differs from committed Director-led state"
                )
            for ref in state.completed_result_refs:
                self._require_run_local_file_ref(ref, field="completed_result_ref")
        else:
            state = self._new_state()
        if not resume and (state.round_index or self.state_path.is_file()):
            raise DirectorLedEngineError(
                "Director-led state already exists; use resume=True for continuation"
            )
        self._persist_state(state)

        if state.phase in {"STOPPED", "FINALIZE", "FAILED"}:
            return self._result(state)
        if state.phase == "PENDING_BATCH":
            from pace.orchestrator.director_parallel import run_batch

            state = run_batch(self, state, control_context=control_context)
            self._publish_committed_round(state, trigger_id=f"batch-{state.round_index:04d}")
        if state.phase == "VERIFY":
            if self.verifier is None:
                return self._fail_round(
                    state,
                    state.round_index,
                    "VERIFY",
                    DirectorLedEngineError(
                        "Prover candidate cannot be verified: verifier is not configured"
                    ),
                )
            state = self._resume_verification(state, control_context=control_context)
            self._publish_committed_round(
                state,
                trigger_id=f"round-{state.round_index:04d}-verification",
            )
            if state.phase != "DIRECTOR":
                return self._result(state)
        if state.phase == "PENDING_SPECIALIST":
            state = self._run_pending_specialist(
                state,
                control_context=control_context,
                recovering=resume,
            )
            self._publish_committed_round(state, trigger_id=f"round-{state.round_index:04d}")

        while state.phase == "DIRECTOR":
            from pace.runtime.sessions import budget_exhausted

            reason = budget_exhausted(self.workdir)
            if self.max_rounds is not None and state.round_index >= self.max_rounds:
                reason = "RESEARCH_ROUND_LIMIT_REACHED"
            if reason:
                state = self._replace_state(
                    state,
                    phase="FINALIZE",
                    stop_reason=reason,
                    terminal_result="FAILED",
                )
                self._persist_state(state)
                break
            state = self._run_round(
                state,
                control_context=control_context,
                recovering=resume,
            )
            self._publish_committed_round(state, trigger_id=f"round-{state.round_index:04d}")

        return self._result(state)

    def _publish_committed_round(self, state: DirectorLedState, *, trigger_id: str) -> None:
        """Update the display-only report after a committed Host boundary."""

        if self.on_round_committed is None or int(state.round_index or 0) < 1:
            return
        try:
            self.on_round_committed(
                trigger_id=trigger_id,
                round_index=state.round_index,
                input_refs=_unique_strings((
                    *self._director_input_refs(state), *state.completed_result_refs,
                )),
                completed_result_refs=state.completed_result_refs,
                state=state,
            )
        except DeliveryPaused:
            # The callback ran after a committed boundary. Pause without
            # changing the mathematical state or consuming another dispatch.
            raise
        except Exception:  # noqa: BLE001 - reporting cannot alter proof flow
            logger.warning("Research Reporter callback failed", exc_info=True)

    def _resume_verification(
        self,
        state: DirectorLedState,
        *,
        control_context: str,
    ) -> DirectorLedState:
        """Resume the committed Prover verification boundary without dispatch."""

        if not state.last_directive_ref or not state.last_specialist_result_ref:
            return self._fail_round(
                state,
                state.round_index,
                "VERIFY",
                DirectorLedEngineError("VERIFY state lacks committed Prover refs"),
            )
        if any(
            (
                state.pending_dispatch_ref,
                state.pending_prompt_ref,
                state.pending_assignment_ref,
            )
        ):
            return self._fail_round(
                state,
                state.round_index,
                "VERIFY",
                DirectorLedEngineError(
                    "VERIFY state contains pending dispatch references"
                ),
            )
        try:
            directive_ref = self._require_round_local_file_ref(
                state.last_directive_ref,
                field="last_directive_ref",
                round_index=state.round_index,
            )
            result_ref = self._require_round_local_file_ref(
                state.last_specialist_result_ref,
                field="last_specialist_result_ref",
                round_index=state.round_index,
            )
            directive = _read_json(self.workdir / directive_ref)
            decision = _coerce_persisted_decision(
                directive,
                expected_round_index=state.round_index,
            )
            if decision.action is not DirectorAction.DISPATCH or decision.agent_id != "prover":
                raise DirectorLedEngineError("VERIFY state is not bound to a Prover dispatch")
            result_record = _read_json(self.workdir / result_ref)
            _validate_specialist_result_agent_id(
                result_record,
                expected_agent_id=decision.agent_id,
            )
            raw_assignment_ref = state.last_assignment_ref
            if not raw_assignment_ref:
                raise DirectorLedEngineError(
                    "VERIFY state lacks a committed assignment reference"
                )
            assignment_ref = self._require_round_local_file_ref(
                raw_assignment_ref,
                field="assignment_ref",
                round_index=state.round_index,
            )
            assignment = _read_json(self.workdir / assignment_ref)
            candidate_ref = self._require_round_local_file_ref(
                state.candidate_ref,
                field="candidate_ref",
                round_index=state.round_index,
            )
            # The Host chooses a deterministic output path when it commits a
            # dispatch.  A recovered state must not be able to replace that
            # binding by changing candidate_ref and assignment together.
            expected_candidate_ref = self._default_output_ref(
                "prover", state.round_index
            )
            if candidate_ref != expected_candidate_ref:
                raise DirectorLedEngineError(
                    "candidate_ref differs from Host assignment output_ref"
                )
            _validate_assignment(
                assignment,
                decision=decision,
                assignment_ref=assignment_ref,
                directive_ref=directive_ref,
                round_index=state.round_index,
                expected_output_ref=expected_candidate_ref,
                expected_input_refs=self._assignment_input_refs(decision, state.round_index),
            )
            decision = replace(decision, input_refs=tuple(assignment["input_refs"]))
            if str(assignment.get("output_ref") or "") != candidate_ref:
                raise DirectorLedEngineError(
                    "candidate_ref differs from Host assignment output_ref"
                )
            self._validate_prover_result_binding(
                result_record,
                candidate_ref=candidate_ref,
            )
            self._validate_optional_verify_checkpoints(
                state,
                decision=decision,
                directive_ref=directive_ref,
                candidate_ref=candidate_ref,
            )
            context = {
                "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                "round_index": state.round_index,
                "target_contract_id": self.target_contract_id or state.target_contract_id,
                    "completed_result_refs": list(state.completed_result_refs),
                    "previous_verification_ref": state.last_verification_ref,
                "control_context": str(control_context or ""),
                "directive_ref": directive_ref,
                "assignment_ref": assignment_ref,
                "output_ref": str(assignment.get("output_ref") or candidate_ref),
                "verified_progress_ref": next((ref for ref in decision.input_refs
                    if ref == self._progress_snapshot_ref(state.round_index)), ""),
            }
            return self._run_verifier(
                state,
                decision=decision,
                context=context,
                result_record=result_record,
                result_ref=result_ref,
                recovering=True,
            )
        except Exception as exc:
            return self._fail_round(state, state.round_index, "VERIFY", exc)

    def _validate_optional_verify_checkpoints(
        self,
        state: DirectorLedState,
        *,
        decision: DirectorDecision,
        directive_ref: str,
        candidate_ref: str,
    ) -> None:
        """Validate checkpoint refs retained while a Prover awaits verification."""

        if state.last_composer_manifest_ref:
            manifest_ref = self._require_round_local_file_ref(
                state.last_composer_manifest_ref,
                field="last_composer_manifest_ref",
                round_index=state.round_index,
            )
            expected_manifest_ref = self._round_ref(
                state.round_index, "prompt_assembly.json"
            )
            if manifest_ref != expected_manifest_ref:
                raise DirectorLedEngineError(
                    "last_composer_manifest_ref differs from the current dispatch"
                )
            prompt_ref = self._round_ref(state.round_index, "final_prompt.md")
            prompt_ref = self._require_round_local_file_ref(
                prompt_ref,
                field="prompt_ref",
                round_index=state.round_index,
            )
            manifest = _read_json(self.workdir / manifest_ref)
            _validate_composer_manifest(
                manifest,
                decision=decision,
                round_index=state.round_index,
                prompt_ref=prompt_ref,
                output_ref=candidate_ref,
                directive_ref=directive_ref,
            )
            prompt_path = self.workdir / prompt_ref
            if not prompt_path.is_file():
                raise DirectorLedEngineError(
                    "committed Composer prompt is missing"
                )
            prompt = prompt_path.read_text(encoding="utf-8")
            _validate_composer_prompt(
                prompt,
                source="committed Composer",
            )

        if state.last_verification_ref:
            verification_ref = self._require_round_local_file_ref(
                state.last_verification_ref,
                field="last_verification_ref",
                round_index=state.round_index,
            )
            expected_verification_ref = self._round_ref(
                state.round_index, "verification/verification.json"
            )
            if verification_ref != expected_verification_ref:
                raise DirectorLedEngineError(
                    "last_verification_ref differs from the current round"
                )

    def _validate_prover_result_binding(
        self,
        result_record: Mapping[str, Any],
        *,
        candidate_ref: str,
    ) -> None:
        """Bind a recovered Prover handoff to the Host-owned candidate path."""

        if not _is_success_status(result_record.get("status")):
            raise DirectorLedEngineError(
                "VERIFY state does not contain a successful Prover handoff"
            )
        # The Host assignment is the only source of the candidate path.  A
        # Prover must repeat that path in the current handoff's explicit
        # ``deliverable_ref`` field; proof-specific aliases are obsolete and
        # must not be interpreted during recovery.
        if "proof_ref" in result_record or "candidate_proof_ref" in result_record:
            raise DirectorLedEngineError(
                "recovered Prover handoff uses an unsupported proof reference alias"
            )
        raw_ref = result_record.get("deliverable_ref")
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            raise DirectorLedEngineError(
                "recovered Prover handoff has no deliverable reference"
            )
        normalized = self._resolve_stable_ref(raw_ref)
        if not normalized or normalized != candidate_ref:
            raise DirectorLedEngineError(
                "recovered Prover handoff deliverable_ref differs from candidate"
            )

    def resume(self, *, control_context: str = "") -> DirectorLedRunResult:
        """Resume from the explicit state file."""

        return self.run(resume=True, control_context=control_context)

    def _new_state(self) -> DirectorLedState:
        return DirectorLedState(
            target_contract_id=self.target_contract_id,
            round_index=0,
            max_proof_states=self.max_proof_states,
            phase="DIRECTOR",
        )

    def _run_round(
        self,
        state: DirectorLedState,
        *,
        control_context: str,
        recovering: bool = False,
    ) -> DirectorLedState:
        round_index = state.round_index + 1
        round_dir = self.workdir / self.round_root / f"round{round_index}"
        try:
            batch_path = round_dir / "batch.json"
            if recovering and batch_path.is_file():
                from pace.orchestrator.director_parallel import run_batch
                reserved = DirectorLedState.from_json(_read_json(batch_path)["reserved_state"])
                self._persist_state(reserved)
                return run_batch(self, reserved, control_context=control_context)
            if _path_has_symlink_component(self.workdir, round_dir):
                raise NotADirectoryError(
                    f"Director round path contains a symlink: {round_dir}"
                )
            if round_dir.exists() or round_dir.is_symlink():
                if not recovering:
                    # A fresh invocation must never reuse an existing round;
                    # doing so could silently attach a new decision to an
                    # old run's artifacts.
                    raise FileExistsError(round_dir)
                if round_dir.is_symlink() or not round_dir.is_dir():
                    raise NotADirectoryError(round_dir)
                # A decision or assignment may be durable before its state
                # commit. Reuse that transaction with its exact input snapshot;
                # Composer and specialist files belong to a later phase.
                entries = tuple(
                    path.relative_to(round_dir).as_posix()
                    for path in round_dir.rglob("*")
                    if path.is_file() or path.is_symlink()
                )
                allowed_entries = {
                    "director_context.json",
                    "director_decision.json",
                    "assignment/task.json",
                    "progress_snapshot.json",
                    "diagnostics.json",
                }
                unexpected = sorted(
                    entry for entry in entries if entry not in allowed_entries
                    and entry != "README.md" and not entry.startswith("calls/")
                )
                if unexpected:
                    raise DirectorLedEngineError(
                        "uncommitted Director round contains unexpected checkpoints: "
                        + ", ".join(unexpected)
                    )
                context_path = round_dir / "director_context.json"
                decision_path = round_dir / "director_decision.json"
                assignment_path = round_dir / "assignment/task.json"
                if decision_path.is_file() and not context_path.is_file():
                    raise DirectorLedEngineError("dispatch decision has no input snapshot")
                if assignment_path.is_file() and not decision_path.is_file():
                    raise DirectorLedEngineError("dispatch assignment has no decision")
                if context_path.is_file():
                    try:
                        persisted_context = _read_json(context_path)
                    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                        raise DirectorLedEngineError(
                            "uncommitted Director context is invalid"
                        ) from exc
                    expected_context = {
                        "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                        "round_index": round_index,
                        "target_contract_id": self.target_contract_id or state.target_contract_id,
                        "completed_result_refs": list(state.completed_result_refs),
                        "previous_verification_ref": state.last_verification_ref,
                        "control_context": str(control_context or ""),
                    }
                    if persisted_context != expected_context:
                        raise DirectorLedEngineError(
                            "uncommitted Director context differs from committed state"
                        )
            else:
                round_dir.mkdir(parents=True, exist_ok=False)
        except Exception as exc:
            # A pre-existing round directory is an incomplete or conflicting
            # checkpoint. Surface it through the same committed diagnostic
            # boundary as other Host failures instead of leaking an I/O error.
            return self._fail_round(state, round_index, "DIRECTOR", exc)
        context = {
            "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
            "round_index": round_index,
            "target_contract_id": self.target_contract_id or state.target_contract_id,
            "completed_result_refs": list(state.completed_result_refs),
            "previous_verification_ref": state.last_verification_ref,
            "control_context": str(control_context or ""),
        }
        context_ref = self._round_ref(round_index, "director_context.json")
        directive_ref = self._round_ref(round_index, "director_decision.json")
        try:
            # Context, decision, and assignment are one Host-owned dispatch
            # transaction.  A write failure must become a committed round
            # diagnostic so the pipeline can still publish a terminal report.
            _write_json(self.workdir / context_ref, context)
            publish_progress(self.workdir, task_id=f'round-{round_index:04d}-director', stage='director', status='started', round_index=round_index)
            snapshot_ref = self._round_ref(round_index, "progress_snapshot.json")
            if recovering and (self.workdir / directive_ref).is_file():
                decision = _coerce_persisted_decision(
                    _read_json(self.workdir / directive_ref),
                    expected_round_index=round_index,
                )
            else:
                raw_decision = _invoke(
                    self.director,
                    {
                        "workdir": self.workdir,
                        "round_index": round_index,
                        "target_contract_id": context["target_contract_id"],
                        "input_refs": self._director_input_refs(state),
                        "completed_result_refs": state.completed_result_refs,
                        "proof_states_remaining": state.max_proof_states - state.proof_states_used,
                        "research_rounds_remaining": (
                            self.max_rounds - state.round_index if self.max_rounds is not None else None
                        ),
                        "control_context": str(control_context or ""),
                        "context": context,
                    },
                )
                decision = _coerce_decision(
                    raw_decision,
                )
            from pace.contracts.research.director_protocol import validate_director_reference_bindings
            validate_director_reference_bindings(decision, self.workdir)
        except Exception as exc:
            return self._fail_round(state, round_index, "DIRECTOR", exc)

        try:
            if decision.action is DirectorAction.DISPATCH:
                snapshot_verified_progress(self.workdir, snapshot_ref=snapshot_ref)
            if decision.parallel_tasks:
                from pace.orchestrator.director_parallel import (
                    reserve_batch,
                    run_batch,
                )
                state = reserve_batch(self, state, decision)
                publish_progress(self.workdir, task_id=f'round-{round_index:04d}-director', stage='director', status='completed', round_index=round_index)
                return run_batch(self, state, control_context=control_context)
            if decision.action is DirectorAction.DISPATCH:
                self._check_task_budget((decision,), state)
            _write_json(
                self.workdir / directive_ref,
                {
                    "schema_version": DIRECTOR_PROTOCOL_SCHEMA_VERSION,
                    "round_index": round_index,
                    **decision.to_json(),
                },
            )
            if decision.action is DirectorAction.DISPATCH:
                assignment_ref = self._round_ref(round_index, "assignment/task.json")
                output_ref = self._default_output_ref(decision.agent_id, round_index)
                if recovering and (self.workdir / assignment_ref).is_file():
                    _validate_assignment(
                        _read_json(self.workdir / assignment_ref),
                        decision=decision,
                        assignment_ref=assignment_ref,
                        directive_ref=directive_ref,
                        round_index=round_index,
                        expected_output_ref=output_ref,
                        expected_input_refs=self._assignment_input_refs(decision, round_index),
                    )
                _write_json(
                    self.workdir / assignment_ref,
                    {
                        "schema_version": DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
                        "round_index": round_index,
                        "agent_id": decision.agent_id,
                        "prompt_brief": decision.prompt_brief,
                        "input_refs": list(self._assignment_input_refs(decision, round_index)),
                        "output_ref": output_ref,
                        "director_decision_ref": directive_ref,
                    },
                )
            else:
                assignment_ref = ""
            state = self._replace_state(
                state,
                round_index=round_index,
                proof_states_used=state.proof_states_used + self._proof_state_cost(decision, state),
                phase="PENDING_SPECIALIST" if decision.action is DirectorAction.DISPATCH else "STOPPED",
                pending_dispatch_ref=directive_ref if decision.action is DirectorAction.DISPATCH else "",
                pending_prompt_ref="",
                pending_assignment_ref=assignment_ref,
                pending_verifier_request_ref="",
                last_assignment_ref="",
                last_directive_ref=directive_ref,
                last_composer_manifest_ref="",
                last_specialist_result_ref="",
                last_verification_ref="",
                diagnostics_ref="",
                candidate_ref="",
                stop_reason="" if decision.action is DirectorAction.DISPATCH else "DIRECTOR_STOP",
                terminal_result="" if decision.action is DirectorAction.DISPATCH else "FAILED",
            )
            self._persist_state(state)
        except DirectorBudgetExhausted:
            state = self._replace_state(
                state, round_index=round_index, phase="FINALIZE",
                stop_reason="PROOF_STATE_BUDGET_EXHAUSTED", terminal_result="FAILED",
            )
            self._persist_state(state)
            publish_progress(self.workdir, task_id=f'round-{round_index:04d}-director',
                             stage='director', status='completed', round_index=round_index,
                             notice='search_stopped')
            return state
        except Exception as exc:
            return self._fail_round(state, max(state.round_index, round_index), "DIRECTOR", exc)
        if decision.action is DirectorAction.STOP:
            publish_progress(self.workdir, task_id=f'round-{round_index:04d}-director', stage='director', status='completed', round_index=round_index)
            return state

        publish_progress(self.workdir, task_id=f'round-{round_index:04d}-director', stage='director', status='completed', round_index=round_index)
        return self._run_pending_specialist(state, control_context=control_context)

    def _director_input_refs(self, state: DirectorLedState) -> tuple[str, ...]:
        """Start scheduling from current navigation and the latest evidence.

        The state ledger and persistent directory retain all historical refs.
        They need not be repeated as reading suggestions on every call.
        """

        refs = [
            work_ref(self.workdir, 'input/request.md'),
            work_ref(self.workdir, 'input/problem.md'),
            work_ref(self.workdir, 'input/target_contract.json'),
            *((work_ref(self.workdir, 'input/handoff.md'),)
              if (self.workdir / work_ref(self.workdir, 'input/handoff.md')).is_file() else ()),
            work_ref(self.workdir, 'state/proof_loop_budget.json'),
            work_ref(self.workdir, 'state/director_state.json'),
            *((FINDINGS_INDEX_REF,) if (self.workdir / FINDINGS_INDEX_REF).is_file() else ()),
            "memory/README.md",
            "memory/active_problem_dossier.json",
            state.last_specialist_result_ref,
            state.last_verification_ref,
            state.diagnostics_ref,
            *stage_finding_review_refs(self.workdir, state.round_index, state.completed_result_refs),
        ]
        if state.last_verification_ref:
            verification = _read_json(self.workdir / state.last_verification_ref).get("verification", {})
            repair_ref = str(verification.get("repair_context_ref") or "")
            if repair_ref:
                refs.append(repair_ref)
        return _unique_strings(ref for ref in refs if isinstance(ref, str) and ref)

    def _progress_snapshot_ref(self, round_index: int) -> str:
        return self._round_ref(round_index, "progress_snapshot.json")

    def _assignment_input_refs(
        self, decision: DirectorDecision, round_index: int,
    ) -> tuple[str, ...]:
        """Bind execution inputs without changing the Director's ticket."""

        progress_ref = self._progress_snapshot_ref(round_index)
        return _unique_strings((
            work_ref(self.workdir, "input/request.md"),
            work_ref(self.workdir, "input/problem.md"),
            work_ref(self.workdir, "input/target_contract.json"),
            *((work_ref(self.workdir, "input/handoff.md"),)
              if (self.workdir / work_ref(self.workdir, "input/handoff.md")).is_file() else ()),
            work_ref(self.workdir, "state/proof_loop_budget.json"),
            *((progress_ref,) if (self.workdir / progress_ref).is_file() else ()),
            *(ref for ref in decision.input_refs if ref != "memory/verified_progress.json"),
        ))

    def _proof_state_cost(self, decision: DirectorDecision, state: DirectorLedState) -> int:
        if decision.action is not DirectorAction.DISPATCH:
            return 0
        return 1 if decision.agent_id in {"research", "smi"} else 0

    def _check_task_budget(self, tasks: tuple[DirectorDecision, ...], state: DirectorLedState) -> None:
        cost = sum(self._proof_state_cost(task, state) for task in tasks)
        if state.proof_states_used + cost > state.max_proof_states:
            raise DirectorBudgetExhausted("insufficient proof states for this batch")

    def _run_pending_specialist(
        self,
        state: DirectorLedState,
        *,
        control_context: str,
        recovering: bool = False,
    ) -> DirectorLedState:
        # A pending dispatch is committed before Composer or the specialist
        # is called.  Do not silently reconstruct either binding from the
        # round layout when a persisted state field is missing.
        directive_ref = state.pending_dispatch_ref
        if not directive_ref:
            return self._fail_round(
                state,
                state.round_index,
                "PENDING_SPECIALIST",
                DirectorLedEngineError(
                    "pending dispatch has no committed directive reference"
                ),
            )
        if not state.pending_assignment_ref:
            return self._fail_round(
                state,
                state.round_index,
                "PENDING_SPECIALIST",
                DirectorLedEngineError(
                    "pending dispatch has no committed assignment reference"
                ),
            )
        try:
            # A pending state is an external recovery boundary.  Validate its
            # persisted references before opening either JSON file; generated
            # round paths are not a substitute for checking tampered state.
            directive_ref = self._require_round_local_file_ref(
                directive_ref,
                field="pending_dispatch_ref",
                round_index=state.round_index,
            )
            if not state.last_directive_ref:
                raise DirectorLedEngineError(
                    "pending dispatch has no committed last_directive_ref"
                )
            last_directive_ref = self._require_round_local_file_ref(
                state.last_directive_ref,
                field="last_directive_ref",
                round_index=state.round_index,
            )
            if last_directive_ref != directive_ref:
                raise DirectorLedEngineError(
                    "pending_dispatch_ref differs from last_directive_ref"
                )
            directive = _read_json(self.workdir / directive_ref)
            decision = _coerce_persisted_decision(
                directive,
                expected_round_index=state.round_index,
            )
            from pace.contracts.research.director_protocol import validate_director_reference_bindings
            validate_director_reference_bindings(decision, self.workdir)
            if decision.action is not DirectorAction.DISPATCH:
                raise DirectorLedEngineError("pending directive is not DISPATCH")
            spec = specialist_spec(decision.agent_id)
            handler = self.specialists.get(decision.agent_id)
            if handler is None:
                raise DirectorLedEngineError(
                    f"no handler is registered for specialist {decision.agent_id}"
                )
            round_index = state.round_index
            assignment_ref = state.pending_assignment_ref
            assignment_ref = self._require_round_local_file_ref(
                assignment_ref,
                field="pending_assignment_ref",
                round_index=state.round_index,
            )
            assignment = _read_json(self.workdir / assignment_ref)
            output_ref = str(assignment.get("output_ref") or "").strip()
            if not output_ref:
                raise DirectorLedEngineError("assignment has no Host output_ref")
            _validate_assignment(
                assignment,
                decision=decision,
                assignment_ref=assignment_ref,
                directive_ref=directive_ref,
                round_index=round_index,
                expected_output_ref=self._default_output_ref(decision.agent_id, round_index),
                expected_input_refs=self._assignment_input_refs(decision, round_index),
            )
            # The model-owned ticket stays unchanged on disk. Composer,
            # specialist and verifier use the frozen Host assignment inputs.
            decision = replace(decision, input_refs=tuple(assignment["input_refs"]))
            context = {
                "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                "round_index": round_index,
                "target_contract_id": self.target_contract_id or state.target_contract_id,
                    "completed_result_refs": list(state.completed_result_refs),
                    "previous_verification_ref": state.last_verification_ref,
                "control_context": str(control_context or ""),
                "directive_ref": directive_ref,
                "assignment_ref": assignment_ref,
                "output_ref": output_ref,
                "verified_progress_ref": next((ref for ref in decision.input_refs
                    if ref == self._progress_snapshot_ref(round_index)), ""),
            }
            publish_progress(self.workdir, task_id=f'round-{round_index:04d}', parent_task_id=self.parent_task_id, stage=decision.agent_id, status='started', round_index=round_index)
            smi_owned_prompt_graph = decision.agent_id == "smi"
            manifest_ref = ""
            prompt_ref = ""
            final_prompt = ""
            input_refs = tuple(decision.input_refs)
            composer_checkpoint_committed = False
            if smi_owned_prompt_graph:
                if state.last_composer_manifest_ref or state.pending_prompt_ref:
                    raise DirectorLedEngineError(
                        "SMI dispatch must not carry a top-level Composer checkpoint"
                    )
            else:
                expected_manifest_ref = self._round_ref(
                    round_index, "prompt_assembly.json"
                )
                expected_prompt_ref = self._round_ref(round_index, "final_prompt.md")
                persisted_manifest_ref = state.last_composer_manifest_ref
                persisted_prompt_ref = state.pending_prompt_ref
                if bool(persisted_manifest_ref) != bool(persisted_prompt_ref):
                    raise DirectorLedEngineError(
                        "Composer checkpoint references must be committed together"
                    )
                composer_checkpoint_committed = bool(
                    persisted_manifest_ref and persisted_prompt_ref
                )
                if composer_checkpoint_committed:
                    manifest_ref = self._require_round_local_file_ref(
                        persisted_manifest_ref,
                        field="last_composer_manifest_ref",
                        round_index=round_index,
                    )
                    prompt_ref = self._require_round_local_file_ref(
                        persisted_prompt_ref,
                        field="pending_prompt_ref",
                        round_index=round_index,
                    )
                    if manifest_ref != expected_manifest_ref:
                        raise DirectorLedEngineError(
                            "last_composer_manifest_ref differs from the current dispatch"
                        )
                    if prompt_ref != expected_prompt_ref:
                        raise DirectorLedEngineError(
                            "pending_prompt_ref differs from the current dispatch"
                        )
                else:
                    manifest_ref = expected_manifest_ref
                    prompt_ref = expected_prompt_ref
                request_path = (self.workdir / manifest_ref).with_name("composer_request.json")
                if composer_checkpoint_committed or request_path.is_file():
                    assembly = load_prompt_assembly(request_path)
                    if assembly.target_contract_id != context["target_contract_id"] or assembly.output_artifact_ref != output_ref:
                        raise DirectorLedEngineError("saved Composer inputs are bound to another task")
                else:
                    assembly = build_specialist_prompt_assembly(
                        workdir=self.workdir, decision=decision,
                        target_contract_id=context["target_contract_id"], output_ref=output_ref,
                        manifest_ref=manifest_ref,
                    )
                input_refs = assembly.candidate_refs
                composer_record = self._load_or_compose(
                    manifest_ref=manifest_ref,
                    prompt_ref=prompt_ref,
                    decision=decision,
                    spec=spec,
                    context=context,
                    round_index=round_index,
                    assembly=assembly,
                    input_refs=input_refs,
                    checkpoint_committed=composer_checkpoint_committed,
                )
                final_prompt = composer_record.final_prompt
                # Commit prompt bytes before invoking an ordinary specialist.
                # SMI instead owns an append-only internal phase graph and
                # recovers from its committed assignment and outcome.
                state = self._replace_state(
                    state,
                    pending_prompt_ref=prompt_ref,
                    last_composer_manifest_ref=manifest_ref,
                )
                self._persist_state(state)
            result_ref = self._round_ref(round_index, "agent_result.json")
            if smi_owned_prompt_graph:
                recovered, specialist_value = False, None
            else:
                recovered, specialist_value = self._recover_pending_specialist_value(
                    result_ref=result_ref,
                    output_ref=output_ref,
                    agent_id=decision.agent_id,
                    round_index=round_index,
                    recovering=recovering and composer_checkpoint_committed,
                )
            if not recovered:
                specialist_value = _invoke(
                    handler,
                    {
                        "workdir": self.workdir,
                        "prompt": final_prompt,
                        "decision": decision,
                        "agent_spec": spec,
                        "context": context,
                        "agent_id": decision.agent_id,
                        "input_refs": input_refs,
                        "round_index": round_index,
                        "prompt_ref": prompt_ref,
                        "composer_manifest_ref": manifest_ref,
                        "output_ref": output_ref,
                    },
                )
            result_record, stable_ref = _coerce_specialist_result(
                specialist_value,
                default_ref=result_ref,
            )
            _validate_specialist_result_agent_id(
                result_record,
                expected_agent_id=decision.agent_id,
            )
            if str(result_record.get("status") or "").upper() == "INTERFACE_FAILED":
                return self._fail_round(
                    state,
                    round_index,
                    "SPECIALIST",
                    DirectorLedEngineError(str(
                        result_record.get("message")
                        or result_record.get("reason_code")
                        or "specialist interface failed"
                    )),
                )
            # The Host-owned wrapper is the stable handoff when a successful
            # specialist does not provide a separate artifact.  A separately
            # named deliverable is promoted only after it resolves to a file.
            stable_refs = []
            succeeded = _is_success_status(result_record.get("status"))
            if succeeded:
                stable_refs.append(result_ref)
            # A specialist handoff must name its deliverable explicitly.  The
            # Host-owned assignment path is an output constraint, not a
            # response-to-proof fallback: accepting it here would allow a
            # successful status (or a transport response) to promote an
            # artifact the specialist never handed off.
            explicit_ref = self._resolve_stable_ref(stable_ref)
            if succeeded and not explicit_ref:
                raise DirectorLedEngineError(
                    "specialist succeeded without a committed deliverable file"
                )
            if explicit_ref and explicit_ref != output_ref:
                raise DirectorLedEngineError(
                    "specialist deliverable_ref differs from Host assignment output_ref"
                )
            # Commit only a valid Host-bound handoff. Survey acquisition can
            # then resume from it without repeating the model call.
            _write_json(self.workdir / result_ref, result_record)
            if decision.agent_id == "survey" and succeeded and explicit_ref:
                from pace.runtime.output.sources import process_survey_sources

                # The model handoff is durable before file acquisition starts.
                # Recovery finishes processing without recalling Survey.
                source_refs = process_survey_sources(
                    workdir=self.workdir, report_ref=explicit_ref,
                    allow_network=result_record.get("external_retrieval_enabled") is True,
                )
                result_record["published_refs"] = list(_unique_strings((
                    *result_record.get("published_refs", ()), *source_refs,
                )))
                _write_json(self.workdir / result_ref, result_record)
            if explicit_ref and explicit_ref not in stable_refs:
                stable_refs.append(explicit_ref)
            if succeeded:
                for ref in result_record.get("published_refs", ()):
                    stable_refs.append(self._require_run_local_file_ref(ref, field="published_ref"))
            if decision.agent_id == "prover" and explicit_ref:
                state = self._replace_state(state, candidate_ref=explicit_ref)
            specialist_succeeded = succeeded and bool(explicit_ref)
            state = self._replace_state(
                state,
                phase=(
                    "VERIFY"
                    if decision.agent_id == "prover" and specialist_succeeded
                    else "DIRECTOR"
                ),
                pending_dispatch_ref="",
                pending_prompt_ref="",
                pending_assignment_ref="",
                pending_verifier_request_ref="",
                last_assignment_ref=assignment_ref,
                last_composer_manifest_ref=manifest_ref,
                last_specialist_result_ref=result_ref,
                diagnostics_ref="",
            )
            if stable_refs:
                state = self._replace_state(
                    state,
                    completed_result_refs=(*state.completed_result_refs, *(
                        ref for ref in stable_refs if ref not in state.completed_result_refs
                    )),
                )
            if self.commit_progress and specialist_succeeded:
                commit_memory_notes(self.workdir, result_refs=(result_ref,))
            self._persist_state(state)
            publish_progress(self.workdir, task_id=f'round-{round_index:04d}', parent_task_id=self.parent_task_id, stage=decision.agent_id, status='completed' if specialist_succeeded else 'failed', round_index=round_index)
            if decision.agent_id == "prover" and specialist_succeeded:
                if self.verifier is None:
                    return self._fail_round(
                        state,
                        round_index,
                        "VERIFY",
                        DirectorLedEngineError(
                            "Prover candidate cannot be verified: verifier is not configured"
                        ),
                    )
                state = self._run_verifier(
                    state,
                    decision=decision,
                    context=context,
                    result_record=result_record,
                    result_ref=result_ref,
                )
            return state
        except Exception as exc:
            # Host execution failed; retry the committed assignment without
            # reserving another task or spending another proof state.
            return self._fail_round(
                state,
                state.round_index,
                "SPECIALIST",
                exc,
            )

    def _recover_pending_specialist_value(
        self,
        *,
        result_ref: str,
        output_ref: str,
        agent_id: str,
        round_index: int,
        recovering: bool,
    ) -> tuple[bool, Mapping[str, Any] | None]:
        """Recover a pending call from its Host-bound round outputs.

        A prompt checkpoint authorizes execution, not completion. A bare output
        may be a partial write and must never become a successful handoff.
        """

        if not recovering:
            return False, None
        result_path = self.workdir / result_ref
        if result_path.is_symlink():
            raise DirectorLedEngineError(
                "pending specialist agent_result.json (result_ref) must not be a symlink"
            )
        if result_path.exists() or result_path.is_symlink():
            normalized_result_ref = self._require_round_local_file_ref(
                result_ref,
                field="pending specialist result_ref",
                round_index=round_index,
            )
            return True, _read_json(self.workdir / normalized_result_ref)

        output_path = self.workdir / output_ref
        directory = output_path.parent
        uncommitted = [
            path for path in (
                output_path,
                *(directory / name for name in (
                    "findings", "memory_note.md", "sources", "source_manifest.json",
                )),
            )
            if path.exists() or path.is_symlink()
        ]
        if uncommitted:
            # Cancellation can leave files before the Host handoff commits.
            # Retain that attempt without publishing its drafts on the retry.
            archive = directory / "interrupted_attempts"
            archive.mkdir(exist_ok=True)
            attempt = archive / f"attempt-{len(list(archive.iterdir())) + 1:04d}"
            attempt.mkdir()
            moved_refs = {}
            for path in uncommitted:
                files = path.rglob("*") if path.is_dir() else (path,)
                for file in files:
                    if file.is_file():
                        moved_refs[file.relative_to(self.workdir).as_posix()] = (
                            attempt / path.name / file.relative_to(path)
                            if path.is_dir() else attempt / path.name
                        ).relative_to(self.workdir).as_posix()
                path.replace(attempt / path.name)
            # A call can finish immediately before agent_result is committed.
            # Its original delivery must keep referring to that attempt.
            for record_path in directory.glob("calls/*/record.json"):
                record = _read_json(record_path)
                archived_ref = moved_refs.get(record.get("deliverable_ref"))
                if archived_ref:
                    record["deliverable_ref"] = archived_ref
                    _write_json(record_path, record)
        return False, None

    def _load_or_compose(
        self,
        *,
        manifest_ref: str,
        prompt_ref: str,
        decision: DirectorDecision,
        spec: Any,
        context: Mapping[str, Any],
        round_index: int,
        assembly: PromptAssembly,
        input_refs: tuple[str, ...],
        checkpoint_committed: bool = False,
    ) -> PromptComposerResult:
        """Restore a committed Composer result or perform it once."""

        manifest_path = self.workdir / manifest_ref
        prompt_path = self.workdir / prompt_ref
        if (
            checkpoint_committed
            and not manifest_path.is_file()
            and not prompt_path.is_file()
        ):
            raise DirectorLedEngineError(
                "committed Composer checkpoint is missing; refusing to recompose"
            )
        checkpoint_files_complete = manifest_path.is_file() and prompt_path.is_file()
        if checkpoint_files_complete or checkpoint_committed:
            # Checkpoint files can be replaced between process invocations;
            # resolve the generated refs through the same public round
            # boundary before reading their bytes.
            manifest_ref = self._require_round_local_file_ref(
                manifest_ref,
                field="composer_manifest_ref",
                round_index=round_index,
            )
            prompt_ref = self._require_round_local_file_ref(
                prompt_ref,
                field="prompt_ref",
                round_index=round_index,
            )
            manifest_path = self.workdir / manifest_ref
            prompt_path = self.workdir / prompt_ref
            if not manifest_path.is_file() or not prompt_path.is_file():
                raise DirectorLedEngineError(
                    "Composer checkpoint is incomplete; refusing to recompose"
                )
            manifest = _read_json(manifest_path)
            _validate_composer_manifest(
                manifest,
                decision=decision,
                round_index=round_index,
                prompt_ref=prompt_ref,
                output_ref=str(context.get("output_ref") or ""),
                directive_ref=self._round_ref(round_index, "director_decision.json"),
            )
            final_ref = str(manifest.get("final_prompt_ref") or prompt_ref)
            if final_ref != prompt_ref:
                raise DirectorLedEngineError(
                    "Composer checkpoint names a different final prompt"
                )
            prompt = prompt_path.read_text(encoding="utf-8")
            prompt = _validate_composer_prompt(
                prompt,
                source="committed Composer",
            )
            return PromptComposerResult(
                final_prompt=prompt,
                call_id=manifest["composer_call_id"],
            )

        from pace.runtime.prompts import append_skill_catalog

        composer_record = self._compose_specialist(
            decision=decision,
            spec=spec,
            context=context,
            round_index=round_index,
            manifest_ref=manifest_ref,
            prompt_ref=prompt_ref,
            assembly=assembly,
            input_refs=input_refs,
        )
        draft_map = self.workdir / context["output_ref"]
        draft_map = draft_map.parent / "recovery_drafts.json"
        if draft_map.is_file():
            composer_record = PromptComposerResult(
                final_prompt=composer_record.final_prompt
                + "\nUnverified partial drafts from interrupted attempts: "
                + draft_map.read_text(),
                call_id=composer_record.call_id,
            )
        # Add discovery after composition so it reaches the actual specialist,
        # even when the Composer condenses its inputs. Committed prompts above
        # are restored byte-for-byte; SMI owns its phase-specific resources.
        if decision.agent_id != "smi":
            composer_record = PromptComposerResult(
                final_prompt=append_skill_catalog(
                    composer_record.final_prompt,
                    workdir=self.workdir,
                ),
                call_id=composer_record.call_id,
            )
        # Add discovery after composition so it reaches the actual specialist,
        # even when the Composer condenses its inputs. Committed prompts above
        # are restored byte-for-byte; SMI owns its phase-specific resources.
        if decision.agent_id != "smi":
            composer_record = PromptComposerResult(
                final_prompt=append_skill_catalog(
                    composer_record.final_prompt, workdir=self.workdir,
                ),
                call_id=composer_record.call_id,
            )
        _validate_composer_prompt(composer_record.final_prompt, source="task prompt")
        _write_text(prompt_path, composer_record.final_prompt)
        _write_json(
            manifest_path,
            {
                "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                "round_index": round_index,
                "agent_id": decision.agent_id,
                "composer_call_id": composer_record.call_id,
                "final_prompt_ref": prompt_ref,
                "director_decision_ref": self._round_ref(
                    round_index, "director_decision.json"
                ),
                "output_artifact_ref": context["output_ref"],
                "assembly_source": "template",
            },
        )
        return composer_record

    def _compose_specialist(self, *, decision, spec, context, round_index,
                            manifest_ref, prompt_ref, assembly, input_refs):
        composer_value = _invoke(
            self.composer,
            {
                "workdir": self.workdir,
                "decision": decision,
                "agent_spec": spec,
                "context": context,
                "target_contract_id": context["target_contract_id"],
                "round_index": round_index,
                "output_ref": context["output_ref"],
                "manifest_ref": manifest_ref,
                "prompt_ref": prompt_ref,
                "assembly": assembly,
                "input_refs": input_refs,
            },
        )
        return _coerce_composer(composer_value)

    def _run_verifier(
        self,
        state: DirectorLedState,
        *,
        decision: DirectorDecision,
        context: Mapping[str, Any],
        result_record: Mapping[str, Any],
        result_ref: str,
        recovering: bool = False,
    ) -> DirectorLedState:
        try:
            if result_ref != state.last_specialist_result_ref:
                raise DirectorLedEngineError(
                    "verifier result_ref differs from committed specialist result"
                )
            verification_ref = self._round_ref(state.round_index, "verification/verification.json")
            request_ref = self._round_ref(state.round_index, "verification/request.json")
            request_path = self.workdir / request_ref
            verification_path = self.workdir / verification_ref
            committed_verification_ref = str(
                state.last_verification_ref or ""
            ).strip()
            pending_request_ref = str(
                state.pending_verifier_request_ref or ""
            ).strip()

            if committed_verification_ref:
                # A committed verification is authoritative only when state
                # names the canonical envelope.  A pending request marker and
                # a committed result are mutually exclusive state snapshots.
                if pending_request_ref:
                    raise DirectorLedEngineError(
                        "Director-led state has both pending and committed verification refs"
                    )
                committed_verification_ref = self._require_round_local_file_ref(
                    committed_verification_ref,
                    field="last_verification_ref",
                    round_index=state.round_index,
                )
                if committed_verification_ref != verification_ref:
                    raise DirectorLedEngineError(
                        "last_verification_ref differs from the current round"
                    )
                persisted = _read_json(self.workdir / committed_verification_ref)
                value = _validate_verification_checkpoint(
                    persisted,
                    expected_round_index=state.round_index,
                    expected_specialist_result_ref=state.last_specialist_result_ref,
                )
                return self._apply_verifier_value(
                    state,
                    value,
                    resolved=resolve_verification(
                        self.workdir, value, target_contract_id=state.target_contract_id,
                        candidate_ref=state.candidate_ref,
                    ),
                    verification_ref=committed_verification_ref,
                    context=context,
                )

            if pending_request_ref:
                # Recovery may reuse a verifier envelope only after the Host
                # committed this exact request binding in state.  A file at
                # the generated path alone is not sufficient: the specialist
                # can pre-write both request and verification files.
                pending_request_ref = self._require_round_local_file_ref(
                    pending_request_ref,
                    field="pending_verifier_request_ref",
                    round_index=state.round_index,
                )
                if pending_request_ref != request_ref:
                    raise DirectorLedEngineError(
                        "pending_verifier_request_ref differs from the current round"
                    )
                if not request_path.is_file():
                    raise DirectorLedEngineError(
                        "committed verifier request is missing"
                    )
                request = _read_json(request_path)
                _validate_verifier_request(
                    request,
                    expected_round_index=state.round_index,
                    expected_specialist_result_ref=result_ref,
                    expected_candidate_ref=state.candidate_ref,
                    expected_directive_ref=str(context.get("directive_ref") or ""),
                    expected_assignment_ref=str(context.get("assignment_ref") or ""),
                )
                if verification_path.exists() or verification_path.is_symlink():
                    self._require_round_local_file_ref(
                        verification_ref,
                        field="verification_ref",
                        round_index=state.round_index,
                    )
                    if not recovering:
                        raise DirectorLedEngineError(
                            "uncommitted verification checkpoint exists"
                        )
                    persisted = _read_json(verification_path)
                    value = _validate_verification_checkpoint(
                        persisted,
                        expected_round_index=state.round_index,
                        expected_specialist_result_ref=state.last_specialist_result_ref,
                    )
                    return self._apply_verifier_value(
                        state,
                        value,
                        resolved=resolve_verification(
                            self.workdir, value, target_contract_id=state.target_contract_id,
                            candidate_ref=state.candidate_ref,
                        ),
                        verification_ref=verification_ref,
                        context=context,
                    )
            else:
                # No Host request has been committed for this handoff.  Any
                # pre-existing request or envelope therefore belongs to an
                # untrusted specialist and must not be adopted.
                if verification_path.exists() or verification_path.is_symlink():
                    self._require_round_local_file_ref(
                        verification_ref,
                        field="verification_ref",
                        round_index=state.round_index,
                    )
                    raise DirectorLedEngineError(
                        "uncommitted verification checkpoint exists"
                    )
                if request_path.exists() or request_path.is_symlink():
                    self._require_round_local_file_ref(
                        request_ref,
                        field="verifier_request_ref",
                        round_index=state.round_index,
                    )
                    raise DirectorLedEngineError(
                        "uncommitted verifier request checkpoint exists"
                    )
                request = {
                    "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                    "round_index": state.round_index,
                    "specialist_result_ref": result_ref,
                    "candidate_ref": state.candidate_ref,
                    "directive_ref": str(context.get("directive_ref") or ""),
                    "assignment_ref": str(context.get("assignment_ref") or ""),
                }
                _validate_verifier_request(
                    request,
                    expected_round_index=state.round_index,
                    expected_specialist_result_ref=result_ref,
                    expected_candidate_ref=state.candidate_ref,
                    expected_directive_ref=str(context.get("directive_ref") or ""),
                    expected_assignment_ref=str(context.get("assignment_ref") or ""),
                )
                _write_json(request_path, request)
                # Persist the marker only after the request bytes are durable.
                # A crash before this state write leaves the request
                # uncommitted and recovery will refuse both files.
                state = self._replace_state(
                    state,
                    pending_verifier_request_ref=request_ref,
                )
                self._persist_state(state)
            value = _invoke(
                self.verifier,
                {
                    "workdir": self.workdir,
                    "decision": decision,
                    "context": context,
                    "result": result_record,
                    "result_ref": result_ref,
                    "round_index": state.round_index,
                },
            )
            resolved = resolve_verification(
                self.workdir, value, target_contract_id=state.target_contract_id,
                candidate_ref=state.candidate_ref,
            )
            if resolved["status"] == "VERIFIER_ERROR":
                if self.commit_progress:
                    commit_verified_progress(self.workdir, (value,))
                    commit_memory_notes(self.workdir, result_refs=(), verification_values=(value,))
                raise DirectorLedEngineError("verifier_interface_failed")
            checkpoint = {
                "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                "round_index": state.round_index,
                "specialist_result_ref": result_ref,
                "verification": value,
            }
            _write_json(self.workdir / verification_ref, checkpoint)
            value = _validate_verification_checkpoint(
                _read_json(self.workdir / verification_ref),
                expected_round_index=state.round_index,
                expected_specialist_result_ref=state.last_specialist_result_ref,
            )
            return self._apply_verifier_value(
                state,
                value,
                resolved=resolved,
                verification_ref=verification_ref,
                context=context,
            )
        except Exception as exc:
            return self._fail_round(state, state.round_index, "VERIFY", exc)

    def _apply_verifier_value(
        self,
        state: DirectorLedState,
        value: Any,
        *,
        resolved: Mapping[str, Any],
        verification_ref: str,
        context: Mapping[str, Any],
    ) -> DirectorLedState:
        state = self._replace_state(
            state,
            phase="DIRECTOR",
            pending_verifier_request_ref="",
            last_verification_ref=verification_ref,
        )
        verification_status = resolved["status"]
        if verification_status == "PASS":
            state = self._replace_state(
                state,
                phase="FINALIZE",
                terminal_result="PASS",
                stop_reason="Auditor and Kernel accepted the same candidate.",
            )
        elif verification_status != "FAILED":
            raise DirectorLedEngineError("verifier returned no recognized terminal status")
        if self.commit_progress and isinstance(value, Mapping):
            commit_verified_progress(self.workdir, (value,))
            commit_memory_notes(self.workdir, result_refs=(), verification_values=(value,))
        self._persist_state(state)
        publish_progress(self.workdir, task_id=f'round-{state.round_index:04d}-verification', stage='verification', round_index=state.round_index, status='completed' if verification_status == 'PASS' else 'needs_work', notice='candidate_checked' if verification_status == 'PASS' else 'candidate_repair' if state.phase == 'DIRECTOR' else 'candidate_unresolved')
        return state

    def _fail_round(
        self,
        state: DirectorLedState,
        round_index: int,
        phase: str,
        exc: Exception,
    ) -> DirectorLedState:
        safe_round = max(1, int(round_index or 1))
        diagnostic_ref = self._round_ref(safe_round, "diagnostics.json")
        diagnostic: dict[str, Any] = {
            "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
            "round_index": safe_round,
            "failure_owner": "HOST",
            "phase": str(phase or ""),
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        if state.phase in {"DIRECTOR", "PENDING_SPECIALIST", "PENDING_BATCH", "VERIFY"}:
            # The active boundary owns its exact next action, including any
            # already reserved budget and committed handoffs. Terminal search
            # decisions are not retryable operational checkpoints.
            diagnostic["resume_checkpoint"] = state.to_json()
        _write_json(
            self.workdir / diagnostic_ref,
            diagnostic,
        )
        next_state = self._replace_state(
            state,
            round_index=max(state.round_index, safe_round),
            phase="FAILED",
            pending_dispatch_ref="",
            pending_prompt_ref="",
            pending_assignment_ref="",
            pending_verifier_request_ref="",
            diagnostics_ref=diagnostic_ref,
            terminal_result="FAILED",
        )
        self._persist_state(next_state)
        task_id = f"round-{safe_round:04d}"
        if phase == "DIRECTOR":
            task_id += "-director"
        elif phase == "VERIFY":
            task_id += "-verification"
        publish_progress(self.workdir, task_id=task_id, stage={'DIRECTOR': 'director', 'VERIFY': 'verification'}.get(phase, 'research'), status='failed', round_index=safe_round, notice='stage_failed_final')
        return next_state

    def _round_ref(self, round_index: int, filename: str) -> str:
        return f"{stage_ref(int(round_index), workdir=self.workdir)}/{filename}"

    def _default_output_ref(self, agent_id: str, round_index: int) -> str:
        names = {
            "framer": "research_brief.json",
            "planner": "planner_deliverable.md",
            "survey": "survey_deliverable.md",
            "smi": "smi_deliverable.md",
            "research": "research_deliverable.md",
            "prover": "candidate_proof.md",
        }
        return self._round_ref(round_index, names.get(agent_id, "deliverable.md"))

    def _resolve_stable_ref(self, value: str) -> str:
        """Return a valid run-local file ref, or an empty value."""

        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            return ""
        # Public handoffs are deliberately portable relative refs.  Do not
        # normalize an absolute path merely because it happens to be inside
        # this workspace: a resumed run must reject that representation.
        if _unsafe_ref_syntax(raw):
            return ""
        normalized = "/".join(part for part in raw.split("/") if part)
        if not normalized:
            return ""
        normalized_path = Path(normalized)
        if is_call_path(normalized_path):
            return ""
        if normalized_path.name.startswith("private_"):
            return ""
        try:
            lexical = self.workdir / normalized
            if _path_has_symlink_component(self.workdir, lexical):
                return ""
            workspace_root = self.workdir.resolve()
            canonical = lexical.resolve()
            canonical_relative = canonical.relative_to(workspace_root)
        except (OSError, RuntimeError, ValueError):
            # A stable handoff must remain inside this run.  This also rejects
            # a public-looking symlink whose target is outside the workspace.
            return ""
        if _is_private_transport_path(canonical_relative):
            return ""
        if not canonical.is_file():
            return ""
        return normalized

    def _require_run_local_file_ref(self, value: str, *, field: str) -> str:
        """Validate a persisted reference before using it during recovery.

        Recovery state is an input boundary, so it cannot rely on the
        generated round layout alone.  In particular, absolute paths that
        happen to point inside the workspace are still rejected: persisted
        public references are deliberately relative and portable.
        """

        raw = str(value or "").strip().replace("\\", "/")
        path = Path(raw)
        if (
            _unsafe_ref_syntax(raw)
            or is_call_path(path)
            or path.name.startswith("private_")
        ):
            raise DirectorLedEngineError(
                f"{field} must be a public relative workspace reference"
            )
        normalized = "/".join(part for part in raw.split("/") if part)
        if not normalized:
            raise DirectorLedEngineError(
                f"{field} must be a public relative workspace reference"
            )
        lexical = self.workdir / normalized
        if _path_has_symlink_component(self.workdir, lexical):
            raise DirectorLedEngineError(f"{field} must not contain a symlink")
        try:
            workspace_root = self.workdir.resolve()
            resolved = lexical.resolve()
            canonical_relative = resolved.relative_to(workspace_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DirectorLedEngineError(
                f"{field} escapes the run workspace"
            ) from exc
        if _is_private_transport_path(canonical_relative):
            raise DirectorLedEngineError(
                f"{field} points to a private or transport artifact"
            )
        if not resolved.is_file():
            raise DirectorLedEngineError(f"{field} does not name a file: {normalized}")
        return normalized

    def _require_round_local_file_ref(
        self,
        value: str,
        *,
        field: str,
        round_index: int,
    ) -> str:
        """Require a recovery reference to remain inside its committed round.

        The generic reference check permits any public run-local file because
        public search context is intentionally cumulative.  Dispatch and
        verification checkpoints are different: their refs bind one exact
        round, so a cross-round path would allow a resumed state to pair a
        directive with another round's result.
        """

        if isinstance(round_index, bool) or int(round_index or 0) < 1:
            raise DirectorLedEngineError(
                f"{field} requires a positive Director round index"
            )
        try:
            normalized = self._require_run_local_file_ref(value, field=field)
        except DirectorLedEngineError as exc:
            if "must not contain a symlink" in str(exc):
                raise DirectorLedEngineError(
                    f"{field} must belong to Director round {int(round_index):04d}; "
                    "symlinks are not allowed"
                ) from exc
            raise
        # Compare canonical paths, not just the lexical ref.  A file under
        # round 0001 may be a symlink to round 0002 (or elsewhere) and must
        # not satisfy the round binding in that case.
        try:
            expected_root = (
                self.workdir / self.round_root / f"round{int(round_index)}"
            ).resolve()
            canonical = (self.workdir / normalized).resolve()
            canonical.relative_to(expected_root)
        except (OSError, RuntimeError, ValueError):
            raise DirectorLedEngineError(
                f"{field} must belong to Director round {int(round_index):04d}"
            ) from None
        return normalized

    def _load_state(self) -> DirectorLedState:
        if _path_has_symlink_component(self.workdir, self.state_path):
            raise DirectorLedEngineError(
                "Director-led state path must not contain a symlink"
            )
        try:
            return DirectorLedState.from_json(_read_json(self.state_path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DirectorLedEngineError("cannot load Director-led state") from exc

    def _persist_state(self, state: DirectorLedState) -> DirectorLedState:
        if _path_has_symlink_component(self.workdir, self.state_path):
            raise DirectorLedEngineError(
                "Director-led state path must not contain a symlink"
            )
        _write_json(self.state_path, state.to_json())
        return state

    def _replace_state(self, state: DirectorLedState, **changes: Any) -> DirectorLedState:
        return replace(state, **changes)

    def _result(self, state: DirectorLedState) -> DirectorLedRunResult:
        return DirectorLedRunResult(
            state=state,
            state_ref=self.state_ref,
            completed_result_refs=state.completed_result_refs,
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
    expected_fields = set(DIRECTOR_FIELDS) | {"schema_version", "round_index"}
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
    if value.get("schema_version") != DIRECTOR_PROTOCOL_SCHEMA_VERSION:
        raise DirectorProtocolError(
            "persisted director decision has an unsupported schema_version"
        )
    round_index = value.get("round_index")
    if isinstance(round_index, bool) or not isinstance(round_index, int):
        raise DirectorProtocolError("persisted director decision round_index must be an integer")
    if expected_round_index is not None and round_index != int(expected_round_index):
        raise DirectorProtocolError(
            "persisted director decision belongs to a different round"
        )
    payload = {field: value[field] for field in DIRECTOR_FIELDS}
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
        "assembly_source"
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

    for field_name, expected in (
        ("input_refs", expected_input_refs),
    ):
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
        missing = sorted(
            DIRECTOR_LED_VERIFIER_REQUEST_FIELDS.difference(actual_fields)
        )
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
        raise DirectorLedEngineError(
            "unsupported Director-led verifier request schema"
        )
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
        "composer_call_id",
        "final_prompt_ref",
        "director_decision_ref",
        "output_artifact_ref",
    }
    actual_fields = set(value)
    if not fields.issubset(actual_fields) or actual_fields - fields - {
        "assembly_source"
    }:
        missing = sorted(fields.difference(actual_fields))
        extra = sorted(actual_fields.difference(fields))
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
        raise DirectorLedEngineError(
            "Composer must return PromptComposerResult"
        )
    return value


def _coerce_specialist_result(value: Any, *, default_ref: str) -> tuple[dict[str, Any], str]:
    """Normalize an opaque specialist return and identify an explicit stable ref."""

    if isinstance(value, str):
        # A transport response is not a specialist artifact.  Keep only the
        # interface diagnosis in the public handoff; the adapter owns any
        # private response trace.
        return (
            {
                "status": "INTERFACE_FAILED",
                "failure_class": "HOST_INTERFACE",
                "reason_code": "specialist_result_not_object",
                "message": "specialist handoff must be an object with an explicit deliverable_ref",
            },
            "",
        )
    if isinstance(value, Mapping):
        record = {str(key): _json_value(item) for key, item in value.items()}
        # ``proof_ref`` and ``candidate_proof_ref`` belonged to removed
        # handoff shapes.  Treat their presence as an interface failure
        # instead of silently promoting either one to a deliverable.
        unsupported_aliases = tuple(
            field
            for field in ("proof_ref", "candidate_proof_ref")
            if field in record
        )
        if unsupported_aliases:
            record.update(
                {
                    "status": "INTERFACE_FAILED",
                    "failure_class": "HOST_INTERFACE",
                    "reason_code": "specialist_deliverable_ref_alias",
                    "message": (
                        "specialist handoff must use the explicit deliverable_ref; "
                        "unsupported aliases: " + ", ".join(unsupported_aliases)
                    ),
                }
            )
            return record, ""
        raw_status = record.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            record.update(
                {
                    "status": "INTERFACE_FAILED",
                    "failure_class": "HOST_INTERFACE",
                    "reason_code": "specialist_status_missing",
                    "message": "specialist handoff requires an explicit status",
                }
            )
            return record, ""
        status = raw_status.strip().upper()
        record["status"] = status
        if status != "SUCCEEDED":
            return record, ""

        raw_ref = record.get("deliverable_ref")
        stable = raw_ref.strip() if isinstance(raw_ref, str) else ""
        if not stable:
            record.update(
                {
                    "status": "INTERFACE_FAILED",
                    "failure_class": "HOST_INTERFACE",
                    "reason_code": "specialist_deliverable_ref_missing",
                    "message": "successful specialist handoff requires an explicit deliverable_ref",
                }
            )
            return record, ""
        return record, stable
    if value is None:
        return {"status": "FAILED", "message": "specialist returned no result"}, ""
    return {
        "status": "INTERFACE_FAILED",
        "failure_class": "HOST_INTERFACE",
        "reason_code": "specialist_result_type_invalid",
        "message": "specialist handoff must be a JSON object",
        "value_type": type(value).__name__,
    }, ""


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
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return handler(**dict(kwargs))
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return handler(**accepted)


def _string_value(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DirectorLedEngineError(f"state field {field} must be a string")
    if value and not value.strip():
        raise DirectorLedEngineError(
            f"state field {field} must not be whitespace-only"
        )
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


def _is_private_transport_path(path: Path) -> bool:
    """Return whether a canonical relative path names hidden transport data."""

    return is_call_path(path) or path.name.startswith("private_")


def _path_has_symlink_component(root: Path, path: Path) -> bool:
    """Return whether a path uses a symbolic-link component below ``root``.

    Resolving a reference and checking that its destination remains inside the
    run is insufficient for machine-owned checkpoints: an in-run alias can
    still substitute a different artifact while retaining the expected
    lexical reference.  Check each lexical component before resolution so
    canonical state and round identities remain stable.
    """

    root_path = Path(root)
    candidate = Path(path)
    try:
        relative = candidate.relative_to(root_path)
    except ValueError:
        return True
    current = root_path
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            # An unreadable component cannot establish a stable identity.
            return True
    return False


def _validate_specialist_result_agent_id(
    result_record: Mapping[str, Any],
    *,
    expected_agent_id: str,
) -> None:
    """Bind an optional specialist result identity to the dispatched agent.

    Specialist handoffs remain opaque apart from their scheduling fields, so
    older injected handlers may omit ``agent_id``.  Whenever a handler emits
    the field, however, it is a machine-visible identity claim and must match
    the committed Director decision exactly.
    """

    if "agent_id" not in result_record:
        return
    observed = result_record.get("agent_id")
    if not isinstance(observed, str) or observed != str(expected_agent_id):
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


def _safe_relative_ref(value: str, *, default: str) -> str:
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


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_json"):
        try:
            return _json_value(value.to_json())
        except Exception:
            pass
    if hasattr(value, "__dataclass_fields__"):
        try:
            from dataclasses import asdict

            return _json_value(asdict(value))
        except Exception:
            pass
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"type": type(value).__name__, "text": str(value)}
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


__all__ = [
    "DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION",
    "DIRECTOR_LED_PHASES",
    "DIRECTOR_LED_ROUND_SCHEMA_VERSION",
    "DIRECTOR_LED_STATE_SCHEMA_VERSION",
    "DirectorLedEngine",
    "DirectorLedEngineError",
    "DirectorLedRunResult",
    "DirectorLedState",
]
