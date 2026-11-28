"""Exercise the six-role RCI graph through the ordinary research CLI."""

import json
from pathlib import Path

import pytest
from pace import cli
from pace.artifacts.workspace import work_ref
from pace.runtime.chat_api import WorkspaceTools
from test_research_round_limit import RoundLimitTransport

DIAGNOSIS = "M proves convergence under H. The original premises do not yet give H."


class RciTransport(RoundLimitTransport):
    def __init__(
        self,
        decisions,
        *,
        mechanism_count,
        probe=None,
        interrupt_review=False,
        resolve=False,
        local_gaps=(),
    ):
        super().__init__("rci")
        self.decisions = iter(decisions)
        self.mechanism_count = mechanism_count
        self.probe = probe
        self.interrupt_review = interrupt_review
        self.local_gaps = iter(local_gaps)
        self.resolve = resolve
        self.records = []
        self.mechanisms = []

    def complete_structured_result(
        self, prompt, *, workdir, call_name, ledger_context, **kwargs
    ):
        root = Path(workdir)
        role = ledger_context["role"]
        if call_name.startswith("global_research_director_"):
            question = work_ref(root, "input/problem.md")
            payload = {
                "action": "DISPATCH",
                "agent_id": "rci",
                "next_step_kind": "CONTINUE",
                "prompt_brief": "Explore the missing boundedness bridge.",
                "input_refs": [question],
                "archive_refs": [],
                "change_refs": [],
                "obligation_ids": ["target"],
                "branch_id": "b-target",
                "research_operation": "CONTINUE_PROBLEM",
                "research_problem_ref": question,
            }
        elif role.startswith("rci_"):
            record = json.loads((root / ledger_context["call_ref"]).read_text())
            directory = (root / record["source_prompt_ref"]).parent
            inputs = json.loads((directory / "prompt_assembly.json").read_text())[
                "phase_inputs"
            ]["input_refs"]
            self.records.append((role, directory, inputs))
            assert ledger_context["read_scope"] == "declared"
            assert not ledger_context.get("archive_refs")
            if role == "rci_mechanism_explorer":
                (directory / "change.md").write_text(
                    "One precise boundedness gap remains."
                )
                if self.resolve:
                    (directory / "closure_brief.md").write_text(
                        "A complete argument for the exact scoped question."
                    )
                    payload = {
                        "action": "CLAIM_RESOLVED",
                        "dependency_refs": inputs["CANDIDATE"],
                    }
                elif len(self.mechanisms) == self.mechanism_count:
                    payload = {"action": "HANDOFF"}
                else:
                    mechanism = directory / "mechanism.md"
                    mechanism.write_text(
                        "The conditional route establishes convergence under H."
                    )
                    self.mechanisms.append(mechanism.relative_to(root).as_posix())
                    payload = {"action": "PROPOSE_MECHANISM"}
            elif role == "rci_mechanism_critic":
                (directory / "review.md").write_text(DIAGNOSIS)
                if self.interrupt_review:
                    self.interrupt_review = False
                    raise KeyboardInterrupt
                if "LOCAL_REPAIR" in prompt:
                    gap = next(self.local_gaps, None)
                    payload = {"action": "LOCAL_REPAIR", "gap_id": gap} if gap else {"action": "ROUTE_DECISION"}
                else:
                    payload = {}
            elif role == "rci_conjecture_explorer":
                assert any(
                    DIAGNOSIS in (root / ref).read_text() for ref in inputs["EVIDENCE"]
                )
                action = next(self.decisions)
                (directory / "change.md").write_text(
                    f"Choose {action} based on the scoped diagnosis."
                )
                payload = {"action": action}
                if action == "PROPOSE_CONJECTURE":
                    (directory / "candidate.md").write_text(
                        "Under H, does the bounded sequence converge?"
                    )
                elif action == "REVIEW_MECHANISM" or (
                    action == "EXPLORE_MECHANISM" and self.mechanism_count == 2
                ):
                    payload["mechanism_ref"] = self.mechanisms[0]
                elif action.startswith("PROBE_"):
                    (directory / "probe_task.md").write_text(
                        "Can a nonnegative bounded sequence diverge?"
                    )
            elif role == "rci_candidate_critic":
                assert set(inputs) <= {
                    "TARGET_ANCHOR",
                    "WORK_ITEM",
                    "CANDIDATE",
                    "RELATED_CANDIDATE",
                    "SOURCE",
                }
                assert self.mechanisms[0] not in ledger_context["input_refs"]
                (directory / "review.md").write_text(
                    "This is a distinct, well-defined conditional question."
                )
                payload = {"action": "ACCEPT"}
            else:
                assert role == self.probe
                assert set(inputs) == {"PROBE_TASK"}
                tools = WorkspaceTools(root, ledger_context)
                assert not tools.readable(self.mechanisms[0])
                (directory / "review.md").write_text(
                    "A scoped probe result for the stated claim."
                )
                payload = {}
        else:
            return super().complete_structured_result(
                prompt,
                workdir=root,
                call_name=call_name,
                ledger_context=ledger_context,
                **kwargs,
            )
        return self._reply(
            json.dumps(payload),
            workdir=root,
            call_name=call_name,
            ledger_context=ledger_context,
            structured=True,
        )


