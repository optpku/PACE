"""User research limits bound every dispatch while preserving verification and resume."""

import json
import re
from pathlib import Path

import pytest

from pace import cli
from pace.orchestrator.director_led import DirectorLedEngine
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.prompt_composer import PromptComposerResult
from pace.runtime.prompts import materialize_run_resources
from test_finding_publication import ProofPublicationTransport


class RoundLimitTransport(ProofPublicationTransport):
    def __init__(self, agent="survey"):
        super().__init__("NONE")
        self.agent = agent
        self.round_prompts = []

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        root = Path(workdir)
        if call_name == "problem_orientation_extract":
            payload = {"target_clauses": [{"start_line": 1, "end_line": 1}],
                       "recommended_mode": "research", "mode_reason": "Investigate the exact target."}
        elif call_name == "problem_research_decision":
            (root / work_ref(root, "input/problem_brief.md")).write_text("# Integer addition\n\nProve 2+2=4.\n")
            payload = {"proof_assessment": {"difficulty": "research", "plan_node_level": 1,
                                            "dependency_depth_level": 1}}
        elif call_name.startswith("global_research_director_"):
            self.round_prompts.append(prompt)
            payload = {"action": "DISPATCH", "agent_id": self.agent,
                       "prompt_brief": "Investigate the stated equality within its original scope.",
                       "input_refs": [work_ref(root, "input/problem.md")]}
        else:
            return super().complete_structured_result(prompt, workdir=workdir,
                       call_name=call_name, ledger_context=ledger_context, **kwargs)
        return self._reply(json.dumps(payload), workdir=root, call_name=call_name,
                           ledger_context=ledger_context, structured=True)


def test_cli_stops_uncharged_research_and_saves_limits_per_turn(tmp_path, monkeypatch, capsys):
    transport = RoundLimitTransport()
    monkeypatch.setattr("pace.orchestrator.pipeline.build_llm", lambda **settings: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "bounded",
                     "--output", str(tmp_path), "--max-rounds", "2", "--json"]) == 0
    root = tmp_path / "bounded"
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert report["reason_code"] == "research_round_limit_reached"
    assert report["run_status"] == "completed"
    assert report["mathematical_status"] == "OPEN"
    state = json.loads((root / "work/turn001/state/director_state.json").read_text())
    assert state["round_index"] == 2
    assert state["proof_states_used"] == 0
    assert not (root / "work/turn001/round3").exists()
    assert len(transport.round_prompts) == 2
    assert "Remaining research rounds (including the next dispatch): 2" in transport.round_prompts[0]
    final = (root / "work/turn001/final_report.json").read_bytes()
    calls = len(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert (root / "work/turn001/final_report.json").read_bytes() == final
    assert len(transport.calls) == calls
    message = tmp_path / "follow-up.md"
    message.write_text("Continue investigating the same equality.")
    assert cli.main(["continue", "--run-dir", str(root), "--message", str(message),
                     "--max-rounds", "1", "--json"]) == 0
    for turn, limit in (("turn001", 2), ("turn002", 1)):
        policy = json.loads((root / "work" / turn / "state/research_execution_policy.json").read_text())
        state = json.loads((root / "work" / turn / "state/director_state.json").read_text())
        assert policy["max_rounds"] == limit
        assert state["round_index"] == limit


def test_last_round_candidate_is_verified_before_round_limit(tmp_path, monkeypatch, capsys):
    transport = RoundLimitTransport("prover")
    monkeypatch.setattr("pace.orchestrator.pipeline.build_llm", lambda **settings: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "verified",
                     "--output", str(tmp_path), "--max-rounds", "1", "--json"]) == 0
    root = tmp_path / "verified"
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert report["result"] == "PASS"
    assert report["reason_code"] == "director_led_verified"
    assert report["director_state"]["round_index"] == 1
    assert report["director_state"]["proof_states_used"] == 0
    assert len(transport.round_prompts) == 1


@pytest.fixture
def research_workspace(tmp_path):
    ws = RunWorkspace.create(tmp_path, "research")
    ws.write_text(ws.work_ref("input/problem.md"), "Investigate the fixed mathematical target.")
    ws.write_json(ws.work_ref("input/target_contract.json"),
                  {"contract_id": "TARGET", "literal_target": "Investigate the fixed target."})
    materialize_run_resources(ws.root, resource_variant="standard")
    return ws


