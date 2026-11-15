"""Execute a Director-selected pair using the existing specialist/verifier path.

Only the parent schedules. Workers have private checkpoints and unique round
outputs; they never call a Director or modify the parent state.
"""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from pace.contracts.research.director_protocol import DIRECTOR_PROTOCOL_SCHEMA_VERSION
from pace.runtime.context_state import commit_verified_progress
from pace.runtime.output.memory import commit_memory_notes
from pace.runtime.processes import cancellable_model_calls
from pace.orchestrator.recovery import load_operational_checkpoint
from pace.orchestrator.director_led import (
    DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION, DirectorBudgetExhausted, DirectorLedEngine, DirectorLedState,
    _coerce_decision, _read_json, _write_json,
)


def reserve_batch(engine, state, decision):
    proposed = (replace(decision, parallel_tasks=()), *decision.parallel_tasks)
    slots = engine.max_rounds - state.round_index if engine.max_rounds is not None else len(proposed)
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
        raise DirectorBudgetExhausted("no proposed task fits the remaining proof states")
    first = state.round_index + 1
    manifest_ref = engine._round_ref(first, "batch.json")
    reserved = replace(state, round_index=state.round_index + len(tasks),
                       proof_states_used=state.max_proof_states - credits,
                       phase="PENDING_BATCH", pending_assignment_ref=manifest_ref)
    _write_json(engine.workdir / manifest_ref, {
        "reserved_state": reserved.to_json(),
        "first_round": first,
        "tasks": [task.to_json() for task in tasks],
        "input_snapshot": state.to_json(),
    })
    engine._persist_state(reserved)
    return reserved


class _TaskExecutor(DirectorLedEngine):
    """Same execution machinery, with a task-local checkpoint destination."""

    def __init__(self, parent, round_index, first_round):
        self.task_round = round_index
        self.first_round = first_round
        super().__init__(
            workdir=parent.workdir, director=parent.director, composer=parent.composer,
            specialists=parent.specialists, verifier=parent.verifier,
            target_contract_id=parent.target_contract_id, max_proof_states=parent.max_proof_states,
            # The parent Host reports once after the batch is merged; workers
            # must never race on the shared user-facing report.
            on_round_committed=None,
        )
        self.parent_task_id = f"batch-{first_round:04d}"
        self.commit_progress = False

    @property
    def state_path(self):
        return self.workdir / self._round_ref(self.task_round, "task_state.json")

    def _round_ref(self, round_index, filename):
        # Parent and worker failures can share a reserved round number.
        if filename == "diagnostics.json":
            filename = "task_diagnostics.json"
        return super()._round_ref(round_index, filename)

    def _progress_snapshot_ref(self, round_index):
        return self._round_ref(self.first_round, "progress_snapshot.json")


def _execute(parent, snapshot, task, index, first_round, control_context):
    worker = _TaskExecutor(parent, index, first_round)
    if worker.state_path.is_file():
        state = worker._load_state()
    else:
        directive = worker._round_ref(index, "director_decision.json")
        assignment = worker._round_ref(index, "assignment/task.json")
        _write_json(worker.workdir / directive, {
            "schema_version": DIRECTOR_PROTOCOL_SCHEMA_VERSION, "round_index": index, **task,
        })
        decision = _coerce_decision(task)
        _write_json(worker.workdir / assignment, {
            "schema_version": DIRECTOR_LED_ASSIGNMENT_SCHEMA_VERSION,
            "round_index": index, **{k: task[k] for k in (
                "agent_id", "prompt_brief")},
            "input_refs": list(worker._assignment_input_refs(decision, index)),
            "output_ref": worker._default_output_ref(task["agent_id"], index),
            "director_decision_ref": directive,
        })
        state = replace(snapshot, round_index=index, phase="PENDING_SPECIALIST",
                        pending_dispatch_ref=directive, pending_assignment_ref=assignment,
                        last_directive_ref=directive, last_assignment_ref="",
                        pending_prompt_ref="", pending_verifier_request_ref="",
                        last_composer_manifest_ref="", last_specialist_result_ref="",
                        last_verification_ref="", candidate_ref="", diagnostics_ref="",
                        terminal_result="", stop_reason="")
        worker._persist_state(state)
    if state.phase == "FAILED":
        checkpoint = load_operational_checkpoint(worker.workdir, state.to_json())
        if checkpoint is None:
            raise ValueError("failed batch worker has no active operational checkpoint")
        archive = worker.state_path.parent / "recovery"
        archive.mkdir(exist_ok=True)
        attempt = archive / f"attempt-{len(list(archive.iterdir())) + 1:04d}"
        attempt.mkdir()
        shutil.copyfile(worker.workdir / state.diagnostics_ref, attempt / "diagnostics.json")
        _write_json(attempt / "failed_state.json", state.to_json())
        state = DirectorLedState.from_json(checkpoint)
        worker._persist_state(state)
    if state.phase == "PENDING_SPECIALIST":
        return worker._run_pending_specialist(state, control_context=control_context, recovering=True)
    if state.phase == "VERIFY":
        return worker._resume_verification(state, control_context=control_context)
    return state


