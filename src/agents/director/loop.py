"""Schedule bounded assignments inside one research turn."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pace.agents.director.executor import AssignmentExecutor
from pace.agents.director.rounds import (
    _coerce_decision,
    _coerce_persisted_decision,
    _invoke,
    _read_json,
    _unique_strings,
    _validate_assignment,
    _validate_verification_checkpoint,
)
from pace.agents.director.state import (
    DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
    DIRECTOR_LED_ROUND_SCHEMA_VERSION,
    DirectorBudgetExhausted,
    DirectorLedEngineError,
    DirectorLedRunResult,
    DirectorLedState,
)
from pace.artifacts.context import (
    snapshot_verified_progress,
)
from pace.artifacts.delivery_pause import (
    DeliveryPaused,
    clear_delivery_pause,
    delivery_pause,
    pause_delivery,
)
from pace.artifacts.events import publish_progress
from pace.artifacts.results import FINDINGS_INDEX_REF, committed_round_reading_refs
from pace.artifacts.workspace import (
    _write_json_atomic as _write_json,
)
from pace.artifacts.workspace import path_has_symlink_component, work_ref
from pace.contracts.research.director_protocol import (
    DIRECTOR_PROTOCOL_SCHEMA_VERSION,
    DISPATCHABLE_AGENT_IDS,
    DirectorAction,
    DirectorDecision,
    validate_director_reference_bindings,
    validate_director_stop,
)
from pace.contracts.research.memory import check_binding

logger = logging.getLogger(__name__)


class DirectorLedEngine(AssignmentExecutor):
    """Run the Director decision loop; execution and independent checks are services."""

    def __init__(
        self,
        *,
        workdir: Path,
        director: Callable[..., Any],
        composer: Callable[..., Any],
        specialists: Mapping[str, Callable[..., Any]],
        target_contract_id: str = "",
        max_proof_states: int = 18,
        max_rounds: int | None = None,
        verifier: Callable[..., Any] | None = None,
        on_round_committed: Callable[..., Any] | None = None,
        reporter: Callable[[str], Any] | None = None,
        review_findings: Callable[..., bool] | None = None,
        commit_memory: Callable[..., Any] | None = None,
        commit_results: Callable[..., Any] | None = None,
    ) -> None:
        if not callable(director) or not callable(composer):
            raise TypeError("director and composer must be callable")
        if not specialists:
            raise ValueError("at least one specialist handler is required")
        unknown = set(specialists).difference(DISPATCHABLE_AGENT_IDS)
        if unknown:
            raise ValueError(f"unknown specialist handlers: {sorted(unknown)}")
        if not isinstance(target_contract_id, str):
            raise TypeError("target_contract_id must be a string")
        if target_contract_id and not target_contract_id.strip():
            raise ValueError("target_contract_id must not be whitespace-only")
        if type(max_proof_states) is not int:
            raise TypeError("max_proof_states must be an integer")
        if max_proof_states < 1:
            raise ValueError("max_proof_states must be positive")
        if max_rounds is not None and (type(max_rounds) is not int or max_rounds < 1):
            raise ValueError("max_rounds must be a positive integer")
        super().__init__(
            workdir=workdir,
            composer=composer,
            specialists=specialists,
            target_contract_id=target_contract_id,
            verifier=verifier,
            reporter=reporter,
            commit_memory=commit_memory,
        )
        self.director = director
        self.max_proof_states = max_proof_states
        self.max_rounds = max_rounds
        self.on_round_committed = on_round_committed
        self.review_findings = review_findings
        self.commit_results_callback = commit_results

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
        if resume and (
            state.phase in {"FINALIZE", "STOPPED"}
            or (state.phase == "DIRECTOR" and state.round_index > 0)
        ):
            # The process may have stopped after the parent checkpoint, before
            # evidence review. Finish it before the Director can choose again.
            paused = delivery_pause(self.workdir)
            self._commit_round_evidence(
                state,
                trigger=paused.get("trigger") or "recovery-findings",
                retry_errors=True,
            )
        elif self.commit_progress:
            # Pending assignments and VERIFY resume their own frozen transaction.
            # In particular, do not review a Prover draft before the whole verdict.
            if self.commit_memory_callback is not None:
                self.commit_memory_callback(
                    workdir=self.workdir, result_refs=state.completed_result_refs
                )

        if state.phase in {"STOPPED", "FINALIZE", "FAILED"}:
            return self._result(state)
        if state.phase == "PENDING_BATCH":
            from pace.agents.director.parallel import run_batch

            state = run_batch(self, state, control_context=control_context)
            self._publish_committed_round(
                state, trigger_id=f"batch-{state.round_index:04d}", retry_errors=True
            )
        if state.phase == "VERIFY":
            if self.verifier is None:
                state = self._fail_round(
                    state,
                    state.round_index,
                    "VERIFY",
                    DirectorLedEngineError(
                        "Prover candidate cannot be verified: verifier is not configured"
                    ),
                )
                return self._result(state)
            state = self._resume_verification(state, control_context=control_context)
            self._publish_committed_round(
                state,
                trigger_id=f"round-{state.round_index:04d}-verification",
                retry_errors=True,
            )
            if state.phase != "DIRECTOR":
                return self._result(state)
        if state.phase == "PENDING_SPECIALIST":
            state = self._run_pending_specialist(
                state,
                control_context=control_context,
                recovering=resume,
            )
            self._publish_committed_round(
                state, trigger_id=f"round-{state.round_index:04d}", retry_errors=True
            )

        while state.phase == "DIRECTOR":
            from pace.runtime.sessions import budget_exhausted

            reason = budget_exhausted(self.workdir)
            if reason and resume:
                # Replay an accepted dispatch even if its Director call used the
                # last allowance before the parent could commit the assignment.
                next_decision = self._round_ref(
                    state.round_index + 1, "director_decision.json"
                )
                if (self.workdir / next_decision).is_file():
                    reason = ""
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
                if state.round_index == 0:
                    self._commit_round_evidence(state, trigger="final-findings")
                break
            state = self._run_round(
                state,
                control_context=control_context,
                recovering=resume,
            )
            self._publish_committed_round(
                state, trigger_id=f"round-{state.round_index:04d}"
            )

        return self._result(state)

    def _commit_round_evidence(
        self,
        state: DirectorLedState,
        *,
        trigger: str,
        retry_errors: bool = False,
    ) -> None:
        """Make parent-committed evidence available to the next Director decision."""
        if not self.commit_progress or state.phase == "FAILED":
            return
        try:
            self._publish_round_progress(state)
            if state.terminal_result == "PASS":
                # The accepted literal target and Kernel publication decision own
                # terminal delivery; optional route certificates stay unauthorised.
                clear_delivery_pause(self.workdir)
                return
            if self.review_findings is not None:
                complete = self.review_findings(
                    workdir=self.workdir,
                    completed_result_refs=state.completed_result_refs,
                    retry_errors=retry_errors,
                )
                if not complete:
                    raise DeliveryPaused(
                        "Finding verification is incomplete; resume the saved task"
                    )
            if self.commit_results_callback is not None:
                self.commit_results_callback(
                    workdir=self.workdir,
                    completed_result_refs=state.completed_result_refs,
                    accepted_candidate_ref=state.candidate_ref
                    if state.terminal_result == "PASS"
                    else "",
                )
        except Exception as exc:
            pause_delivery(
                self.workdir,
                trigger=trigger,
                round_index=state.round_index,
                stage="findings",
            )
            if isinstance(exc, DeliveryPaused):
                raise
            logger.exception("Could not commit research evidence after %s", trigger)
            raise DeliveryPaused(f"Research evidence update paused: {exc}") from exc
        clear_delivery_pause(self.workdir)

    def _committed_round_members(self, state: DirectorLedState):
        """Return the authors and verdicts accepted by the latest parent checkpoint."""
        if state.stop_reason == "PROOF_STATE_BUDGET_EXHAUSTED":
            return []
        members = [
            (
                state.round_index,
                state.last_specialist_result_ref,
                state.last_verification_ref,
            )
        ]
        if state.last_batch_result_ref:
            ref = self._require_run_local_file_ref(
                state.last_batch_result_ref, field="last_batch_result_ref"
            )
            check_binding(
                self.workdir, {"ref": ref, "sha256": state.last_batch_result_sha256}
            )
            batch = _read_json(self.workdir / ref)
            tasks = batch["tasks"]
            first = tasks[0]["round_index"]
            if (
                ref != self._round_ref(first, "batch_result.json")
                or not first <= state.round_index <= batch["reserved_through_round"]
                or [t["round_index"] for t in tasks]
                != list(range(first, batch["reserved_through_round"] + 1))
            ):
                raise DirectorLedEngineError(
                    "batch evidence differs from committed round"
                )
            members = [
                (t["round_index"], t["result_ref"], t["verification_ref"])
                for t in tasks
            ]
        committed = []
        for index, result_ref, verification_ref in members:
            if result_ref not in state.completed_result_refs:
                if verification_ref:
                    raise DirectorLedEngineError("uncommitted author for verification")
                continue
            if result_ref != self._round_ref(index, "agent_result.json"):
                raise DirectorLedEngineError(
                    "committed result belongs to another round"
                )
            if verification_ref and verification_ref != self._round_ref(
                index, "verification/verification.json"
            ):
                raise DirectorLedEngineError(
                    "committed verification belongs to another round"
                )
            committed.append((index, result_ref, verification_ref))
        return committed

    def _publish_round_progress(self, state: DirectorLedState) -> None:
        """Project notes and verified progress from the exact committed round or batch."""
        results = []
        verdicts = []
        current = ""
        for index, result_ref, verification_ref in self._committed_round_members(state):
            results.append(result_ref)
            if verification_ref:
                path = self._require_round_local_file_ref(
                    verification_ref, field="verification_ref", round_index=index
                )
                verdict = _validate_verification_checkpoint(
                    _read_json(self.workdir / path),
                    expected_round_index=index,
                    expected_specialist_result_ref=result_ref,
                )
                verdicts.append(verdict)
                if verification_ref == state.last_verification_ref:
                    current = verdict.get("verified_progress_ref", "")
        if self.commit_memory_callback is not None:
            self.commit_memory_callback(
                workdir=self.workdir,
                result_refs=tuple(
                    dict.fromkeys((*state.completed_result_refs, *results))
                ),
                verification_values=verdicts,
                current_progress_ref=current,
            )

    def _publish_committed_round(
        self,
        state: DirectorLedState,
        *,
        trigger_id: str,
        retry_errors: bool = False,
    ) -> None:
        """Complete the evidence handoff before notifying optional display consumers."""
        self._commit_round_evidence(
            state, trigger=trigger_id, retry_errors=retry_errors
        )
        if self.on_round_committed is None or int(state.round_index or 0) < 1:
            return
        try:
            self.on_round_committed(
                trigger_id=trigger_id,
                round_index=state.round_index,
                input_refs=_unique_strings(
                    (
                        *self._director_input_refs(state),
                        *state.completed_result_refs,
                    )
                ),
                completed_result_refs=state.completed_result_refs,
                state=state,
            )
        except DeliveryPaused:
            # The callback ran after a committed boundary. Pause without
            # changing the mathematical state or consuming another dispatch.
            raise
        except Exception:  # noqa: BLE001 - reporting cannot alter proof flow
            logger.warning("Round notification callback failed", exc_info=True)

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
        saved_inputs = None
        try:
            batch_path = round_dir / "batch.json"
            if recovering and batch_path.is_file():
                from pace.agents.director.parallel import run_batch

                self._require_round_local_file_ref(
                    self._round_ref(round_index, "batch.json"),
                    field="batch manifest",
                    round_index=round_index,
                )
                manifest = _read_json(batch_path)
                if (
                    manifest["first_round"] != round_index
                    or manifest["input_snapshot"] != state.to_json()
                ):
                    raise DirectorLedEngineError(
                        "batch reservation differs from the parent checkpoint"
                    )
                reserved = DirectorLedState.from_json(manifest["reserved_state"])
                self._persist_state(reserved)
                return run_batch(self, reserved, control_context=control_context)
            if path_has_symlink_component(self.workdir, round_dir):
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
                    "assignment/context.json",
                    "assignment/route_context.json",
                    "progress_snapshot.json",
                    "diagnostics.json",
                }
                unexpected = sorted(
                    entry
                    for entry in entries
                    if entry not in allowed_entries
                    and entry != "README.md"
                    and not entry.startswith(("calls/", "assignment/snapshot/"))
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
                    raise DirectorLedEngineError(
                        "dispatch decision has no input snapshot"
                    )
                if assignment_path.is_file() and not decision_path.is_file():
                    raise DirectorLedEngineError("dispatch assignment has no decision")
                if context_path.is_file():
                    try:
                        persisted_context = _read_json(context_path)
                    except (
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as exc:
                        raise DirectorLedEngineError(
                            "uncommitted Director context is invalid"
                        ) from exc
                    expected_context = {
                        "schema_version": DIRECTOR_LED_ROUND_SCHEMA_VERSION,
                        "round_index": round_index,
                        "target_contract_id": self.target_contract_id
                        or state.target_contract_id,
                        "completed_result_refs": list(state.completed_result_refs),
                        "previous_verification_ref": state.last_verification_ref,
                        "control_context": str(control_context or ""),
                    }
                    saved_inputs = persisted_context.pop("decision_inputs", None)
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
        proof_states_remaining = state.max_proof_states - state.proof_states_used
        research_rounds_remaining = (
            None if self.max_rounds is None else self.max_rounds - state.round_index
        )
        try:
            # Context, decision, and assignment are one Host-owned dispatch
            # transaction.  A write failure must become a committed round
            # diagnostic so the pipeline can still publish a terminal report.
            reuse_decision = recovering and (self.workdir / directive_ref).is_file()
            publish_progress(
                self.workdir,
                task_id=f"round-{round_index:04d}-director",
                stage="director",
                status="started",
                round_index=round_index,
            )
            snapshot_ref = self._round_ref(round_index, "progress_snapshot.json")
            if reuse_decision:
                decision = _coerce_persisted_decision(
                    _read_json(self.workdir / directive_ref),
                    expected_round_index=round_index,
                )
            else:
                from pace.agents.director.context import director_decision_inputs

                inputs = director_decision_inputs(
                    self.workdir,
                    self._director_input_refs(state),
                    state.completed_result_refs,
                    assembling=proof_states_remaining == 0
                    or bool(state.last_verification_ref),
                )
                inputs.update(
                    proof_states_remaining=proof_states_remaining,
                    research_rounds_remaining=research_rounds_remaining,
                )
                if saved_inputs is not None:
                    # Cost events may have advanced. Evidence and permissions stay
                    # bound to the original target, route, obligations and sources.
                    # New versions may surface more committed evidence. A pending
                    # call keeps its original, still-authorized reading list.
                    allowed = set(inputs["required_refs"]) | set(inputs["archive_refs"])
                    if not allowed.issuperset(
                        (*saved_inputs["required_refs"], *saved_inputs["archive_refs"])
                    ):
                        raise DirectorLedEngineError(
                            "saved Director input is no longer available"
                        )
                    for key in inputs.keys() - {
                        "brief",
                        "required_refs",
                        "archive_refs",
                    }:
                        if inputs[key] != saved_inputs[key]:
                            raise DirectorLedEngineError(
                                f"saved Director input changed: {key}"
                            )
                    inputs = saved_inputs
                context["decision_inputs"] = inputs
                _write_json(self.workdir / context_ref, context)

                def save_decision(payload):
                    accepted = _coerce_decision(payload)
                    validate_director_reference_bindings(accepted, self.workdir)
                    validate_director_stop(
                        accepted,
                        proof_states_remaining=proof_states_remaining,
                        research_rounds_remaining=research_rounds_remaining,
                    )
                    _write_json(
                        self.workdir / directive_ref,
                        {
                            "schema_version": "research_director_decision/v3"
                            if accepted.next_step_kind
                            else DIRECTOR_PROTOCOL_SCHEMA_VERSION,
                            "round_index": round_index,
                            **accepted.to_json(),
                        },
                    )
                    return accepted

                raw_decision = _invoke(
                    self.director,
                    {
                        "workdir": self.workdir,
                        "round_index": round_index,
                        "target_contract_id": context["target_contract_id"],
                        "input_refs": tuple(inputs["required_refs"]),
                        "decision_inputs": inputs,
                        "on_accept": save_decision,
                        "completed_result_refs": state.completed_result_refs,
                        "proof_states_remaining": proof_states_remaining,
                        "research_rounds_remaining": research_rounds_remaining,
                        "control_context": str(control_context or ""),
                        "context": context,
                    },
                )
                if (self.workdir / directive_ref).is_file():
                    decision = _coerce_persisted_decision(
                        _read_json(self.workdir / directive_ref),
                        expected_round_index=round_index,
                    )
                    if decision != _coerce_decision(raw_decision):
                        raise DirectorLedEngineError(
                            "Director result differs from the accepted decision"
                        )
                else:
                    decision = save_decision(raw_decision)
            if reuse_decision:
                validate_director_reference_bindings(decision, self.workdir)
                validate_director_stop(
                    decision,
                    proof_states_remaining=proof_states_remaining,
                    research_rounds_remaining=research_rounds_remaining,
                )
        except Exception as exc:
            return self._fail_round(state, round_index, "DIRECTOR", exc)

        try:
            if decision.action is DirectorAction.DISPATCH:
                snapshot_verified_progress(self.workdir, snapshot_ref=snapshot_ref)
            if decision.parallel_tasks:
                from pace.agents.director.parallel import (
                    reserve_batch,
                    run_batch,
                )

                state = reserve_batch(self, state, decision)
                publish_progress(
                    self.workdir,
                    task_id=f"round-{round_index:04d}-director",
                    stage="director",
                    status="completed",
                    round_index=round_index,
                )
                return run_batch(self, state, control_context=control_context)
            if decision.action is DirectorAction.DISPATCH:
                self._check_task_budget((decision,), state)
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
                        expected_input_refs=self._assignment_input_refs(
                            decision, round_index
                        ),
                    )
                _write_json(
                    self.workdir / assignment_ref,
                    {
                        "schema_version": DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
                        "round_index": round_index,
                        "agent_id": decision.agent_id,
                        "prompt_brief": decision.prompt_brief,
                        "input_refs": list(
                            self._assignment_input_refs(decision, round_index)
                        ),
                        "output_ref": output_ref,
                        "director_decision_ref": directive_ref,
                        **self._context_binding(round_index),
                    },
                )
            else:
                assignment_ref = ""
            state = self._replace_state(
                state,
                round_index=round_index,
                proof_states_used=state.proof_states_used
                + self._proof_state_cost(decision, state),
                phase="PENDING_SPECIALIST"
                if decision.action is DirectorAction.DISPATCH
                else "STOPPED",
                pending_dispatch_ref=directive_ref
                if decision.action is DirectorAction.DISPATCH
                else "",
                pending_prompt_ref="",
                pending_assignment_ref=assignment_ref,
                pending_verifier_request_ref="",
                last_assignment_ref="",
                last_directive_ref=directive_ref,
                last_composer_manifest_ref="",
                last_specialist_result_ref="",
                last_batch_result_ref="",
                last_batch_result_sha256="",
                last_verification_ref="",
                diagnostics_ref="",
                candidate_ref="",
                stop_reason=""
                if decision.action is DirectorAction.DISPATCH
                else "DIRECTOR_STOP",
                terminal_result=""
                if decision.action is DirectorAction.DISPATCH
                else "FAILED",
            )
            self._persist_state(state)
        except DirectorBudgetExhausted:
            state = self._replace_state(
                state,
                round_index=round_index,
                phase="FINALIZE",
                stop_reason="PROOF_STATE_BUDGET_EXHAUSTED",
                terminal_result="FAILED",
            )
            self._persist_state(state)
            publish_progress(
                self.workdir,
                task_id=f"round-{round_index:04d}-director",
                stage="director",
                status="completed",
                round_index=round_index,
                notice="search_stopped",
            )
            return state
        except Exception as exc:
            return self._fail_round(
                state, max(state.round_index, round_index), "DIRECTOR", exc
            )
        if decision.action is DirectorAction.STOP:
            publish_progress(
                self.workdir,
                task_id=f"round-{round_index:04d}-director",
                stage="director",
                status="completed",
                round_index=round_index,
            )
            return state

        publish_progress(
            self.workdir,
            task_id=f"round-{round_index:04d}-director",
            stage="director",
            status="completed",
            round_index=round_index,
        )
        return self._run_pending_specialist(state, control_context=control_context)

    def _director_input_refs(self, state: DirectorLedState) -> tuple[str, ...]:
        """Start scheduling from current navigation and the latest evidence.

        The state ledger and persistent directory retain all historical refs.
        They need not be repeated as reading suggestions on every call.
        """

        refs = [
            work_ref(self.workdir, "input/request.md"),
            work_ref(self.workdir, "input/problem.md"),
            work_ref(self.workdir, "input/target_contract.json"),
            *(
                (work_ref(self.workdir, "input/handoff.md"),)
                if (self.workdir / work_ref(self.workdir, "input/handoff.md")).is_file()
                else ()
            ),
            work_ref(self.workdir, "state/proof_loop_budget.json"),
            work_ref(self.workdir, "state/director_state.json"),
            *(
                (FINDINGS_INDEX_REF,)
                if (self.workdir / FINDINGS_INDEX_REF).is_file()
                else ()
            ),
            "memory/README.md",
            "memory/active_problem_dossier.json",
            state.last_specialist_result_ref,
            state.last_verification_ref,
            state.diagnostics_ref,
        ]
        for _, result_ref, verification_ref in self._committed_round_members(state):
            refs.extend(committed_round_reading_refs(self.workdir, result_ref))
            if verification_ref:
                refs.append(verification_ref)
                verification = _read_json(self.workdir / verification_ref)[
                    "verification"
                ]
                refs.append(verification.get("repair_context_ref", ""))
        return _unique_strings(ref for ref in refs if isinstance(ref, str) and ref)

    def _proof_state_cost(
        self, decision: DirectorDecision, state: DirectorLedState
    ) -> int:
        if decision.action is not DirectorAction.DISPATCH:
            return 0
        return 1 if decision.agent_id in {"research", "rci"} else 0

    def _check_task_budget(
        self, tasks: tuple[DirectorDecision, ...], state: DirectorLedState
    ) -> None:
        cost = sum(self._proof_state_cost(task, state) for task in tasks)
        if state.proof_states_used + cost > state.max_proof_states:
            raise DirectorBudgetExhausted("insufficient proof states for this batch")

    def _result(self, state: DirectorLedState) -> DirectorLedRunResult:
        return DirectorLedRunResult(
            state=state,
            state_ref=self.state_ref,
            completed_result_refs=state.completed_result_refs,
        )