def run_rci(tmp_path, monkeypatch, transport, *options):
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Investigate convergence of the bounded sequence.\n")
    assert (
        cli.main(
            [
                "run",
                "--problem",
                str(problem),
                "--output",
                str(tmp_path),
                "--problem-id",
                "rci",
                "--max-rounds",
                "1",
                "--json",
                *options,
            ]
        )
        == 0
    )
    root = tmp_path / "rci"
    round_dir = root / work_ref(root, "round1")
    result = json.loads((round_dir / "agent_result.json").read_text())
    outcome = json.loads((round_dir / "rci/attempt_001/outcome.json").read_text())
    assert result["status"] == "SUCCEEDED"
    assert outcome["outcome"] == "HANDOFF"
    assert set(outcome["published_refs"]) <= set(result["published_refs"])
    final = json.loads((root / work_ref(root, "final_report.json")).read_text())
    assert final["mathematical_status"] == "OPEN"
    return root, outcome


@pytest.mark.parametrize("repair", [True, False])
def test_same_diagnosis_can_repair_mechanism_or_revise_question(
    tmp_path, monkeypatch, repair
):
    transport = RciTransport(
        ["EXPLORE_MECHANISM", "HANDOFF"]
        if repair
        else ["PROPOSE_CONJECTURE", "REVIEW_MECHANISM", "HANDOFF"],
        mechanism_count=2,
    )
    root, outcome = run_rci(tmp_path, monkeypatch, transport)
    records = transport.records
    assert [role for role, _, _ in records[:3]] == [
        "rci_mechanism_explorer",
        "rci_mechanism_critic",
        "rci_conjecture_explorer",
    ]
    assert (records[1][1] / "review.md").read_text() == DIAGNOSIS
    first_question = work_ref(root, "input/problem.md")
    if repair:
        original = json.loads((records[0][1] / "output.json").read_text())
        revised = json.loads((records[3][1] / "output.json").read_text())
        assert records[3][2]["BASE_MECHANISM"] == [original["mechanism_ref"]]
        assert revised["mechanism_id"] == original["mechanism_id"]
        assert revised["mechanism_ref"] != original["mechanism_ref"]
        assert records[4][2]["PREVIOUS_MECHANISM"] == [original["mechanism_ref"]]
        assert outcome["candidate_ref"] == first_question
    else:
        assert records[3][0] == "rci_candidate_critic"
        assert records[4][2]["CANDIDATE"] == [outcome["candidate_ref"]]
        assert outcome["candidate_ref"] != first_question
        assert (root / first_question).read_text().startswith("Investigate convergence")
        review = json.loads((records[1][1] / "output.json").read_text())
        assert review["reviewed_candidate_ref"] == first_question
        recheck = json.loads((records[-2][1] / "output.json").read_text())
        assert recheck["reviewed_candidate_ref"] == outcome["candidate_ref"]
        assert recheck["reviewed_mechanism_ref"] == review["reviewed_mechanism_ref"]


def test_single_dispatch_can_develop_four_mechanisms(tmp_path, monkeypatch):
    transport = RciTransport(["EXPLORE_MECHANISM"] * 3 + ["HANDOFF"], mechanism_count=4)
    _, outcome = run_rci(tmp_path, monkeypatch, transport)
    assert len(transport.mechanisms) == len(set(transport.mechanisms)) == 4
    assert set(transport.mechanisms) <= set(outcome["published_refs"])


@pytest.mark.parametrize(
    "action,role",
    [
        ("PROBE_PROOF", "rci_blind_prover_probe"),
        ("PROBE_REFUTATION", "rci_refute_prover_probe"),
    ],
)
def test_probes_run_individually_on_their_own_statement(
    tmp_path, monkeypatch, action, role
):
    transport = RciTransport([action, "HANDOFF"], mechanism_count=1, probe=role)
    run_rci(tmp_path, monkeypatch, transport)
    assert [name for name, _, _ in transport.records if name.endswith("_probe")] == [
        role
    ]