@pytest.mark.parametrize("limit", [1, 2])
def test_parallel_dispatch_reserves_only_remaining_rounds(research_workspace, limit):
    ws = research_workspace
    calls = []

    def specialist(**kwargs):
        calls.append(kwargs["agent_id"])
        (ws.root / kwargs["output_ref"]).write_text("The assigned local question remains open.")
        return {"agent_id": kwargs["agent_id"], "status": "SUCCEEDED",
                "deliverable_ref": kwargs["output_ref"]}

    task = {"action": "DISPATCH", "agent_id": "survey", "prompt_brief": "Inspect existing results.",
            "input_refs": [ws.work_ref("input/problem.md")]}
    tasks = [{**task, "agent_id": "planner"}]
    engine = DirectorLedEngine(workdir=ws.root,
        director=lambda **_: {**task, "parallel_tasks": tasks},
        composer=lambda **_: PromptComposerResult(final_prompt="Execute the fixed task.", call_id="composer"),
        specialists={agent: specialist for agent in ("survey", "planner", "research")},
        target_contract_id="TARGET", max_rounds=limit)
    result = engine.run()
    assert result.phase == "FINALIZE"
    assert result.state.round_index == limit
    assert set(calls) == ({"survey"} if limit == 1 else {"survey", "planner"})
    assert result.state.proof_states_used == 0
    assert not ws.path(ws.work_ref(f"round{limit + 1}")).exists()


def test_resume_finishes_the_reserved_last_round_without_redispatch(research_workspace):
    ws = research_workspace
    attempts = []

    def specialist(**kwargs):
        attempts.append(kwargs["round_index"])
        if len(attempts) == 1:
            raise KeyboardInterrupt
        (ws.root / kwargs["output_ref"]).write_text("The source search is complete; the target remains open.")
        return {"agent_id": "survey", "status": "SUCCEEDED", "deliverable_ref": kwargs["output_ref"]}

    engine = DirectorLedEngine(workdir=ws.root,
        director=lambda **_: {"action": "DISPATCH", "agent_id": "survey", "prompt_brief": "Inspect sources.",
                             "input_refs": [ws.work_ref("input/problem.md")]},
        composer=lambda **_: PromptComposerResult(final_prompt="Inspect the fixed sources.", call_id="composer"),
        specialists={"survey": specialist}, target_contract_id="TARGET", max_rounds=1)
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    assert json.loads(engine.state_path.read_text())["round_index"] == 1
    engine.director = lambda **_: pytest.fail("The reserved last round must resume without a new decision.")
    result = engine.resume()
    assert result.phase == "FINALIZE"
    assert result.state.stop_reason == "RESEARCH_ROUND_LIMIT_REACHED"
    assert attempts == [1, 1]
    assert not ws.path(ws.work_ref("round2")).exists()


def credit_task(ws, agent):
    task = {"action": "DISPATCH", "agent_id": agent,
            "prompt_brief": "Investigate the fixed target using the existing mathematical evidence.",
            "input_refs": [ws.work_ref("input/problem.md")]}
    if agent == "smi":
        task["research_operation"] = "NEW_PROBLEM"
    return task


@pytest.mark.parametrize("agent", ["research", "smi"])
def test_paid_dispatch_stops_at_credit_limit_without_operational_failure(research_workspace, agent):
    ws = research_workspace
    calls = []

    def specialist(**kwargs):
        calls.append(kwargs["agent_id"])
        (ws.root / kwargs["output_ref"]).write_text("A partial result; the original target remains open.")
        return {"agent_id": agent, "status": "SUCCEEDED", "deliverable_ref": kwargs["output_ref"]}

    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: credit_task(ws, agent),
        composer=lambda **_: PromptComposerResult(final_prompt="Investigate the exact target.", call_id="composer"),
        specialists={agent: specialist}, target_contract_id="TARGET", max_proof_states=1, max_rounds=3)
    result = engine.run()
    assert calls == [agent]
    assert result.state.proof_states_used == 1
    assert result.phase == "FINALIZE"
    assert result.state.stop_reason == "PROOF_STATE_BUDGET_EXHAUSTED"
    assert not list(ws.root.glob("work/turn001/round*/diagnostics.json"))


@pytest.mark.parametrize("credits", [1, 2])
def test_parallel_research_and_smi_reserve_only_available_credits(research_workspace, credits):
    ws = research_workspace
    calls = []

    def specialist(**kwargs):
        calls.append(kwargs["agent_id"])
        (ws.root / kwargs["output_ref"]).write_text("The assigned mathematical work is preserved.")
        return {"agent_id": kwargs["agent_id"], "status": "SUCCEEDED",
                "deliverable_ref": kwargs["output_ref"]}

    task = credit_task(ws, "research")
    engine = DirectorLedEngine(workdir=ws.root,
        director=lambda **_: {**task, "parallel_tasks": [credit_task(ws, "smi")]},
        composer=lambda **_: PromptComposerResult(final_prompt="Investigate the exact target.", call_id="composer"),
        specialists={agent: specialist for agent in ("research", "smi")}, target_contract_id="TARGET",
        max_proof_states=credits, max_rounds=2)
    result = engine.run()
    assert result.state.proof_states_used == credits
    assert sorted(calls) == (["research"] if credits == 1 else ["research", "smi"])
    assert result.phase == "FINALIZE"


