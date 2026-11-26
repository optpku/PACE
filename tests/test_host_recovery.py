import json
import os
import signal
import subprocess
import sys

import pytest

from pace.runtime.input_protection import protected_inputs, recover_open_input_snapshots
from pace.runtime.llm import recover_pending_completion
from pace.runtime.agent_environment import call_environment
from pace.artifacts.calls import begin_call
from pace.artifacts.events import append_event, read_events
from pace.artifacts.workspace import RunWorkspace, list_problems, reconcile_run_status
from pace.runtime.semantic import SemanticCallSpec, _semantic_call_once


SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string"}},
    "required": ["action"],
    "additionalProperties": False,
}


def workspace(tmp_path):
    ws = RunWorkspace.create(tmp_path, "topic")
    ws.write_text(ws.work_ref("input/problem.md"), "Prove the current problem.")
    ws.write_json(ws.work_ref("input/target_contract.json"), {"contract_id": "TARGET"})
    return ws


def test_killed_host_is_reconciled_and_active_host_is_preserved(tmp_path):
    ws = workspace(tmp_path)
    script = """
import sys,time
from pathlib import Path
from pace.artifacts.workspace import RunWorkspace
from pace.runtime.sessions import run_session
ws=RunWorkspace.open_existing(Path(sys.argv[1]))
with ws.execution_lock():
    ws.finish_turn('running')
    with run_session(ws.root):
        print('locked',flush=True)
        time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(ws.root)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        assert list_problems(tmp_path)[0]["status"] == "running"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        state = list_problems(tmp_path)[0]
        assert state["status"] == "interrupted"
        assert state["interruption_reason"] == "host_exit_unobserved"
        assert state["ended_at"] is None
        sessions = json.loads(ws.path(ws.work_ref("state/sessions.json")).read_text())
        assert sessions[-1]["status"] == "end_unobserved"
        assert sessions[-1]["elapsed_seconds"] == 0
        assert "ended_at" not in sessions[-1]
        before = ws.path("work/state/current_turn.json").read_bytes()
        assert list_problems(tmp_path)[0]["status"] == "interrupted"
        assert ws.path("work/state/current_turn.json").read_bytes() == before
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_live_owner_and_reused_pid_are_distinguished(tmp_path):
    ws = workspace(tmp_path)
    assert reconcile_run_status(ws.root)["status"] == "running"
    state = json.loads(ws.path("work/state/current_turn.json").read_text())
    state["host_process"]["started_identity"] = "another process incarnation"
    ws.write_json("work/state/current_turn.json", state)
    assert reconcile_run_status(ws.root)["status"] == "interrupted"


def test_lock_preserves_legacy_run_without_host_identity(tmp_path):
    ws = workspace(tmp_path)
    with ws.execution_lock():
        state = json.loads(ws.path("work/state/current_turn.json").read_text())
        state.pop("host_process")
        ws.write_json("work/state/current_turn.json", state)
        assert reconcile_run_status(ws.root)["status"] == "running"
        ws.finish_turn("completed")
    assert list_problems(tmp_path)[0]["status"] == "completed"


def test_owned_child_keeps_run_active_until_it_exits(tmp_path):
    from pace.runtime.processes import _process_identity

    ws = workspace(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ws.write_json(
            "work/state/current_turn.json", {"turn_id": "turn001", "status": "running"}
        )
        ws.write_json(
            f"work/state/model_processes/{child.pid}.json", _process_identity(child.pid)
        )
        assert list_problems(tmp_path)[0]["status"] == "running"
        child.terminate()
        child.wait(timeout=5)
        assert list_problems(tmp_path)[0]["status"] == "interrupted"
        assert not ws.path(f"work/state/model_processes/{child.pid}.json").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def pending(ws, role="director"):
    meta = {"round_index": 1, "task_id": "round-1", "target_contract_id": "TARGET"}
    inputs = (ws.work_ref("input/problem.md"),)
    ref = begin_call(
        ws.root,
        call_id="pending",
        role=role,
        stage="decide",
        prompt="Decide the next step.",
        input_refs=inputs,
        metadata=call_environment(ws.root, role, meta),
    )
    append_event(
        ws.root,
        "SEMANTIC_CALL_STARTED",
        call_id="pending",
        call_name="decide",
        role=role,
        stage="decide",
        call_ref=ref,
    )
    directory = (ws.root / ref).parent
    (directory / "output_schema.json").write_text(json.dumps(SCHEMA))
    attempt = directory / "attempts/0001"
    attempt.mkdir(parents=True)
    append_event(
        ws.root,
        "TRANSPORT_ATTEMPT_STARTED",
        call_id="pending",
        call_name="decide",
        role=role,
        stage="decide",
        invocation_id="original",
        attempt_index=1,
    )
    protection = protected_inputs(
        ws.root,
        (*inputs, str((directory / "prompt.md").relative_to(ws.root))),
        directory,
    )
    protection.arm()
    (attempt / "stdout.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"type": "thread.started", "thread_id": "original-thread"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"action":"DISPATCH"}'},
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            ]
        )
        + "\n"
    )
    return (
        ref,
        attempt,
        dict(
            prompt="Decide the next step.",
            call_name="decide",
            role=role,
            input_refs=inputs,
            metadata=meta,
            output_schema=SCHEMA,
        ),
    )


def test_recovered_director_keeps_its_native_codex_session(tmp_path, monkeypatch):
    from pathlib import Path
    from pace.runtime.agent_environment import director_thread

    monkeypatch.setenv("PACE_CODEX_ENV_ROOT", str(tmp_path / "role-environments"))
    ws = workspace(tmp_path)
    ref, _, args = pending(ws, role="GLOBAL_RESEARCH_DIRECTOR")
    recover_open_input_snapshots(ws.root)
    result = recover_pending_completion(ws.root, **args)
    assert result is not None
    record = json.loads((ws.root / ref).read_text())
    assert record["thread_id"] == "original-thread"
    assert director_thread(Path(record["agent_home"])) == "original-thread"


def test_completed_response_reenters_semantic_acceptance_without_inference(tmp_path):
    ws = workspace(tmp_path)
    ref, attempt, args = pending(ws)
    raw = (attempt / "stdout.jsonl").read_bytes()
    source = ws.path(ws.work_ref("input/problem.md")).read_bytes()
    recover_open_input_snapshots(ws.root)

    class NoInference:
        def complete_structured_result(self, *args, **kwargs):
            raise AssertionError("completed model task was called again")

    accepted = []
    result = _semantic_call_once(
        NoInference(),
        args["prompt"],
        workdir=ws.root,
        call_name="decide",
        spec=SemanticCallSpec(
            "director", SCHEMA, "v1", "Host", on_accept=accepted.append
        ),
        input_refs=args["input_refs"],
        host_metadata=args["metadata"],
    )
    assert result.payload == {"action": "DISPATCH"}
    assert accepted == [{"action": "DISPATCH"}]
    assert result.host["call_id"] == "pending"
    assert result.host["invocation_ids"] == ["original"]
    assert json.loads((ws.root / ref).read_text())["status"] == "SUCCEEDED"
    assert (attempt / "stdout.jsonl").read_bytes() == raw
    assert ws.path(ws.work_ref("input/problem.md")).read_bytes() == source
    assert recover_pending_completion(ws.root, **args) is None
    assert (
        len(
            [
                e
                for e in read_events(ws.root)
                if e["event_kind"] == "SEMANTIC_CALL_STARTED"
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "change", ["input", "prompt", "schema", "task", "terminal", "rejected", "restored"]
)
def test_response_recovery_rejects_changed_or_incomplete_evidence(tmp_path, change):
    ws = workspace(tmp_path)
    ref, attempt, args = pending(ws)
    if change == "restored":
        ws.path(args["input_refs"][0]).write_text("Unauthorized edit")
    recover_open_input_snapshots(ws.root)
    if change == "input":
        ws.path(args["input_refs"][0]).write_text("A revised task")
    elif change == "prompt":
        args["prompt"] = "Another instruction"
    elif change == "schema":
        args["output_schema"] = {"type": "object"}
    elif change == "task":
        args["metadata"]["task_id"] = "another-task"
    elif change == "terminal":
        (attempt / "stdout.jsonl").write_text('{"type":"turn.failed"}\n')
    elif change == "rejected":
        d = json.loads((ws.root / ref).read_text())
        d["status"] = "REPRESENTATION_FAILED"
        (ws.root / ref).write_text(json.dumps(d))
    assert recover_pending_completion(ws.root, **args) is None


def test_writing_does_not_charge_the_mathematical_budget(tmp_path, monkeypatch):
    from pace.runtime import sessions
    from pace.artifacts.events import append_event

    ws = workspace(tmp_path)
    policy = ws.work_ref("state/research_execution_policy.json")
    ws.write_json(policy, {"max_calls": 2, "max_seconds": 10})
    clock = [0.0]
    monkeypatch.setattr(sessions.time, "monotonic", lambda: clock[0])
    with sessions.run_session(ws.root):
        append_event(ws.root, "SEMANTIC_CALL_STARTED", call_name="research_reporter_round1")
        clock[0] = 3
    with sessions.run_session(ws.root, writing_request_ref="request.md"):
        append_event(ws.root, "SEMANTIC_CALL_STARTED", call_name="research_report_writer")
        clock[0] = 103
    with sessions.run_session(ws.root):
        assert sessions.budget_exhausted(ws.root) == ""
        clock[0] += 7
        assert sessions.budget_exhausted(ws.root) == "ACTIVE_TIME_BUDGET_EXHAUSTED"
        append_event(ws.root, "SEMANTIC_CALL_STARTED", call_name="proof_contract_auditor")
        assert sessions.budget_exhausted(ws.root) == "TOTAL_CALL_BUDGET_EXHAUSTED"