def test_cli_continues_saved_rci_question_and_reuses_finished_phase(tmp_path, monkeypatch):
    from pace.agents.rci import assignment
    from pace.agents.director.context import load_index

    class ContinuationTransport(RoundLimitTransport):
        def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
            root = Path(workdir)
            question = work_ref(root, "round1/assignment/task.json")
            obstacle = work_ref(root, "round1/rci/attempt_001/001_conjecture_explorer/change.md")
            role = ledger_context["role"]
            if call_name.startswith("global_research_director_"):
                if call_name.endswith("0002"):
                    assert question in prompt and obstacle in prompt
                corrected = call_name.endswith("_receipt_correction")
                if corrected:
                    assert "This RCI work order already ran" in prompt
                payload = {
                    "action": "DISPATCH", "agent_id": "rci",
                    "next_step_kind": "CONTINUE", "branch_id": "b-target",
                    "obligation_ids": ["target"], "change_refs": [],
                    "prompt_brief": (
                        "Use a block estimate to address the recorded obstacle."
                        if corrected else "Explore whether a one-step estimate gives convergence."
                    ),
                    "research_operation": "CONTINUE_PROBLEM" if corrected else "NEW_PROBLEM",
                    "research_problem_ref": question if corrected else "",
                    "input_refs": [question] if corrected else [work_ref(root, "input/problem.md")],
                }
            elif role.startswith("rci_"):
                record = json.loads((root / ledger_context["call_ref"]).read_text())
                directory = (root / record["source_prompt_ref"]).parent
                if role == "rci_mechanism_explorer":
                    inputs = json.loads((directory / "prompt_assembly.json").read_text())[
                        "phase_inputs"
                    ]["input_refs"]
                    assert inputs["CANDIDATE"] == [question]
                    assert obstacle in inputs["SOURCE"]
                else:
                    assert role == "rci_conjecture_explorer"
                (directory / "change.md").write_text("The estimate leaves a boundedness gap.")
                payload = {"action": "HANDOFF"}
            elif role == "RESEARCH_REPORTER":
                note = next(ref for ref in ledger_context["input_refs"] if ref.endswith("/change.md"))
                payload = {"summary": "The original boundedness gap remains.", "delta": {
                    "schema_version": "research_delta/v1", "branch_id": "b-target",
                    "obligation_ids": ["target"], "change_ref": note, "applications": [],
                    "attempt": {"outcome": "METHOD_OBSTRUCTION", "obstacle_ref": note,
                                "reopen_condition_ref": note},
                }}
            else:
                return super().complete_structured_result(
                    prompt, workdir=workdir, call_name=call_name, ledger_context=ledger_context, **kwargs
                )
            return self._reply(
                json.dumps(payload), workdir=root, call_name=call_name,
                ledger_context=ledger_context, structured=True,
            )

    transport = ContinuationTransport("rci")
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Investigate convergence of the bounded sequence.\n")
    write = assignment._write_text_atomic

    def interrupt_handoff(path, text):
        if path.parent.name == "round2":
            raise KeyboardInterrupt
        write(path, text)

    with monkeypatch.context() as scoped:
        scoped.setattr(assignment, "_write_text_atomic", interrupt_handoff)
        assert cli.main([
            "run", "--problem", str(problem), "--output", str(tmp_path),
            "--problem-id", "rci", "--max-rounds", "2", "--resource-variant",
            "standard", "--json",
        ]) == 130
    root = tmp_path / "rci"
    calls = list(transport.calls)
    assert sum("conjecture_explorer" in name for name in calls) == 1
    assert sum("mechanism_explorer" in name for name in calls) == 1
    assert calls.count("global_research_director_0002_receipt_correction") == 1
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == [*calls, "research_reporter_round2"]
    question = work_ref(root, "round1/assignment/task.json")
    assert [a["research_problem_ref"] for a in load_index(root)["attempts"]] == [question, question]
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == [*calls, "research_reporter_round2"]


