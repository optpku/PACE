"""Execute a Director-selected pair using the existing specialist/verifier path.

Only the parent schedules. Workers have private checkpoints and unique round
outputs; they never call a Director or modify the parent state.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from pace.agents.director.context import validate_context
from pace.agents.director.executor import AssignmentExecutor
from pace.agents.director.recovery import load_operational_checkpoint
from pace.agents.director.rounds import _coerce_decision, _read_json
from pace.agents.director.state import (
    DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
    DirectorBudgetExhausted,
    DirectorLedState,
)
from pace.artifacts.workspace import _write_json_atomic as _write_json
from pace.contracts.research.director_protocol import DIRECTOR_PROTOCOL_SCHEMA_VERSION
from pace.contracts.research.memory import binding, check_binding
from pace.runtime.processes import cancellable_model_calls


def reserve_batch(engine, state, decision):
    proposed = (replace(decision, parallel_tasks=()), *decision.parallel_tasks)
    slots = (
        engine.max_rounds - state.round_index
        if engine.max_rounds is not None
        else len(proposed)
    )
    credits = state.max_proof_states - state.proof_states_used
    tasks = []
    for task in proposed:
        if len(tasks) == slots:
            break
        cost = engine._proof_state_cost(task, state)
        if cost <= credits:
            tasks.append(task)
            credits -= cost
    if not tasks:
        raise DirectorBudgetExhausted(
            "no proposed task fits the remaining proof states"
        )
    first = state.round_index + 1
    manifest_ref = engine._round_ref(first, "batch.json")
    reserved = replace(
        state,
        round_index=state.round_index + len(tasks),
        proof_states_used=state.max_proof_states - credits,
        phase="PENDING_BATCH",
        pending_assignment_ref=manifest_ref,
        last_batch_result_ref="",
        last_batch_result_sha256="",
    )
    # Freeze every input before any worker starts. Siblings never observe a new route.
    frozen = []
    for offset, task in enumerate(tasks):
        index = first + offset
        # All workers use the first round's already committed progress snapshot.
        refs = engine._assignment_input_refs(task, index, snapshot_round=first)
        frozen.append(
            {
                "round_index": index,
                "input_refs": list(refs),
                **engine._context_binding(index),
            }
        )
    _write_json(
        engine.workdir / manifest_ref,
        {
            "reserved_state": reserved.to_json(),
            "first_round": first,
            "tasks": [task.to_json() for task in tasks],
            "input_snapshot": state.to_json(),
            "contexts": frozen,
        },
    )
    engine._persist_state(reserved)
    return reserved


def _execute(parent, snapshot, task, index, first_round, control_context):
    worker = AssignmentExecutor(
        workdir=parent.workdir,
        composer=parent.composer,
        specialists=parent.specialists,
        target_contract_id=parent.target_contract_id,
        verifier=parent.verifier,
        reporter=parent.reporter,
        checkpoint_ref=parent._round_ref(index, "task_state.json"),
        snapshot_round=first_round,
        parent_task_id=f"batch-{first_round:04d}",
        commit_progress=False,
        commit_memory=parent.commit_memory_callback,
    )
    if worker.state_path.is_file():
        state = worker._load_state()
    else:
        directive = worker._round_ref(index, "director_decision.json")
        assignment = worker._round_ref(index, "assignment/task.json")
        _write_json(
            worker.workdir / directive,
            {
                "schema_version": "research_director_decision/v3"
                if task.get("next_step_kind")
                else DIRECTOR_PROTOCOL_SCHEMA_VERSION,
                "round_index": index,
                **task,
            },
        )
        decision = _coerce_decision(task)
        _write_json(
            worker.workdir / assignment,
            {
                "schema_version": DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
                "round_index": index,
                **{k: task[k] for k in ("agent_id", "prompt_brief")},
                "input_refs": list(worker._assignment_input_refs(decision, index)),
                "output_ref": worker._default_output_ref(task["agent_id"], index),
                "director_decision_ref": directive,
                **worker._context_binding(index),
            },
        )
        state = replace(
            snapshot,
            round_index=index,
            phase="PENDING_SPECIALIST",
            pending_dispatch_ref=directive,
            pending_assignment_ref=assignment,
            last_directive_ref=directive,
            last_assignment_ref="",
            pending_prompt_ref="",
            pending_verifier_request_ref="",
            last_composer_manifest_ref="",
            last_specialist_result_ref="",
            last_verification_ref="",
            last_batch_result_ref="",
            last_batch_result_sha256="",
            candidate_ref="",
            diagnostics_ref="",
            terminal_result="",
            stop_reason="",
        )
        worker._persist_state(state)
    if state.phase == "FAILED":
        checkpoint = load_operational_checkpoint(worker.workdir, state.to_json())
        if checkpoint is None:
            raise ValueError("failed batch worker has no active operational checkpoint")
        archive = worker.state_path.parent / "recovery"
        archive.mkdir(exist_ok=True)
        attempt = archive / f"attempt-{len(list(archive.iterdir())) + 1:04d}"
        attempt.mkdir()
        shutil.copyfile(
            worker.workdir / state.diagnostics_ref, attempt / "diagnostics.json"
        )
        _write_json(attempt / "failed_state.json", state.to_json())
        state = DirectorLedState.from_json(checkpoint)
        worker._persist_state(state)
    if state.phase == "PENDING_SPECIALIST":
        return worker._run_pending_specialist(
            state, control_context=control_context, recovering=True
        )
    if state.phase == "VERIFY":
        return worker._resume_verification(state, control_context=control_context)
    return state


def run_batch(engine, state, *, control_context):
    manifest_ref = engine._require_run_local_file_ref(
        state.pending_assignment_ref, field="batch manifest"
    )
    manifest = _read_json(engine.workdir / manifest_ref)
    if manifest["reserved_state"] != state.to_json():
        raise ValueError("batch manifest differs from the committed reservation")
    snapshot = DirectorLedState.from_json(manifest["input_snapshot"])
    snapshot = replace(snapshot, proof_states_used=state.proof_states_used)
    tasks = manifest["tasks"]
    first = manifest["first_round"]
    for row in manifest["contexts"]:
        if "context_binding" in row:
            expected = engine._round_ref(row["round_index"], "assignment/context.json")
            if row["context_binding"]["ref"] != expected:
                raise ValueError("batch context belongs to another reservation")
            check_binding(engine.workdir, row["context_binding"])
            frozen = validate_context(engine.workdir, expected)
            if row["input_refs"] != [
                *frozen["required_refs"],
                expected,
                frozen["route_context"]["ref"],
            ]:
                raise ValueError("batch inputs differ from the reservation")
    from pace.artifacts.events import publish_progress

    publish_progress(
        engine.workdir,
        task_id=f"batch-{first:04d}",
        stage="batch",
        status="started",
        round_index=first,
        task_count=len(tasks),
        artifact_refs=(manifest_ref,),
    )
    # Reservations were committed before any worker starts. Recovered workers
    # consume the same slots and keep their already committed handoffs.
    with cancellable_model_calls(engine.workdir) as calls:
        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="director-task"
        ) as pool:
            futures = []
            try:
                for i, task in enumerate(tasks):
                    futures.append(
                        pool.submit(
                            _execute,
                            engine,
                            snapshot,
                            task,
                            first + i,
                            first,
                            control_context,
                        )
                    )
                results = [future.result() for future in futures]
            except (KeyboardInterrupt, SystemExit):
                # ThreadPoolExecutor.__exit__ waits for running workers. Stop
                # their owned processes first so Ctrl-C preserves the batch
                # reservation instead of waiting for model timeouts.
                calls.cancel()
                for future in futures:
                    future.cancel()
                raise
    from pace.artifacts.events import write_call_summary

    write_call_summary(engine.workdir)
    result_ref = engine._round_ref(first, "batch_result.json")
    _write_json(
        engine.workdir / result_ref,
        {
            "reserved_through_round": state.round_index,
            "tasks": [
                {
                    "round_index": item.round_index,
                    "phase": item.phase,
                    "result_ref": item.last_specialist_result_ref,
                    "verification_ref": item.last_verification_ref,
                    "terminal_result": item.terminal_result,
                }
                for item in results
            ],
        },
    )
    winner = next((item for item in results if item.terminal_result == "PASS"), None)
    from pace.contracts.research.memory import reporter_owned

    # A winning proof does not discard a sibling's required reporting handoff.
    if any(item.phase == "FAILED" for item in results) and (
        winner is None or reporter_owned(engine.workdir)
    ):
        return engine._fail_round(
            state,
            state.round_index,
            "PENDING_BATCH",
            RuntimeError("A batch worker has an unfinished operational failure"),
        )
    selected = winner or results[-1]
    completed = tuple(
        dict.fromkeys(ref for item in results for ref in item.completed_result_refs)
    )
    # A terminal PASS keeps the winning candidate's exact round binding; the
    # batch result records all reserved slots, including its completed sibling.
    merged = replace(
        selected,
        round_index=selected.round_index if winner else state.round_index,
        phase="FINALIZE" if winner else "DIRECTOR",
        terminal_result="PASS" if winner else "",
        stop_reason=selected.stop_reason if winner else "",
        pending_dispatch_ref="",
        pending_assignment_ref="",
        pending_prompt_ref="",
        pending_verifier_request_ref="",
        completed_result_refs=completed,
        last_batch_result_ref=result_ref,
        last_batch_result_sha256=binding(engine.workdir, result_ref)["sha256"],
    )
    committed = engine._persist_state(merged)
    publish_progress(
        engine.workdir,
        task_id=f"batch-{first:04d}",
        stage="batch",
        status="completed",
        round_index=first,
        task_count=len(tasks),
        artifact_refs=(result_ref, *completed),
    )
    return committed