def test_parallel_free_task_can_use_the_last_round_at_zero_credits(research_workspace):
    ws = research_workspace
    calls = []

    def specialist(**kwargs):
        calls.append(kwargs["agent_id"])
        (ws.root / kwargs["output_ref"]).write_text("The fixed local question remains open.")
        return {"agent_id": kwargs["agent_id"], "status": "SUCCEEDED",
                "deliverable_ref": kwargs["output_ref"]}

    def director(**kwargs):
        if kwargs["round_index"] == 1:
            return credit_task(ws, "research")
        return {**credit_task(ws, "smi"), "parallel_tasks": [credit_task(ws, "survey")]}

    engine = DirectorLedEngine(workdir=ws.root, director=director,
        composer=lambda **_: PromptComposerResult(final_prompt="Use the existing evidence.", call_id="composer"),
        specialists={agent: specialist for agent in ("research", "smi", "survey")},
        target_contract_id="TARGET", max_proof_states=1, max_rounds=2)
    result = engine.run()
    assert calls == ["research", "survey"]
    assert result.state.proof_states_used == 1
    assert result.state.round_index == 2
    assert result.phase == "FINALIZE"


@pytest.mark.parametrize("agent", ["research", "smi"])
def test_paid_dispatch_resume_reuses_its_reserved_credit(research_workspace, agent):
    ws = research_workspace
    attempts = []

    def specialist(**kwargs):
        attempts.append(json.loads(engine.state_path.read_text())["proof_states_used"])
        if len(attempts) == 1:
            raise KeyboardInterrupt
        (ws.root / kwargs["output_ref"]).write_text("The interrupted local work is preserved.")
        return {"agent_id": agent, "status": "SUCCEEDED", "deliverable_ref": kwargs["output_ref"]}

    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: credit_task(ws, agent),
        composer=lambda **_: PromptComposerResult(final_prompt="Investigate the exact target.", call_id="composer"),
        specialists={agent: specialist}, target_contract_id="TARGET", max_proof_states=1, max_rounds=1)
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    result = engine.resume()
    assert attempts == [1, 1]
    assert result.state.proof_states_used == 1
    assert result.phase == "FINALIZE"


class CreditTransport(RoundLimitTransport):
    def __init__(self):
        super().__init__()
        self.prover_calls = 0

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        if call_name.startswith("global_research_director_"):
            self.agent = "research" if not self.round_prompts else "prover"
        if call_name.startswith("math_verifier_kernel_") and self.prover_calls == 1:
            return self._reply(json.dumps({"stage_status": "MATH_FAIL",
                "canonical_blockers": [{"reason": "The first draft has no complete derivation."}]}),
                workdir=workdir, call_name=call_name, ledger_context=ledger_context, structured=True)
        return super().complete_structured_result(prompt, workdir=workdir, call_name=call_name,
                    ledger_context=ledger_context, **kwargs)

    def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context):
        if call_name.startswith("director_led_prover_"):
            self.prover_calls += 1
        if call_name.startswith("director_led_research_") or (
            call_name.startswith("director_led_prover_") and self.prover_calls == 1
        ):
            output = Path(workdir) / re.search(r"## Deliverable\nWrite `([^`]+)`", prompt)[1]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("The target is plausible; its complete derivation is still missing.\n")
            return self._reply("Submitted the partial mathematical work.", workdir=workdir,
                               call_name=call_name, ledger_context=ledger_context, structured=False)
        return super().complete_unstructured_result(prompt, workdir=workdir, call_name=call_name,
                                                    ledger_context=ledger_context)


def test_cli_prover_can_assemble_and_repair_after_research_uses_last_credit(tmp_path, monkeypatch, capsys):
    transport = CreditTransport()
    monkeypatch.setattr("pace.orchestrator.pipeline.build_llm", lambda **settings: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "credits",
                     "--output", str(tmp_path), "--max-proof-states", "1",
                     "--max-rounds", "3", "--json"]) == 0
    root = tmp_path / "credits/work/turn001"
    report = json.loads((root / "final_report.json").read_text())
    assert report["result"] == "PASS"
    assert report["director_state"]["proof_states_used"] == 1
    assert report["director_state"]["round_index"] == 3
    assert transport.prover_calls == 2