def run_batch(engine, state, *, control_context):
    manifest_ref = engine._require_run_local_file_ref(
        state.pending_assignment_ref, field="batch manifest")
    manifest = _read_json(engine.workdir / manifest_ref)
    if manifest["reserved_state"] != state.to_json():
        raise ValueError("batch manifest differs from the committed reservation")
    snapshot = DirectorLedState.from_json(manifest["input_snapshot"])
    snapshot = replace(snapshot, proof_states_used=state.proof_states_used)
    tasks = manifest["tasks"]
    first = manifest["first_round"]
    from pace.runtime.output.events import publish_progress
    publish_progress(engine.workdir, task_id=f"batch-{first:04d}", stage="batch", status="started", round_index=first, task_count=len(tasks), artifact_refs=(manifest_ref,))
    # Reservations were committed before any worker starts. Recovered workers
    # consume the same slots and keep their already committed handoffs.
    with cancellable_model_calls(engine.workdir) as calls:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="director-task") as pool:
            futures = []
            try:
                for i, task in enumerate(tasks):
                    futures.append(pool.submit(_execute, engine, snapshot, task,
                                               first + i, first, control_context))
                results = [future.result() for future in futures]
            except (KeyboardInterrupt, SystemExit):
                # ThreadPoolExecutor.__exit__ waits for running workers. Stop
                # their owned processes first so Ctrl-C preserves the batch
                # reservation instead of waiting for model timeouts.
                calls.cancel()
                for future in futures:
                    future.cancel()
                raise
    from pace.runtime.output.events import write_call_summary
    write_call_summary(engine.workdir)
    result_ref = engine._round_ref(first, "batch_result.json")
    _write_json(engine.workdir / result_ref, {
        "reserved_through_round": state.round_index,
        "tasks": [{"round_index": item.round_index, "phase": item.phase,
                   "result_ref": item.last_specialist_result_ref,
                   "verification_ref": item.last_verification_ref,
                   "terminal_result": item.terminal_result} for item in results],
    })
    winner = next((item for item in results if item.terminal_result == "PASS"), None)
    if winner is None and any(item.phase == "FAILED" for item in results):
        return engine._fail_round(
            state, state.round_index, "PENDING_BATCH",
            RuntimeError("A batch worker has an unfinished operational failure"),
        )
    selected = winner or results[-1]
    completed = tuple(dict.fromkeys(ref for item in results for ref in item.completed_result_refs))
    # A terminal PASS keeps the winning candidate's exact round binding; the
    # batch result records all reserved slots, including its completed sibling.
    merged = replace(selected, round_index=selected.round_index if winner else state.round_index,
                     phase="FINALIZE" if winner else "DIRECTOR",
                     terminal_result="PASS" if winner else "",
                     stop_reason=selected.stop_reason if winner else "",
                     pending_dispatch_ref="", pending_assignment_ref="",
                     pending_prompt_ref="", pending_verifier_request_ref="",
                     completed_result_refs=completed)
    verifications = {
        item.last_verification_ref: _read_json(engine.workdir / item.last_verification_ref)["verification"]
        for item in results if item.last_verification_ref
    }
    verification_values = tuple(verifications.values())
    commit_verified_progress(
        engine.workdir, verification_values,
        current_progress_ref=verifications.get(selected.last_verification_ref, {}).get("verified_progress_ref") or "",
    )
    commit_memory_notes(engine.workdir, result_refs=completed,
                                verification_values=verification_values)
    committed = engine._persist_state(merged)
    publish_progress(engine.workdir, task_id=f"batch-{first:04d}", stage="batch", status="completed", round_index=first, task_count=len(tasks), artifact_refs=(result_ref, *completed))
    return committed