def test_cli_resumes_interrupted_review_after_committed_mechanism(
    tmp_path, monkeypatch
):
    transport = RciTransport(["HANDOFF"], mechanism_count=1, interrupt_review=True)
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Investigate convergence of the bounded sequence.\n")
    args = [
        "run",
        "--problem",
        str(problem),
        "--output",
        str(tmp_path),
        "--problem-id",
        "resume-rci",
        "--max-rounds",
        "1",
        "--json",
    ]
    assert cli.main(args) == 130
    root = tmp_path / "resume-rci"
    mechanism = root / transport.mechanisms[0]
    original = mechanism.read_bytes()
    checkpoint = json.loads((mechanism.parent / "completed_phase.json").read_text())
    assert set(checkpoint) == {"outputs"}
    assert str((mechanism.parent / "output.json").relative_to(root)) in checkpoint["outputs"]

    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert [role for role, _, _ in transport.records] == [
        "rci_mechanism_explorer",
        "rci_mechanism_critic",
        "rci_mechanism_critic",
        "rci_conjecture_explorer",
    ]
    assert transport.records[1][1] == transport.records[2][1]
    assert mechanism.read_bytes() == original
    traces = list((root / "work/turn001").rglob("interface_result.json"))
    successful = [t for path in traces if (t := json.loads(path.read_text()))["host"]["semantic_status"] == "PASS"]
    assert successful and all("payload" not in t and (root / t["host"]["response_ref"]).is_file() for t in successful)


def test_call_budget_preserves_original_mechanism_and_review(tmp_path, monkeypatch):
    transport = RciTransport([], mechanism_count=1)
    root, outcome = run_rci(tmp_path, monkeypatch, transport, "--max-calls", "5")
    assert [role for role, _, _ in transport.records] == [
        "rci_mechanism_explorer",
        "rci_mechanism_critic",
    ]
    assert outcome["reason"] == "TOTAL_CALL_BUDGET_EXHAUSTED"
    assert transport.mechanisms[0] in outcome["published_refs"]
    assert (root / outcome["evidence_refs"][-1]).read_text() == DIAGNOSIS


def test_resolved_question_has_one_original_for_independent_review(
    tmp_path, monkeypatch
):
    transport = RciTransport([], mechanism_count=0, resolve=True)
    root, outcome = run_rci(tmp_path, monkeypatch, transport)
    assert outcome["reason"] == "candidate_resolution"
    (ref,) = outcome["manuscript_refs"]
    assert ref.endswith("/closure_brief.md")
    assert outcome["manuscript_dependencies"] == {ref: [outcome["candidate_ref"]]}
    round_dir = root / work_ref(root, "round1")
    request = json.loads((round_dir / "reporter_request.json").read_text())
    assert request["manuscript_refs"] == [ref]
    assert ref in request["required_refs"]
    assert [role for role, _, _ in transport.records] == ["rci_mechanism_explorer"]


def test_local_repair_replays_committed_critic_and_escalates_recurring_gap(tmp_path, monkeypatch):
    from pace.agents.rci.phase_runner import BudgetedPhaseRunner

    transport = RciTransport(["HANDOFF"], mechanism_count=2, local_gaps=["tail-bound", "tail-bound"])
    run_phase = BudgetedPhaseRunner.run
    def interrupt(self, **kw):
        if kw["phase"] == "MECHANISM_EXPLORER" and kw["input_refs"]["BASE_MECHANISM"]:
            raise KeyboardInterrupt
        return run_phase(self, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(BudgetedPhaseRunner, "run", interrupt)
        patch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
        question = tmp_path / "problem.md"
        question.write_text("Investigate convergence of the bounded sequence.\n")
        assert cli.main(["run", "--problem", str(question), "--output", str(tmp_path),
                         "--problem-id", "rci", "--resource-variant", "standard",
                         "--max-rounds", "1", "--json"]) == 130
    root = tmp_path / "rci"
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    names = [name for name, _, _ in transport.records]
    assert names == ["rci_mechanism_explorer", "rci_mechanism_critic",
                     "rci_mechanism_explorer", "rci_mechanism_critic", "rci_conjecture_explorer"]
    _, review_dir, _ = transport.records[1]
    _, _, inputs = transport.records[2]
    assert inputs["BASE_MECHANISM"] == transport.mechanisms[:1]
    assert (review_dir / "review.md").relative_to(root).as_posix() in inputs["EVIDENCE"]
    assert (review_dir / "review.md").read_text() == DIAGNOSIS


def test_weak_rci_uses_the_original_critic_receipt(tmp_path, monkeypatch):
    transport = RciTransport(["HANDOFF"], mechanism_count=1)
    run_rci(tmp_path, monkeypatch, transport, "--resource-variant", "weak")
    roles = [name for name, _, _ in transport.records]
    assert roles == ["rci_mechanism_explorer", "rci_mechanism_critic", "rci_conjecture_explorer"]
    assert "action" not in json.loads((transport.records[1][1] / "output.json").read_text())
