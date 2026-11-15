"""Bounded agent-owned SMI research pipeline.

Mathematical roles own every mathematical judgment and artifact body.  This
module owns only the fixed phase graph, bounded counters, action routing,
reference identity, original-file handoffs, and append-only persistence.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pace.runtime.output.workspace import (
    _write_json_atomic as _write_json,
    work_ref,
)

from .phase_runner import (
    BudgetedPhaseRunner,
    SmiBudgetExhausted,
    SmiPhaseError,
    SmiPhaseResult,
)
from .roles import PHASE_ACTIONS, SKILL_ROOT
from .context import scope_snapshot, scope_matches, objection_key

MAX_ACTIVE_CANDIDATES = 2
MAX_EXPLORER_CALLS = 4
MAX_COVERAGE_CALLS = 4
MAX_MECHANISM_IDS = 3
MAX_MECHANISM_REPAIRS = 3
MAX_GATE_CYCLES = 3
MAX_CHILD_TASKS = 2
_SEED_REPAIR_CARD = f"{SKILL_ROOT}/raw_seed/raw_seed_repair.md"
_MECHANISM_REPAIR_CARD = f"{SKILL_ROOT}/mechanism/mechanism_repair.md"
_MECHANISM_INTERFACE_CARD = (
    f"{SKILL_ROOT}/multi_mechanism/capability_interface_contract.md"
)
_FRONTIER_SUPPORT_CARD = f"{SKILL_ROOT}/multi_mechanism/targeted_support_generation.md"
EXPLORER_ACTIONS = PHASE_ACTIONS["FRONTIER_EXPLORER"]


class SmiPipelineProtocolError(RuntimeError):
    """A prompt-owned payload cannot drive the declared phase transition."""

    failure_owner = "MODEL_INTERFACE"

    def __init__(self, message: str, *, phase: str, reason_code: str) -> None:
        super().__init__(message)
        self.phase = str(phase)
        self.reason_code = str(reason_code)


class PhaseRunner(Protocol):
    def run(
        self,
        *,
        phase: str,
        workdir: Path,
        call_dir: Path,
        target_contract_id: str,
        input_refs: Mapping[str, str | Path | Iterable[str | Path]],
        prompt_seed: str,
        skill_card_refs: Iterable[str] = (),
        round_index: int = 0,
        allowed_actions: Sequence[str] = (),
        routing_context: Mapping[str, Any] | None = None,
    ) -> SmiPhaseResult: ...


@dataclass
class _LineageBudget:
    split_used: bool = False
    challenger_repair_used: bool = False
    coverage_hardening_used: bool = False
    blind_hardening_used: bool = False
    normalizer_used: bool = False
    gate_cycles: int = 0
    blind_solutions: int = 0


@dataclass
class _Mechanism:
    mechanism_id: str
    ref: str
    receipt_ref: str = ""


@dataclass
class _Candidate:
    candidate_id: str
    root: Path
    lineage: _LineageBudget
    candidate_ref: str = ""
    candidate_receipt_ref: str = ""
    revision: int = 0
    explorer_calls: int = 0
    coverage_calls: int = 0
    mechanism_ids_used: int = 0
    mechanism_repairs: int = 0
    promoted: list[_Mechanism] = field(default_factory=list)
    coverage_ref: str = ""
    coverage_action: str = ""
    selected_mechanism_ids: tuple[str, ...] = ()
    challenge_ref: str = ""
    frontier_probe_ref: str = ""
    internal_probe_refs: tuple[str, ...] = ()
    frontier_probe_revision: int = 0
    repair_brief_refs: tuple[str, ...] = ()
    phase_calls: dict[str, int] = field(default_factory=dict)
    split_generation: int = 0
    extra_probe_continuation_used: bool = False
    required_explorer_action: str = ""


@dataclass(frozen=True)
class _TerminalCandidate:
    state: _Candidate
    task_kind: str
    trigger_refs: tuple[str, ...]


@dataclass(frozen=True)
class _DriveResult:
    terminals: tuple[_TerminalCandidate, ...] = ()
    children: tuple[_Candidate, ...] = ()
    reason: str = ""


class SmiPipeline:
    """Run the fixed twelve-role SMI phase graph for one ready work item."""

    def __init__(self, *, phase_runner: PhaseRunner) -> None:
        self.phase_runner = phase_runner
        self._candidate_ordinal = 0

    def run(self, **kwargs) -> str:
        """Enforce the shared soft budget at every new SMI phase boundary."""
        delegate = self.phase_runner
        root, directory = Path(kwargs["workdir"]), Path(kwargs["round_root"])
        self.phase_runner = BudgetedPhaseRunner(
            delegate, root=root, directory=directory
        )
        try:
            return self._run(**kwargs)
        except SmiBudgetExhausted as exc:
            return self._commit_outcome(
                workdir=root,
                artifact_root=directory,
                work_item_id=kwargs["work_item_id"],
                outcome="NO_ACCEPTED_TASK",
                reasons=(str(exc),),
            )
        finally:
            self.phase_runner = delegate

    def _run(
        self,
        *,
        workdir: Path,
        round_root: Path,
        target_contract_id: str,
        work_item_id: str,
        target_anchor_ref: str,
        work_item_ref: str,
        verified_progress_ref: str = "",
        source_refs: Sequence[str] = (),
        independent_source_refs: Sequence[str] = (),
        blocked_context_refs: Sequence[str] = (),
        prior_accepted_refs: Sequence[str] = (),
        prior_batch_refs: Sequence[str] = (),
        research_operation: str = "NEW_PROBLEM",
        research_problem_ref: str = "",
        child_task_capacity: int = MAX_CHILD_TASKS,
        round_index: int = 0,
    ) -> str:
        """Run one dispatch and commit exactly one round outcome."""

        self.research_operation = research_operation
        self.continued_ref = research_problem_ref
        self.prior_refs = tuple(prior_accepted_refs)
        self.independent_source_refs = tuple(independent_source_refs)
        if research_operation not in {"NEW_PROBLEM", "CONTINUE_PROBLEM"}:
            raise ValueError("unsupported SMI research operation")
        if research_operation == "CONTINUE_PROBLEM":
            research_problem_ref = _file_ref(
                research_problem_ref, Path(workdir), phase="FRONTIER_EXPLORER"
            )
            self.continued_ref = research_problem_ref
        self.round_index = round_index
        self.work_item_ref = work_item_ref
        root = Path(workdir)
        artifact_root = Path(round_root)
        artifact_root.mkdir(parents=True, exist_ok=True)
        self._candidate_ordinal = 0
        if child_task_capacity < 1:
            raise ValueError("SMI requires positive child_task_capacity")
        capacity = min(MAX_CHILD_TASKS, int(child_task_capacity))
        candidate_failures: list[str] = []
        terminal_candidates: list[_TerminalCandidate] = []
        no_task_reasons: list[str] = []

        initial = self._new_candidate(
            artifact_root=artifact_root,
            lineage=_LineageBudget(),
        )
        self.scope_records = []
        self.scope_objections = {}
        self.scope_objection_refs = []
        for ref in source_refs:
            if Path(ref).suffix != ".json":
                continue
            try:
                data = json.loads((root / ref).read_text())
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            verdict = data.get("verdict")
            if data.get("action") in {"REPAIR", "REJECT"} or (
                isinstance(verdict, dict) and verdict.get("stage_status") == "MATH_FAIL"
            ):
                key, content = objection_key(root, ref)
                self.scope_objections[key] = content
                self.scope_objection_refs.append(ref)
        self.prior_scope_records = []
        for ref in prior_batch_refs:
            saved = json.loads((root / ref).read_text())
            self.prior_scope_records.extend(saved.get("dependency_scope_reviews", ()))
        try:
            if research_operation == "CONTINUE_PROBLEM":
                reviewed_seed_ref, seed_reason = research_problem_ref, ""
                initial.candidate_ref = research_problem_ref
            else:
                reviewed_seed_ref, seed_reason = self._review_seed(
                    state=initial,
                    workdir=root,
                    target_contract_id=target_contract_id,
                    target_anchor_ref=target_anchor_ref,
                    work_item_ref=work_item_ref,
                    source_refs=tuple(source_refs),
                    verified_progress_ref=verified_progress_ref,
                    blocked_context_refs=tuple(blocked_context_refs),
                )
        except (SmiPhaseError, SmiPipelineProtocolError) as exc:
            failure_ref = self._record_failure(root, initial, exc)
            if missing_reason := _missing_input_reason(exc):
                # A missing predecessor is a recoverable transport condition.
                # Keep the diagnostic in the round and let the enclosing
                # research loop retry or move to the next bounded round.
                return self._commit_outcome(
                    workdir=root,
                    artifact_root=artifact_root,
                    work_item_id=work_item_id,
                    outcome="NO_ACCEPTED_TASK",
                    failure_refs=(failure_ref,),
                    reasons=(missing_reason,),
                )
            return self._commit_outcome(
                workdir=root,
                artifact_root=artifact_root,
                work_item_id=work_item_id,
                outcome="INTERFACE_FAILED",
                failure_refs=(failure_ref,),
                reasons=("seed_interface_failed",),
            )
        if not reviewed_seed_ref:
            return self._commit_outcome(
                workdir=root,
                artifact_root=artifact_root,
                work_item_id=work_item_id,
                outcome="NO_ACCEPTED_TASK",
                reasons=(seed_reason or "seed_rejected",),
            )

        queue: deque[_Candidate] = deque((initial,))
        split_occurred = False
        blocking_candidate_failure = False
        while queue:
            state = queue.popleft()
            try:
                driven = self._drive_candidate(
                    state=state,
                    workdir=root,
                    target_contract_id=target_contract_id,
                    target_anchor_ref=target_anchor_ref,
                    reviewed_seed_ref=reviewed_seed_ref,
                    source_refs=tuple(source_refs),
                    blocked_context_refs=tuple(blocked_context_refs),
                )
            except (SmiPhaseError, SmiPipelineProtocolError) as exc:
                failure_ref = self._record_failure(root, state, exc)
                candidate_failures.append(failure_ref)
                if _missing_input_reason(exc):
                    # The candidate cannot run this phase without its
                    # predecessor, but the round can still process sibling
                    # candidates and commit any usable task later.
                    continue
                blocking_candidate_failure = True
                if not split_occurred:
                    return self._commit_outcome(
                        workdir=root,
                        artifact_root=artifact_root,
                        work_item_id=work_item_id,
                        outcome="INTERFACE_FAILED",
                        failure_refs=tuple(candidate_failures),
                        reasons=("candidate_interface_failed",),
                    )
                continue
            if driven.children:
                split_occurred = True
                queue.extend(driven.children)
            terminal_candidates.extend(driven.terminals)
            if driven.reason:
                no_task_reasons.append(driven.reason)

        if not terminal_candidates:
            outcome = (
                "INTERFACE_FAILED" if blocking_candidate_failure else "NO_ACCEPTED_TASK"
            )
            return self._commit_outcome(
                workdir=root,
                artifact_root=artifact_root,
                work_item_id=work_item_id,
                outcome=outcome,
                failure_refs=tuple(candidate_failures),
                reasons=tuple(no_task_reasons) or ("bounded_search_produced_no_task",),
            )

        tasks = [
            self._task_record(
                terminal=terminal,
                task_ordinal=index,
                target_anchor_ref=target_anchor_ref,
                work_item_id=work_item_id,
            )
            for index, terminal in enumerate(terminal_candidates, start=1)
        ]
        open_tasks = [item for item in tasks if item["task_kind"] == "OPEN_PROBLEM"]
        resolutions = [
            item for item in tasks if item["task_kind"] == "RESOLUTION_CHECK"
        ]
        accepted_open = [
            item for item in open_tasks if item["candidate_ref"] == self.continued_ref
        ]
        new_open = [
            item for item in open_tasks if item["candidate_ref"] != self.continued_ref
        ]
        if new_open:
            try:
                accepted_open += self._deduplicate(
                    workdir=root,
                    artifact_root=artifact_root,
                    target_contract_id=target_contract_id,
                    open_tasks=new_open,
                    prior_accepted_refs=tuple(prior_accepted_refs),
                )
            except (SmiPhaseError, SmiPipelineProtocolError) as exc:
                failure_ref = self._record_portfolio_failure(root, artifact_root, exc)
                candidate_failures.append(failure_ref)
                if _missing_input_reason(exc):
                    # Dedup is a search-context pass.  If one of its required
                    # task files disappeared, preserve the open tasks and leave
                    # the exact missing-file diagnostic for the next round.
                    accepted_open += list(new_open)
                elif not resolutions:
                    return self._commit_outcome(
                        workdir=root,
                        artifact_root=artifact_root,
                        work_item_id=work_item_id,
                        outcome="INTERFACE_FAILED",
                        failure_refs=tuple(candidate_failures),
                        reasons=("semantic_dedup_interface_failed",),
                    )

        ready = sorted(
            (*accepted_open, *resolutions),
            key=lambda item: item["task_id"],
        )
        if not ready:
            return self._commit_outcome(
                workdir=root,
                artifact_root=artifact_root,
                work_item_id=work_item_id,
                outcome="NO_ACCEPTED_TASK",
                failure_refs=tuple(candidate_failures),
                reasons=("all_open_tasks_were_duplicates",),
            )

        batch_ref = _relative_ref(artifact_root / "research_task_batch.json", root)
        _write_json(
            root / batch_ref,
            {
                "work_item_id": work_item_id,
                "dependency_scope_reviews": self.scope_records,
                "tasks": ready[:capacity],
                "deferred_tasks": ready[capacity:],
            },
        )
        return self._commit_outcome(
            workdir=root,
            artifact_root=artifact_root,
            work_item_id=work_item_id,
            outcome="TASKS_READY",
            task_batch_ref=batch_ref,
            accepted_task_refs=tuple(
                item["candidate_ref"]
                for item in ready
                if item["task_kind"] == "OPEN_PROBLEM"
            ),
            failure_refs=tuple(candidate_failures),
            reasons=tuple(no_task_reasons),
        )

    def _review_seed(
        self,
        *,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        work_item_ref: str,
        source_refs: tuple[str, ...],
        verified_progress_ref: str,
        blocked_context_refs: tuple[str, ...],
    ) -> tuple[str, str]:
        seed_ref = ""
        repair_refs: tuple[str, ...] = ()
        for attempt in range(2):
            inputs: dict[str, str | tuple[str, ...]] = {
                "TARGET_ANCHOR": target_anchor_ref,
                "WORK_ITEM": work_item_ref,
            }
            if source_refs:
                inputs["SOURCE"] = source_refs
            if verified_progress_ref:
                inputs["VERIFIED_PROGRESS"] = verified_progress_ref
            if blocked_context_refs:
                inputs["BLOCKED_CONTEXT"] = blocked_context_refs
            if seed_ref:
                inputs["SEED"] = seed_ref
            if repair_refs:
                inputs["REPAIR_BRIEF"] = repair_refs
            operation = "CREATE" if attempt == 0 else "REVISE"
            seed_call = self._call(
                state,
                phase="SEED_SCOUT",
                workdir=workdir,
                target_contract_id=target_contract_id,
                input_refs=inputs,
                prompt_seed=(
                    "Create the one bounded seed for this work item. Earlier seeds are "
                    "context for this new dispatch. The Host assigns operation CREATE."
                    if attempt == 0
                    else "Repair only the defects named in the supplied critic briefs. Preserve all unrelated passages, "
                    "do not add a new branch or parameter family. Record original defect, actual edits and affected dependencies; "
                    "the Host assigns operation REVISE."
                ),
                skill_card_refs=(_SEED_REPAIR_CARD,) if attempt else (),
                routing_context={"seed_operation": operation},
            )
            seed_ref = _phase_ref(seed_call, "seed_ref", workdir)
            if attempt == 0 and self.prior_refs:
                identity = self.phase_runner.run(
                    phase="SEMANTIC_DEDUP",
                    workdir=workdir,
                    call_dir=state.root / "early_identity",
                    target_contract_id=target_contract_id,
                    input_refs={
                        "CANDIDATE_TASK": seed_ref,
                        "PRIOR_TASK": self.prior_refs,
                    },
                    prompt_seed="Compare the precise question in this seed before exploration. New task ID: seed. "
                    "Return duplicate_of_ref only for mathematical equivalence; ignore source hints.",
                    round_index=self.round_index,
                    routing_context={
                        "tasks": [{"task_id": "seed", "candidate_ref": seed_ref}],
                        "prior_refs": list(self.prior_refs),
                    },
                )
                duplicate = next(
                    (
                        row.get("duplicate_of_ref")
                        for row in identity.payload.get("decisions", ())
                        if row.get("action") == "DUPLICATE"
                        and row.get("duplicate_of_ref") in self.prior_refs
                    ),
                    "",
                )
                if duplicate:
                    self.continued_ref = duplicate
                    state.candidate_ref = duplicate
                    return duplicate, ""
            reviews = (
                self._call(
                    state,
                    phase="RAW_SEED_CRITIC_MATH",
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    input_refs={
                        "TARGET_ANCHOR": target_anchor_ref,
                        "SEED": seed_ref,
                        "WORK_ITEM": work_item_ref,
                        **({"SOURCE": source_refs} if source_refs else {}),
                    },
                    prompt_seed="Review only the seed's mathematical validity.",
                ),
                self._call(
                    state,
                    phase="RAW_SEED_CRITIC_RESEARCH",
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    input_refs={
                        "TARGET_ANCHOR": target_anchor_ref,
                        "SEED": seed_ref,
                        "WORK_ITEM": work_item_ref,
                        **({"SOURCE": source_refs} if source_refs else {}),
                    },
                    prompt_seed="Review only the seed's research value and readiness.",
                ),
            )
            actions = tuple(
                self._expect_action(
                    review.payload,
                    review.phase,
                    ("ACCEPT", "REPAIR", "REJECT"),
                )
                for review in reviews
            )
            if "REJECT" in actions:
                return "", "raw_seed_rejected"
            if actions == ("ACCEPT", "ACCEPT"):
                return seed_ref, ""
            if attempt == 1:
                return "", "raw_seed_repair_exhausted"
            repair_refs = tuple(
                review.output_ref
                for review, action in zip(reviews, actions, strict=True)
                if action == "REPAIR"
            )
        return "", "raw_seed_repair_exhausted"

    def _drive_candidate(
        self,
        *,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        reviewed_seed_ref: str,
        source_refs: tuple[str, ...],
        blocked_context_refs: tuple[str, ...],
    ) -> _DriveResult:
        pending_probe_continuation = False
        while True:
            if state.explorer_calls >= MAX_EXPLORER_CALLS:
                if (
                    not pending_probe_continuation
                    or state.extra_probe_continuation_used
                ):
                    return _DriveResult(reason="explorer_budget_exhausted")
                state.extra_probe_continuation_used = True
            explorer = self._run_explorer(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
                required_action=state.required_explorer_action,
            )
            pending_probe_continuation = False
            action = str(explorer.payload["action"]).strip().upper()
            state.required_explorer_action = ""
            if action not in {"REJECT", "HANDOFF", "SPLIT"}:
                self._commit_candidate(
                    state,
                    _phase_ref(explorer, "candidate_ref", workdir),
                    explorer.output_ref,
                )
            state.repair_brief_refs = ()

            if state.extra_probe_continuation_used and action not in {
                "REVISE_FRONTIER",
                "REJECT",
                "HANDOFF",
                "CLAIM_RESOLVED",
            }:
                return _DriveResult(reason="final_probe_continuation_exhausted")
            if action == "REQUEST_REFUTATION_PROBE":
                if state.frontier_probe_revision == state.revision:
                    raise SmiPipelineProtocolError(
                        "frontier refute probe repeated for an unchanged revision",
                        phase="FRONTIER_EXPLORER",
                        reason_code="repeated_frontier_probe",
                    )
                view_ref = _phase_ref(explorer, "probe_task_ref", workdir)
                probe = self._call(
                    state,
                    phase="FRONTIER_REFUTE_PROBE",
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    input_refs={"PROBE_TASK": view_ref},
                    prompt_seed="Attack only the concrete construction or precise lemma in probe_target. A positive special case does not refute an existential target. If no focused target is supplied, return NO_COUNTEREXAMPLE with the missing obligation; do not re-explore the original open problem.",
                )
                self._expect_action(
                    probe.payload,
                    "FRONTIER_REFUTE_PROBE",
                    (
                        "COUNTEREXAMPLE_CANDIDATE",
                        "NO_COUNTEREXAMPLE",
                        "INVALID_FRONTIER",
                    ),
                )
                state.frontier_probe_ref = probe.output_ref
                state.internal_probe_refs += (probe.output_ref,)
                state.frontier_probe_revision = state.revision
                pending_probe_continuation = True
                continue
            if action == "REVISE_FRONTIER":
                continue
            if action == "SPLIT":
                return _DriveResult(
                    children=self._split_candidate(
                        state=state,
                        payload=explorer.payload,
                        workdir=workdir,
                    )
                )
            if action == "HANDOFF":
                return _DriveResult(reason="explorer_handoff")
            if action == "REJECT":
                return _DriveResult(reason="explorer_rejected_candidate")
            if action == "CLAIM_RESOLVED":
                return _DriveResult(
                    terminals=(
                        _TerminalCandidate(
                            state=state,
                            task_kind="RESOLUTION_CHECK",
                            trigger_refs=(explorer.output_ref,),
                        ),
                    )
                )
            if action == "EMIT":
                # Coverage is model-owned evidence.  The Host only routes the
                # explicit action; it does not reject an EMIT because a prior
                # coverage label is absent or uses another representation.
                return self._challenge_candidate(
                    state=state,
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    target_anchor_ref=target_anchor_ref,
                    reviewed_seed_ref=reviewed_seed_ref,
                    source_refs=source_refs,
                    blocked_context_refs=blocked_context_refs,
                )

            mechanism_outcome = self._review_mechanism(
                state=state,
                explorer=explorer,
                proposal_action=action,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
            )
            if mechanism_outcome != "PROMOTED":
                if mechanism_outcome == "BUDGET_EXHAUSTED":
                    return _DriveResult(reason="mechanism_budget_exhausted")
                state.required_explorer_action = (
                    "ADD_MECHANISM" if state.promoted else "PROPOSE_MECHANISM"
                )
                continue

            if len(state.promoted) == 1 and state.coverage_action == "LOCAL_SUPPORT":
                state.coverage_action = "SUBSTANTIVE_PARTIAL"
                state.selected_mechanism_ids = (state.promoted[0].mechanism_id,)
                state.coverage_ref = state.promoted[0].receipt_ref
                return self._challenge_candidate(
                    state=state,
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    target_anchor_ref=target_anchor_ref,
                    reviewed_seed_ref=reviewed_seed_ref,
                    source_refs=source_refs,
                    blocked_context_refs=blocked_context_refs,
                )
            coverage = self._run_coverage(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
            )
            if coverage is None:
                return _DriveResult(reason="coverage_budget_exhausted")
            coverage_action = self._expect_action(
                coverage.payload,
                "COVERAGE_JUDGE",
                (
                    "INSUFFICIENT_SUPPORT",
                    "SUBSTANTIVE_PARTIAL",
                    "FULL_COVERAGE",
                    "SPLIT",
                    "REVISE_FRONTIER",
                    "RESOLUTION_CANDIDATE",
                    "REJECT",
                ),
            )
            # Coverage is mathematical evidence.  Keep the model's selection
            # when it names known mechanisms, but do not turn a sparse or
            # differently shaped selection into a Host protocol failure.  An
            # empty usable selection falls back to all promoted support so the
            # next loop stage still receives context.
            promoted_ids = tuple(item.mechanism_id for item in state.promoted)
            supplied_ids = _string_tuple(coverage.payload.get("selected_mechanism_ids"))
            selected_ids = (
                tuple(
                    dict.fromkeys(item for item in supplied_ids if item in promoted_ids)
                )
                or promoted_ids
            )
            state.selected_mechanism_ids = selected_ids
            state.coverage_ref = coverage.output_ref
            state.coverage_action = coverage_action
            if coverage_action == "INSUFFICIENT_SUPPORT":
                state.repair_brief_refs = (coverage.output_ref,)
                state.required_explorer_action = "ADD_MECHANISM"
                continue
            if coverage_action == "SUBSTANTIVE_PARTIAL":
                return self._challenge_candidate(
                    state=state,
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    target_anchor_ref=target_anchor_ref,
                    reviewed_seed_ref=reviewed_seed_ref,
                    source_refs=source_refs,
                    blocked_context_refs=blocked_context_refs,
                )
            if coverage_action == "FULL_COVERAGE":
                if state.lineage.coverage_hardening_used:
                    return _DriveResult(
                        terminals=(
                            _TerminalCandidate(
                                state=state,
                                task_kind="RESOLUTION_CHECK",
                                trigger_refs=(coverage.output_ref,),
                            ),
                        )
                    )
                state.lineage.coverage_hardening_used = True
                closure_ref = _phase_ref(coverage, "closure_brief_ref", workdir)
                disposition = self._resolve_or_harden_candidate(
                    state=state,
                    workdir=workdir,
                    target_contract_id=target_contract_id,
                    target_anchor_ref=target_anchor_ref,
                    trigger_kind="CLOSURE_BRIEF",
                    trigger_ref=closure_ref,
                )
                if disposition is not None:
                    return disposition
                continue
            if coverage_action in {"SPLIT", "REVISE_FRONTIER"}:
                state.repair_brief_refs = (coverage.output_ref,)
                state.required_explorer_action = (
                    "SPLIT" if coverage_action == "SPLIT" else "REVISE_FRONTIER"
                )
                continue
            if coverage_action == "RESOLUTION_CANDIDATE":
                return _DriveResult(
                    terminals=(
                        _TerminalCandidate(
                            state=state,
                            task_kind="RESOLUTION_CHECK",
                            trigger_refs=(coverage.output_ref,),
                        ),
                    )
                )
            return _DriveResult(reason="coverage_rejected_candidate")

    def _run_explorer(
        self,
        *,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        reviewed_seed_ref: str,
        source_refs: tuple[str, ...],
        blocked_context_refs: tuple[str, ...],
        current_mechanism_ref: str = "",
        required_action: str = "",
    ) -> SmiPhaseResult:
        state.explorer_calls += 1
        inputs: dict[str, str | tuple[str, ...]] = {
            "TARGET_ANCHOR": target_anchor_ref,
            "WORK_ITEM": self.work_item_ref,
            "REVIEWED_SEED": reviewed_seed_ref,
        }
        if state.candidate_ref:
            inputs["CANDIDATE"] = state.candidate_ref
        if source_refs:
            inputs["SOURCE"] = source_refs
        mechanism_refs = tuple(
            dict.fromkeys(
                (
                    *(item.ref for item in state.promoted),
                    *((current_mechanism_ref,) if current_mechanism_ref else ()),
                )
            )
        )
        if mechanism_refs:
            inputs["MECHANISM"] = mechanism_refs
        if state.promoted:
            inputs["MECHANISM_RECEIPT"] = tuple(
                item.receipt_ref for item in state.promoted
            )
        if state.coverage_ref:
            inputs["COVERAGE"] = state.coverage_ref
        if state.frontier_probe_ref:
            inputs["FRONTIER_REFUTE_RECEIPT"] = state.frontier_probe_ref
        if state.internal_probe_refs:
            inputs["INTERNAL_SEARCH_CONTEXT"] = state.internal_probe_refs
        if state.repair_brief_refs:
            inputs["REPAIR_BRIEF"] = state.repair_brief_refs
        if blocked_context_refs:
            inputs["BLOCKED_CONTEXT"] = blocked_context_refs
        allowed = (
            (required_action,)
            if required_action
            else tuple(
                action
                for action in EXPLORER_ACTIONS
                if action != "EMIT" or state.coverage_action == "SUBSTANTIVE_PARTIAL"
            )
        )
        task = (
            "The work item fixes the question, this dispatch's one gap, and its return condition. "
            "Own the next mathematical frontier/mechanism action. Obey the repair brief. "
            "For repeated exploration, name a changed assumption, parameter, evidence, or relation to test in change.md. "
            "Develop and test a concrete mathematical move within the assigned scope; the absence of a supplied new estimate is not a reason to stop before trying. "
            "When a bounded attempt cannot advance or an exact source is missing, use HANDOFF: record the actual attempt, first unresolved step, needed input and useful next test in change.md. "
            "Do not replay unchanged toy cases. Reserve REJECT for a concrete mathematical or scope defect in the candidate, not lack of a proof or new progress. "
            f"Allowed actions for this invocation: {', '.join(allowed)}. "
            "This invocation's action constraint overrides the general role menu. "
            "For mechanism repair, preserve the mechanism ID and return the requested "
            "proposal action for independent re-review; do not emit or skip review."
        )
        if self.continued_ref:
            task += f" CONTINUE_PROBLEM: reuse the exact candidate `{self.continued_ref}` unchanged. Propose statement changes explicitly as new questions."
        task += " Promoted mechanism identities: " + json.dumps(
            [
                {
                    "mechanism_id": item.mechanism_id,
                    "mechanism_ref": item.ref,
                    "review_ref": item.receipt_ref,
                }
                for item in state.promoted
            ]
        )
        result = self._call(
            state,
            phase="FRONTIER_EXPLORER",
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs=inputs,
            prompt_seed=task,
            allowed_actions=allowed,
            skill_card_refs=(
                (_MECHANISM_REPAIR_CARD,)
                if current_mechanism_ref
                else (_FRONTIER_SUPPORT_CARD,)
                if state.repair_brief_refs
                else ()
            ),
        )
        return result

    def _review_mechanism(
        self,
        *,
        state: _Candidate,
        explorer: SmiPhaseResult,
        proposal_action: str,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        reviewed_seed_ref: str,
        source_refs: tuple[str, ...],
        blocked_context_refs: tuple[str, ...],
    ) -> str:
        if state.mechanism_ids_used >= MAX_MECHANISM_IDS:
            return "BUDGET_EXHAUSTED"
        state.mechanism_ids_used += 1
        mechanism_id = f"mechanism_{state.mechanism_ids_used:02d}"
        proposal = explorer
        record = _Mechanism(
            mechanism_id=mechanism_id,
            ref=_phase_ref(proposal, "mechanism_ref", workdir),
        )
        previous_review_refs: tuple[str, ...] = ()
        while True:
            dependency_ids = _string_tuple(
                proposal.payload.get("dependency_mechanism_ids")
            )
            dependency_records = [
                item for item in state.promoted if item.mechanism_id in dependency_ids
            ]
            critic_inputs: dict[str, str | tuple[str, ...]] = {
                "TARGET_ANCHOR": target_anchor_ref,
                "CANDIDATE": state.candidate_ref,
                "MECHANISM": record.ref,
            }
            critic_inputs["WORK_ITEM"] = self.work_item_ref
            if proposal.payload.get("change_ref"):
                critic_inputs["CHANGE"] = proposal.payload["change_ref"]
            critic_inputs["INTERNAL_SEARCH_CONTEXT"] = tuple(
                dict.fromkeys(
                    ref
                    for ref in (
                        reviewed_seed_ref,
                        *state.internal_probe_refs,
                        state.coverage_ref,
                        *state.repair_brief_refs,
                    )
                    if ref
                )
            )
            if previous_review_refs:
                critic_inputs["PREVIOUS_REVIEW"] = previous_review_refs
            if dependency_records:
                critic_inputs["DEPENDENCY_MECHANISM"] = tuple(
                    item.ref for item in dependency_records
                )
                critic_inputs["DEPENDENCY_RECEIPT"] = tuple(
                    item.receipt_ref for item in dependency_records
                )
            # Source records are candidate context.  Keep the caller's
            # suggested refs available to the critic instead of filtering
            # them by a model-produced string list, which can silently hide a
            # usable source when its label or path differs.
            if "dependency_refs" in proposal.payload:
                critic_inputs["MECHANISM_SOURCE"] = tuple(
                    proposal.payload["dependency_refs"]
                )
                critic_inputs["ARCHIVE_SOURCE"] = source_refs
                if self.scope_objection_refs:
                    critic_inputs["REPAIR_BRIEF"] = tuple(self.scope_objection_refs)
            elif source_refs:
                critic_inputs["MECHANISM_SOURCE"] = source_refs
            critic = self._call(
                state,
                phase="MECHANISM_CRITIC",
                workdir=workdir,
                target_contract_id=target_contract_id,
                input_refs=critic_inputs,
                prompt_seed=(
                    f"Independently review only {mechanism_id}. Internal probe results are visible search context, not verified premises. "
                    "When PREVIOUS_REVIEW is present, compare the old candidate and mechanism, prior review, repair response, and current artifacts. "
                    "For local example or citation changes, audit the delta and its dependency impact. "
                    "If assumptions, core claims, derivation, or dependencies changed, perform a full review. You decide the mathematical scope, not the Host."
                ),
                base_dir=state.root / mechanism_id / "critic",
                routing_context={"mechanism_id": mechanism_id},
                skill_card_refs=(_MECHANISM_INTERFACE_CARD,)
                if previous_review_refs
                else (),
            )
            decision = self._expect_action(
                critic.payload,
                "MECHANISM_CRITIC",
                ("PROMOTE", "REPAIR", "REJECT"),
            )
            if decision == "PROMOTE":
                record.receipt_ref = critic.output_ref
                state.promoted.append(record)
                state.coverage_action = critic.payload.get("support_disposition", "")
                return "PROMOTED"
            if decision == "REJECT":
                state.repair_brief_refs = (critic.output_ref,)
                return "REJECTED"
            if state.mechanism_repairs >= MAX_MECHANISM_REPAIRS:
                return "BUDGET_EXHAUSTED"
            if state.explorer_calls >= MAX_EXPLORER_CALLS:
                return "BUDGET_EXHAUSTED"
            state.mechanism_repairs += 1
            state.repair_brief_refs = (critic.output_ref,)
            previous_review_refs = (state.candidate_ref, record.ref, critic.output_ref)
            repaired = self._run_explorer(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
                current_mechanism_ref=record.ref,
                required_action=proposal_action,
            )
            previous_review_refs += (repaired.output_ref,)
            self._commit_candidate(
                state,
                _phase_ref(repaired, "candidate_ref", workdir),
                repaired.output_ref,
            )
            proposal = repaired
            record.ref = _phase_ref(repaired, "mechanism_ref", workdir)
            state.repair_brief_refs = ()

    def _run_coverage(
        self,
        *,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
    ) -> SmiPhaseResult | None:
        if state.coverage_calls >= MAX_COVERAGE_CALLS:
            return None
        state.coverage_calls += 1
        return self._call(
            state,
            phase="COVERAGE_JUDGE",
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs={
                "TARGET_ANCHOR": target_anchor_ref,
                "WORK_ITEM": self.work_item_ref,
                "CANDIDATE": state.candidate_ref,
                "MECHANISM": tuple(item.ref for item in state.promoted),
                "MECHANISM_RECEIPT": tuple(item.receipt_ref for item in state.promoted),
            },
            prompt_seed=(
                "Judge the combined promoted support and return exactly one coverage action. "
                "Promoted mechanism identities: "
                + json.dumps(
                    [
                        {
                            "mechanism_id": item.mechanism_id,
                            "mechanism_ref": item.ref,
                            "review_ref": item.receipt_ref,
                        }
                        for item in state.promoted
                    ]
                )
            ),
            routing_context={
                "mechanism_ids": [item.mechanism_id for item in state.promoted]
            },
        )

    def _challenge_candidate(
        self,
        *,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        reviewed_seed_ref: str,
        source_refs: tuple[str, ...],
        blocked_context_refs: tuple[str, ...],
    ) -> _DriveResult:
        inputs: dict[str, str | tuple[str, ...]] = {
            "TARGET_ANCHOR": target_anchor_ref,
            "CANDIDATE": state.candidate_ref,
        }
        if self.independent_source_refs:
            inputs["SOURCE"] = self.independent_source_refs
        challenge = self._scope_call(
            state,
            phase="FINAL_CHALLENGER",
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs=inputs,
            prompt_seed="Audit only the candidate, without mechanism or coverage records.",
        )
        state.challenge_ref = challenge.output_ref
        action = self._expect_action(
            challenge.payload,
            "FINAL_CHALLENGER",
            (
                "ACCEPT",
                "RUN_PROBES",
                "REPAIR",
                "SPLIT",
                "RESOLUTION_CANDIDATE",
                "REJECT",
            ),
        )
        if action == "REJECT":
            return _DriveResult(reason="challenger_rejected_candidate")
        if action == "RESOLUTION_CANDIDATE":
            return _DriveResult(
                terminals=(
                    _TerminalCandidate(
                        state=state,
                        task_kind="RESOLUTION_CHECK",
                        trigger_refs=(challenge.output_ref,),
                    ),
                )
            )
        if action in {"REPAIR", "SPLIT"}:
            if action == "REPAIR" and state.lineage.challenger_repair_used:
                return _DriveResult(reason="challenger_repair_budget_exhausted")
            if action == "REPAIR":
                state.lineage.challenger_repair_used = True
            if state.explorer_calls >= MAX_EXPLORER_CALLS:
                return _DriveResult(reason="explorer_budget_exhausted")
            state.repair_brief_refs = (challenge.output_ref,)
            expected = "SPLIT" if action == "SPLIT" else "REVISE_FRONTIER"
            explorer = self._run_explorer(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
                required_action=expected,
            )
            if expected != "SPLIT":
                self._commit_candidate(
                    state,
                    _phase_ref(explorer, "candidate_ref", workdir),
                    explorer.output_ref,
                )
            state.repair_brief_refs = ()
            if expected == "SPLIT":
                return _DriveResult(
                    children=self._split_candidate(
                        state=state,
                        payload=explorer.payload,
                        workdir=workdir,
                    )
                )
            return self._drive_candidate(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
            )
        if action == "ACCEPT":
            return _DriveResult(
                terminals=(
                    _TerminalCandidate(
                        state=state,
                        task_kind="OPEN_PROBLEM",
                        trigger_refs=(challenge.output_ref,),
                    ),
                )
            )
        return self._run_prover_gate(
            probe_task_ref=_phase_ref(challenge, "probe_task_ref", workdir),
            state=state,
            workdir=workdir,
            target_contract_id=target_contract_id,
            target_anchor_ref=target_anchor_ref,
            reviewed_seed_ref=reviewed_seed_ref,
            source_refs=source_refs,
            blocked_context_refs=blocked_context_refs,
        )

    def _run_prover_gate(
        self,
        *,
        probe_task_ref: str,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        reviewed_seed_ref: str,
        source_refs: tuple[str, ...],
        blocked_context_refs: tuple[str, ...],
    ) -> _DriveResult:
        if state.lineage.gate_cycles >= MAX_GATE_CYCLES:
            return _DriveResult(reason="gate_cycle_budget_exhausted")
        state.lineage.gate_cycles += 1
        prover_ref = probe_task_ref
        blind = self._call(
            state,
            phase="BLIND_PROVER_PROBE",
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs={"PROBE_TASK": prover_ref},
            prompt_seed="Attempt only the concrete construction or precise lemma in probe_target, independently of mechanisms. Report local progress separately from a complete solution of the original target.",
            base_dir=(
                state.root / "probes" / f"revision_{state.revision:02d}" / "blind"
            ),
        )
        refute = self._call(
            state,
            phase="REFUTE_PROVER_PROBE",
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs={"PROBE_TASK": prover_ref},
            prompt_seed="Attack only probe_target from the same mechanism-free view. Distinguish a failed local construction from a refutation of the original target; do not repeat generic positive special cases.",
            base_dir=(
                state.root / "probes" / f"revision_{state.revision:02d}" / "refute"
            ),
        )
        state.internal_probe_refs += (blind.output_ref, refute.output_ref)
        blind_action = self._expect_action(
            blind.payload,
            "BLIND_PROVER_PROBE",
            (
                "COMPLETE_SOLUTION",
                "PARTIAL_PROGRESS",
                "NO_FIRST_STEP",
                "INVALID_STATEMENT",
            ),
        )
        refute_action = self._expect_action(
            refute.payload,
            "REFUTE_PROVER_PROBE",
            (
                "COUNTEREXAMPLE_CANDIDATE",
                "NO_COUNTEREXAMPLE",
                "PARTIAL_PROGRESS",
                "INVALID_STATEMENT",
            ),
        )
        if refute_action == "COUNTEREXAMPLE_CANDIDATE":
            return _DriveResult(
                terminals=(
                    _TerminalCandidate(
                        state=state,
                        task_kind="RESOLUTION_CHECK",
                        trigger_refs=(refute.output_ref,),
                    ),
                )
            )
        invalid = (
            blind
            if blind_action == "INVALID_STATEMENT"
            else (refute if refute_action == "INVALID_STATEMENT" else None)
        )
        if invalid is not None:
            if state.lineage.normalizer_used:
                return _DriveResult(reason="normalizer_budget_exhausted")
            state.lineage.normalizer_used = True
            normalized = self._call(
                state,
                phase="STATEMENT_NORMALIZER",
                workdir=workdir,
                target_contract_id=target_contract_id,
                input_refs={
                    "TARGET_ANCHOR": target_anchor_ref,
                    "CANDIDATE": state.candidate_ref,
                    "INVALID_PROBE_RECEIPT": invalid.output_ref,
                },
                prompt_seed="Normalize only the reported presentation defect.",
            )
            action = self._expect_action(
                normalized.payload,
                "STATEMENT_NORMALIZER",
                ("NORMALIZE", "REJECT"),
            )
            if action == "REJECT":
                return _DriveResult(reason="normalizer_rejected_candidate")
            self._commit_candidate(
                state,
                _phase_ref(normalized, "candidate_ref", workdir),
                normalized.output_ref,
            )
            return self._drive_candidate(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
            )
        if blind_action == "COMPLETE_SOLUTION":
            state.lineage.blind_solutions += 1
            if state.lineage.blind_solutions >= 2:
                return _DriveResult(
                    terminals=(
                        _TerminalCandidate(
                            state=state,
                            task_kind="RESOLUTION_CHECK",
                            trigger_refs=(blind.output_ref,),
                        ),
                    )
                )
            if state.lineage.blind_hardening_used:
                return _DriveResult(reason="blind_hardening_budget_exhausted")
            state.lineage.blind_hardening_used = True
            disposition = self._resolve_or_harden_candidate(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                trigger_kind="BLIND_SOLUTION_RECEIPT",
                trigger_ref=blind.output_ref,
            )
            if disposition is not None:
                return disposition
            return self._drive_candidate(
                state=state,
                workdir=workdir,
                target_contract_id=target_contract_id,
                target_anchor_ref=target_anchor_ref,
                reviewed_seed_ref=reviewed_seed_ref,
                source_refs=source_refs,
                blocked_context_refs=blocked_context_refs,
            )
        return _DriveResult(
            terminals=(
                _TerminalCandidate(
                    state=state,
                    task_kind="OPEN_PROBLEM",
                    trigger_refs=(state.challenge_ref,),
                ),
            )
        )

    def _resolve_or_harden_candidate(
        self,
        *,
        state: _Candidate,
        workdir: Path,
        target_contract_id: str,
        target_anchor_ref: str,
        trigger_kind: str,
        trigger_ref: str,
    ) -> _DriveResult | None:
        hardened = self._call(
            state,
            phase="FRONTIER_HARDENER",
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs={
                "TARGET_ANCHOR": target_anchor_ref,
                "WORK_ITEM": self.work_item_ref,
                "CANDIDATE": state.candidate_ref,
                trigger_kind: trigger_ref,
            },
            prompt_seed="Use the work item and explicit closure trigger to choose resolution handoff, in-scope hardening, or rejection.",
        )
        action = self._expect_action(
            hardened.payload,
            "FRONTIER_HARDENER",
            ("HARDEN", "RESOLUTION_CANDIDATE", "REJECT"),
        )
        if action == "RESOLUTION_CANDIDATE":
            return _DriveResult(
                terminals=(
                    _TerminalCandidate(
                        state=state,
                        task_kind="RESOLUTION_CHECK",
                        trigger_refs=(trigger_ref, hardened.output_ref),
                    ),
                )
            )
        if action == "REJECT":
            return _DriveResult(reason="hardener_rejected_candidate")
        self._commit_candidate(
            state, _phase_ref(hardened, "candidate_ref", workdir), hardened.output_ref
        )
        return None

    def _split_candidate(
        self,
        *,
        state: _Candidate,
        payload: Mapping[str, Any],
        workdir: Path,
    ) -> tuple[_Candidate, ...]:
        if state.lineage.split_used:
            raise SmiPipelineProtocolError(
                "nested candidate split is not allowed",
                phase="FRONTIER_EXPLORER",
                reason_code="split_budget_exhausted",
            )
        child_refs = payload["child_candidate_refs"]
        if not isinstance(child_refs, list):
            raise SmiPipelineProtocolError(
                "SPLIT requires child_candidate_refs",
                phase="FRONTIER_EXPLORER",
                reason_code="child_refs_missing",
            )
        child_refs = child_refs[:MAX_ACTIVE_CANDIDATES]
        if not child_refs:
            return ()
        state.lineage.split_used = True
        children: list[_Candidate] = []
        for child_ref in child_refs:
            child = self._new_candidate(
                artifact_root=state.root.parent,
                lineage=state.lineage,
                split_generation=state.split_generation + 1,
            )
            reference = _file_ref(child_ref, workdir, phase="FRONTIER_EXPLORER")
            self._commit_candidate(child, reference, "")
            children.append(child)
        return tuple(children)

    def _deduplicate(
        self,
        *,
        workdir: Path,
        artifact_root: Path,
        target_contract_id: str,
        open_tasks: Sequence[dict[str, Any]],
        prior_accepted_refs: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if len(open_tasks) == 1 and not prior_accepted_refs:
            return list(open_tasks)
        call_dir = artifact_root / "semantic_dedup"
        result = self.phase_runner.run(
            round_index=self.round_index,
            phase="SEMANTIC_DEDUP",
            workdir=workdir,
            call_dir=call_dir,
            target_contract_id=target_contract_id,
            input_refs={
                "CANDIDATE_TASK": tuple(item["candidate_ref"] for item in open_tasks),
                **({"PRIOR_TASK": prior_accepted_refs} if prior_accepted_refs else {}),
            },
            prompt_seed=(
                "Return one KEEP or DUPLICATE decision for every new task ID. New tasks: "
                + json.dumps(
                    [
                        {key: item[key] for key in ("task_id", "candidate_ref")}
                        for item in open_tasks
                    ]
                )
            ),
            routing_context={
                "tasks": [
                    {key: item[key] for key in ("task_id", "candidate_ref")}
                    for item in open_tasks
                ],
                "prior_refs": list(prior_accepted_refs),
            },
        )
        raw_value = result.payload.get("decisions")
        raw = raw_value if isinstance(raw_value, list) else [raw_value]
        by_id = {str(row.get("task_id")): row for row in raw if isinstance(row, dict)}
        known = set(prior_accepted_refs) | {
            item["candidate_ref"] for item in open_tasks
        }
        result_tasks = []
        for item in open_tasks:
            decision = by_id.get(item["task_id"], {})
            reference = decision.get("duplicate_of_ref", "")
            if decision.get("action") == "DUPLICATE" and reference in known:
                item = {
                    **item,
                    "candidate_ref": reference,
                    "research_operation": "CONTINUE_PROBLEM",
                }
            result_tasks.append(item)
        # Register equivalent statements under the original identity while preserving
        # the newly produced mechanisms and all review context.
        return result_tasks

    def _task_record(
        self,
        *,
        terminal: _TerminalCandidate,
        task_ordinal: int,
        target_anchor_ref: str,
        work_item_id: str,
    ) -> dict[str, Any]:
        state = terminal.state
        selected = self._selected_mechanism_refs(state)
        context_refs = (
            state.candidate_receipt_ref,
            *selected,
            *(item.receipt_ref for item in state.promoted if item.ref in selected),
            state.coverage_ref,
            state.challenge_ref,
            *state.internal_probe_refs,
            *terminal.trigger_refs,
        )
        return {
            "task_id": f"{work_item_id}-task_{task_ordinal:02d}",
            "task_kind": terminal.task_kind,
            "candidate_ref": state.candidate_ref,
            "target_contract_ref": target_anchor_ref,
            "context_refs": list(dict.fromkeys(ref for ref in context_refs if ref)),
        }

    def _commit_candidate(
        self, state: _Candidate, candidate_ref: str, receipt_ref: str
    ) -> None:
        state.candidate_receipt_ref = receipt_ref
        if candidate_ref == state.candidate_ref:
            return
        state.revision += 1
        state.candidate_ref = candidate_ref
        state.promoted.clear()
        state.coverage_ref = ""
        state.coverage_action = ""
        state.selected_mechanism_ids = ()
        state.challenge_ref = ""
        state.frontier_probe_ref = ""
        state.frontier_probe_revision = 0

    @staticmethod
    def _selected_mechanism_refs(state: _Candidate) -> tuple[str, ...]:
        selected = set(state.selected_mechanism_ids)
        return tuple(
            item.ref for item in state.promoted if item.mechanism_id in selected
        )

    def _scope_call(self, state, **kwargs):
        """Reuse question-scope acceptance, never a mathematical proof verdict."""
        root = Path(kwargs["workdir"])
        phase, inputs = kwargs["phase"], kwargs["input_refs"]
        candidate = inputs.get("CANDIDATE") or inputs.get("SEED")
        from .roles import (
            role_binding,
            resolve_skill_refs,
            SMI_SKILL_CORE_REF,
            SMI_SKILL_ENTRY_REF,
        )

        binding = role_binding(phase)
        skill_refs = resolve_skill_refs(
            root, (*binding.skill_refs, SMI_SKILL_CORE_REF, SMI_SKILL_ENTRY_REF)
        )
        rules = {
            ref: (root / ref).read_text()
            for ref in (work_ref(root, binding.prompt_ref), *skill_refs)
        }
        dependencies = tuple(
            ref
            for kind, values in inputs.items()
            if kind not in {"TARGET_ANCHOR", "CANDIDATE", "SEED"}
            for ref in ((values,) if isinstance(values, str) else values)
        )
        for row in reversed(getattr(self, "prior_scope_records", ())):
            if row["phase"] != phase or row["candidate_ref"] != candidate:
                continue
            # A review may follow a source reference that was not a suggested
            # starting input. Compare its recorded dependencies directly too.
            scope = scope_snapshot(
                root, candidate, inputs["TARGET_ANCHOR"], rules,
                (*row["scope"]["dependencies"], *dependencies),
            )
            scope["objections"].update(self.scope_objections)
            if not scope_matches(row["scope"], scope):
                continue
            if not row.get("outputs") or not (root / row["output_ref"]).is_file():
                continue
            if not all(
                (root / ref).is_file() and (root / ref).read_text() == content
                for ref, content in row["outputs"].items()
            ):
                continue
            payload = json.loads((root / row["output_ref"]).read_text())
            if payload.get("action") != "ACCEPT":
                continue
            from pace.runtime.output.events import append_event

            append_event(
                root,
                "SMI_SCOPE_REUSED",
                phase=phase,
                candidate_ref=candidate,
                review_ref=row["output_ref"],
            )
            self.scope_records.append(row)
            return SmiPhaseResult(
                phase=phase,
                role=binding.role,
                payload=payload,
                output_ref=row["output_ref"],
                prompt_assembly_ref=row["prompt_assembly_ref"],
            )
        result = self._call(state, **kwargs)
        if (
            result.payload.get("action") == "ACCEPT"
            and (root / result.output_ref).is_file()
        ):
            scope = scope_snapshot(
                root,
                candidate,
                inputs["TARGET_ANCHOR"],
                rules,
                tuple(result.payload["dependency_refs"]),
            )
            scope["objections"].update(getattr(self, "scope_objections", {}))
            outputs = [
                result.output_ref,
                *(
                    (result.payload["review_ref"],)
                    if result.payload.get("review_ref")
                    else ()
                ),
            ]
            self.scope_records.append(
                {
                    "phase": phase,
                    "candidate_ref": candidate,
                    "scope": scope,
                    "output_ref": result.output_ref,
                    "prompt_assembly_ref": result.prompt_assembly_ref,
                    "outputs": {
                        ref: (root / ref).read_text()
                        for ref in outputs
                        if (root / ref).is_file()
                    },
                }
            )
        return result

    def _call(
        self,
        state: _Candidate,
        *,
        phase: str,
        workdir: Path,
        target_contract_id: str,
        input_refs: Mapping[str, str | Path | Iterable[str | Path]],
        prompt_seed: str,
        base_dir: Path | None = None,
        skill_card_refs: Iterable[str] = (),
        allowed_actions: Sequence[str] = (),
        routing_context: Mapping[str, Any] | None = None,
    ) -> SmiPhaseResult:
        ordinal = state.phase_calls.get(phase, 0) + 1
        state.phase_calls[phase] = ordinal
        phase_dir = base_dir or state.root / phase.lower()
        return self.phase_runner.run(
            round_index=self.round_index,
            phase=phase,
            workdir=workdir,
            call_dir=phase_dir if ordinal == 1 else phase_dir.with_name(f"{phase_dir.name}_{ordinal:02d}"),
            target_contract_id=target_contract_id,
            input_refs=input_refs,
            prompt_seed=prompt_seed,
            skill_card_refs=skill_card_refs,
            **({"allowed_actions": tuple(allowed_actions)} if allowed_actions else {}),
            **({"routing_context": routing_context} if routing_context else {}),
        )

    def _new_candidate(
        self,
        *,
        artifact_root: Path,
        lineage: _LineageBudget,
        split_generation: int = 0,
    ) -> _Candidate:
        self._candidate_ordinal += 1
        candidate_id = f"candidate_{self._candidate_ordinal:02d}"
        return _Candidate(
            candidate_id=candidate_id,
            root=artifact_root / candidate_id,
            lineage=lineage,
            split_generation=split_generation,
        )

    def _record_failure(
        self,
        workdir: Path,
        state: _Candidate,
        exc: Exception,
    ) -> str:
        path = state.root / "failure.json"
        _write_json(path, _failure_payload(exc))
        return _relative_ref(path, workdir)

    def _record_portfolio_failure(
        self,
        workdir: Path,
        artifact_root: Path,
        exc: Exception,
    ) -> str:
        path = artifact_root / "portfolio_failure.json"
        _write_json(path, _failure_payload(exc))
        return _relative_ref(path, workdir)

    def _commit_outcome(
        self,
        *,
        workdir: Path,
        artifact_root: Path,
        work_item_id: str,
        outcome: str,
        task_batch_ref: str = "",
        accepted_task_refs: tuple[str, ...] = (),
        failure_refs: tuple[str, ...] = (),
        reasons: tuple[str, ...] = (),
    ) -> str:
        scope_ref = ""
        if getattr(self, "scope_records", ()):
            scope_ref = _relative_ref(artifact_root / "scope_reviews.json", workdir)
            _write_json(
                workdir / scope_ref, {"dependency_scope_reviews": self.scope_records}
            )
        outcome_path = artifact_root / "outcome.json"
        _write_json(
            outcome_path,
            {
                "outcome": outcome,
                "work_item_id": work_item_id,
                "task_batch_ref": task_batch_ref,
                "scope_reviews_ref": scope_ref,
                "accepted_task_refs": list(accepted_task_refs),
                "candidate_failure_refs": list(failure_refs),
                "reasons": list(reasons),
            },
        )
        return _relative_ref(outcome_path, workdir)

    @staticmethod
    def _expect_action(
        payload: Mapping[str, Any],
        phase: str,
        allowed: Sequence[str],
    ) -> str:
        action = str(payload.get("action") or "").strip().upper()
        if action not in allowed:
            raise SmiPipelineProtocolError(
                f"{phase} expected one of {list(allowed)!r} in the current step; received {action!r}",
                phase=phase,
                reason_code="unsupported_action",
            )
        return action


def _file_ref(value: Any, workdir: Path, *, phase: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise SmiPipelineProtocolError(
            "phase artifact requires a problem-relative file reference",
            phase=phase,
            reason_code="artifact_ref_invalid",
        )
    if not (workdir / value).is_file():
        raise SmiPipelineProtocolError(
            f"phase artifact was not written: {value}",
            phase=phase,
            reason_code="phase_artifact_missing",
        )
    return value


def _phase_ref(result: SmiPhaseResult, name: str, workdir: Path) -> str:
    return _file_ref(result.payload.get(name), workdir, phase=result.phase)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item))


def _missing_input_reason(exc: Exception) -> str:
    return next(
        (
            reason
            for reason in ("required_input_missing", "required_input_file_missing")
            if _has_reason_code(exc, reason)
        ),
        "",
    )


def _has_reason_code(exc: Exception, reason_code: str) -> bool:
    """Recognize one explicitly recoverable diagnostic without broad matching."""

    if str(getattr(exc, "reason_code", "") or "") == reason_code:
        return True
    return any(
        str(row.get("reason_code") or "") == reason_code
        for row in getattr(exc, "diagnostics", ())
        if isinstance(row, Mapping)
    )


def _failure_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "failure_owner": str(getattr(exc, "failure_owner", "HOST")),
        "phase": str(getattr(exc, "phase", "SMI")),
        "error_type": type(exc).__name__,
        "message": str(exc),
        "receipt_corrections": int(getattr(exc, "receipt_corrections", 0)),
    }
    reason_code = str(getattr(exc, "reason_code", "") or "")
    if reason_code:
        payload["reason_code"] = reason_code
    diagnostics = getattr(exc, "diagnostics", ())
    if diagnostics:
        payload["diagnostics"] = list(diagnostics)
    return payload


def _relative_ref(path: Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


__all__ = [
    "MAX_ACTIVE_CANDIDATES",
    "MAX_CHILD_TASKS",
    "MAX_COVERAGE_CALLS",
    "MAX_EXPLORER_CALLS",
    "MAX_GATE_CYCLES",
    "MAX_MECHANISM_IDS",
    "MAX_MECHANISM_REPAIRS",
    "SmiPipeline",
    "SmiPipelineProtocolError",
    "PhaseRunner",
]
