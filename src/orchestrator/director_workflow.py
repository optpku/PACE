"""Research-turn lifecycle between shared orientation and the terminal mathematical report."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from pace.agents.orientation import proof_assessment_schema
from pace.contracts.problem.loader import Problem
from pace.contracts.proof.binding import load_active_target
from pace.orchestrator.director_led import DirectorLedEngineError, DirectorLedState
from pace.orchestrator.director_led_finalization import finalize_director_led
from pace.orchestrator.finding_publication import pending_finding_checks
from pace.pipeline_types import MAX_PROOF_STATES, PipelineResult
from pace.runtime.context_state import build_context_manifest
from pace.runtime.output.delivery_pause import DeliveryPaused, delivery_pause
from pace.runtime.output.events import append_event
from pace.runtime.output.memory import initialize_memory, initialize_research_memory
from pace.runtime.output.profiling import ProfileRecorder
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.schema import validate_json_instance


def _proof_budget_from_assessment(assessment: dict[str, Any]) -> dict[str, Any]:
    errors = validate_json_instance(assessment, proof_assessment_schema())
    if errors:
        raise ValueError(
            "invalid proof assessment: " + json.dumps(errors, ensure_ascii=False)
        )
    difficulty = assessment["difficulty"]
    complexity = max(
        assessment["plan_node_level"], assessment["dependency_depth_level"]
    )
    if difficulty == "research":
        total = 10 + 3 * (complexity - 1)
    elif difficulty == "routine":
        total = 2
    else:
        total = 3 * complexity
    return {
        "max_total_proof_states": min(MAX_PROOF_STATES, total),
        "cap_origin": "ORIENTATION_SCHEMA",
    }


def initialize_research_turn(ws: RunWorkspace, orientation_result: dict, configured_cap: int | None) -> None:
    """Commit shared context and budget before the pristine Director checkpoint."""
    target_contract = load_active_target(ws.root)
    state_ref = ws.work_ref("state/director_state.json")
    if ws.path(state_ref).exists():
        initial = DirectorLedState.from_json(json.loads(ws.path(state_ref).read_text()))
        if (initial.phase != "DIRECTOR" or initial.round_index or initial.proof_states_used
                or initial.target_contract_id != target_contract["contract_id"]):
            raise ValueError("initialization cannot overwrite an active Director checkpoint")
        return
    initialize_memory(
        ws,
        context_manifest=build_context_manifest(
            target_contract=target_contract,
            problem_ref=ws.work_ref("input/problem.md"),
            target_contract_ref=ws.work_ref("input/target_contract.json"),
            problem_brief_ref=orientation_result.get("problem_brief_ref", ""),
            orientation_ref=ws.work_ref(
                "orientation/problem_orientation_result.json"
            ),
        ),
    )

    assessment_ref = orientation_result.get("assessment_ref", "")
    if assessment_ref:
        assessment = json.loads(ws.path(assessment_ref).read_text(encoding="utf-8"))
        proof_budget = _proof_budget_from_assessment(assessment["proof_assessment"])
    else:
        proof_budget = {
            "max_total_proof_states": MAX_PROOF_STATES,
            "cap_origin": "DEFAULT_PROOF_STATES",
        }
    # The caller's optional upper bound can reduce, never expand, the
    # analysis-derived shared proof-state budget.
    max_proof_states = int(proof_budget["max_total_proof_states"])
    if configured_cap is not None:
        max_proof_states = min(max_proof_states, configured_cap)
        proof_budget["configured_upper_bound"] = configured_cap
        if max_proof_states < proof_budget["max_total_proof_states"]:
            proof_budget["cap_origin"] = "CONFIGURED_UPPER_BOUND"
        proof_budget["max_total_proof_states"] = max_proof_states
    policy = json.loads(
        ws.path(
            work_ref(ws.root, "state/research_execution_policy.json")
        ).read_text(encoding="utf-8")
    )
    policy.update(
        max_proof_states=max_proof_states,
        budget_origin=proof_budget["cap_origin"],
        mode="research",
    )
    ws.write_json(work_ref(ws.root, "state/research_execution_policy.json"), policy)
    ws.write_json(work_ref(ws.root, "state/proof_loop_budget.json"), proof_budget)

    initialize_research_memory(ws.root)
    initial = DirectorLedState(target_contract_id=target_contract["contract_id"], max_proof_states=max_proof_states)
    ws.write_json(state_ref, initial.to_json())


def run_research_turn(ws, problem, profiler, state, build_engine, *, finalize=False):
    """Start and resume through the same evidence and final delivery boundaries."""
    if finalize and state.phase not in {"DIRECTOR", "FINALIZE", "STOPPED"}:
        from pace.orchestrator.recovery import RecoveryError
        raise RecoveryError("finalization_requires_research_boundary",
                            "complete the pending task or verification before --finalize")
    deferred = False
    try:
        if state.phase != "FAILED":
            target_id = load_active_target(ws.root)["contract_id"]
            if target_id != state.target_contract_id:
                raise DirectorLedEngineError("target differs from the saved research checkpoint")
            if finalize and state.phase == "DIRECTOR":
                state = replace(state, phase="FINALIZE", terminal_result="FAILED",
                                stop_reason="USER_REQUESTED_FINALIZATION")
                ws.write_json(ws.work_ref("state/director_state.json"), state.to_json())
                append_event(ws.root, "RESEARCH_FINALIZATION_REQUESTED", round_index=state.round_index,
                             target_contract_id=target_id, proof_states_used=state.proof_states_used,
                             max_proof_states=state.max_proof_states)
            engine = build_engine(ws, target_contract_id=target_id)
            policy = json.loads(ws.path(ws.work_ref("state/research_execution_policy.json")).read_text())
            state = engine.run(control_context=policy["control_context"], resume=True).state
    except DeliveryPaused:
        state = DirectorLedState.from_json(json.loads(ws.path(ws.work_ref("state/director_state.json")).read_text()))
        if state.phase not in {"FINALIZE", "STOPPED", "FAILED"}:
            raise
        deferred = True
    except Exception as exc:
        return finalize_engine_failure(ws, problem, profiler, error=exc)
    return finalize_director_led(workspace=ws, problem=problem, state=state, profiler=profiler,
                                 defer_delivery=deferred)


def finalize_engine_failure(
    ws: RunWorkspace,
    problem: Problem,
    profiler: ProfileRecorder,
    *,
    error: Exception,
) -> PipelineResult:
    diagnostic_ref = work_ref(
        ws.root, "state/diagnostics/director_led_engine_failure.json"
    )
    ws.write_json(
        diagnostic_ref,
        {
            "schema_version": "operational_diagnostic/v1",
            "stage": "DIRECTOR_LED_ENGINE",
            "error_type": type(error).__name__,
            "message": str(error),
        },
    )
    try:
        state = DirectorLedState.from_json(
            json.loads(
                ws.path(work_ref(ws.root, "state/director_state.json")).read_text(
                    encoding="utf-8"
                )
            )
        )
    except (
        DirectorLedEngineError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        raise DirectorLedEngineError(
            "Director checkpoint is unavailable; preserve its diagnostics"
        ) from error
    if state.phase in {"DIRECTOR", "PENDING_SPECIALIST", "PENDING_BATCH", "VERIFY"}:
        diagnostic = json.loads(ws.path(diagnostic_ref).read_text(encoding="utf-8"))
        diagnostic["resume_checkpoint"] = state.to_json()
        ws.write_json(diagnostic_ref, diagnostic)
    failed = replace(state, phase="FAILED", round_index=max(1, state.round_index),
                     diagnostics_ref=diagnostic_ref, stop_reason="director_led_engine_failed",
                     terminal_result="FAILED")
    ws.write_json(work_ref(ws.root, "state/director_state.json"), failed.to_json())
    return finalize_director_led(
        workspace=ws,
        problem=problem,
        state=failed,
        profiler=profiler,
        reason_code="director_led_engine_failed",
    )


def resume_research_checkpoint(workspace: RunWorkspace, continue_pipeline):
    """Use a bound terminal result or resume its exact pending mathematical or delivery phase."""
    from pace.orchestrator.recovery import (
        RecoveryError, RecoveryRequest, _project_terminal_result, _read_json_object,
        _restore_operational_checkpoint, load_director_checkpoint,
    )
    terminal = workspace.path(work_ref(workspace.root, 'final_report.json'))
    if terminal.is_symlink():
        raise RecoveryError(
            "invalid_final_report",
            "final_report.json must be a regular file, not a symlink",
        )
    if terminal.is_file():
        report = _read_json_object(terminal)
        result = _project_terminal_result(workspace, report)
        if report["result"] == "FAILED" and continue_pipeline is not None:
            state = load_director_checkpoint(workspace)
            restored = _restore_operational_checkpoint(workspace, state)
            if restored is not None:
                return continue_pipeline(RecoveryRequest(
                    workspace=workspace, checkpoint=restored,
                    continuation_action=restored["phase"],
                ))

        # Publication can finish just before a crash leaves the earlier paused
        # final report on disk. Re-project that report from the saved receipts.
        if continue_pipeline is None or (
            report.get("operational_reason_code") not in {"finding_verification_incomplete", "finding_publication_incomplete"}
            and not delivery_pause(workspace.root)
            and not pending_finding_checks(workspace.root)
        ):
            return result
        # The mathematical binding has already been checked. Resume only its
        # committed terminal phase to finish result publication.
        state = load_director_checkpoint(workspace)
        if state["phase"] not in {"FINALIZE", "STOPPED", "FAILED"}:
            raise RecoveryError("invalid_director_checkpoint", "terminal result requires a terminal checkpoint")
        return continue_pipeline(RecoveryRequest(
            workspace=workspace, checkpoint=state,
            continuation_action=state["phase"],
        ))

    state = load_director_checkpoint(workspace)
    if continue_pipeline is None:
        raise RecoveryError(
            "continuation_hook_unavailable",
            "the run is nonterminal, but no pipeline continuation hook was supplied",
        )
    state = _restore_operational_checkpoint(workspace, state) or state
    return continue_pipeline(
        RecoveryRequest(
            workspace=workspace,
            checkpoint=state,
            continuation_action=state["phase"],
        )
    )
