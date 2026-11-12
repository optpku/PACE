"""New writing commands execute the public pipeline with committed source inputs."""

import json
import re
from pathlib import Path

import pytest

from pace import cli
from pace.pipeline import ProofPipeline
from pace.reporting import research_report, theory_paper
from pace.reporting.writing import active_writing_request, writing_artifacts
from pace.runtime.llm import TransportSuccess
from pace.runtime.output.calls import attempt_directory
from pace.runtime.output.events import read_events
from pace.runtime.output.workspace import RunWorkspace
from pace.runtime.prompts import materialize_run_resources


class WritingTransport:
    def __init__(self):
        self.calls = []

    def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context):
        root = Path(workdir)
        self.calls.append((call_name, prompt, ledger_context))
        output = root / re.search(r"## Deliverable\nWrite `([^`]+)`", prompt)[1]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            r"\documentclass{article}\begin{document}Existing theorem.\end{document}"
            if call_name == "theory_paper_writer"
            else "# Writing result\n\nExisting theorem and its precise gap.\n"
        )
        folder = attempt_directory(root, ledger_context["call_ref"], 1)
        (folder / "response.txt").write_text("Submitted the requested original file.")
        return TransportSuccess(
            text="Submitted the requested original file.",
            invocation_id=f"writing-{len(self.calls)}",
            call_name=call_name,
            exit_code=0,
            terminal_event_type="turn.completed",
            accepted_output_source="file",
            output_schema_valid=None,
            attempt_count=1,
            artifact_dir=folder,
        )


@pytest.fixture
def writing_run(tmp_path, monkeypatch):
    ws = RunWorkspace.create(tmp_path, "writing")
    ws.write_text(ws.work_ref("input/problem.md"), "Original literal mathematical problem.")
    ws.write_text(ws.work_ref("input/request.md"), "Original author request.")
    ws.write_json(
        ws.work_ref("state/research_execution_policy.json"),
        {
            "mode": "proof",
            "resource_variant": "standard",
            "max_calls": None,
            "max_seconds": None,
        },
    )
    materialize_run_resources(ws.root, resource_variant="standard")
    transport = WritingTransport()
    monkeypatch.setattr(ProofPipeline, "_llm", lambda self: transport)
    monkeypatch.setattr(ProofPipeline, "_director_llm", lambda self: transport)
    return ws, transport


def command(ws, action, *args):
    return cli.main([action, "--run-dir", str(ws.root), "--json", *map(str, args)])


def test_report_uses_frozen_outline_and_retains_full_request(writing_run, tmp_path, capsys):
    ws, transport = writing_run
    message = tmp_path / "request.txt"
    message.write_text("Preserve this complete author request, including its exact scope.")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Keep the technical appendix.")
    assert command(ws, "outline", "--message", message, "--requirements", requirements) == 0
    assert command(ws, "report") == 0
    state = active_writing_request(ws.root)["request"]
    frozen = (ws.root / state["outline_ref"]).read_text()
    (ws.root / "results/outline.md").write_text("Later unrelated outline.")
    assert (ws.root / state["outline_ref"]).read_text() == frozen
    complete_request = (ws.root / state["request_ref"]).read_text()
    assert message.read_text() in complete_request
    assert requirements.read_text() in complete_request
    before = len(transport.calls)
    assert command(ws, "resume") == 0
    assert len(transport.calls) == before
    assert set(writing_artifacts(ws.root)) >= {"research_outline", "research_report"}
    assert [name for name, _, _ in transport.calls] == [
        "research_report_outline",
        "research_report_writer",
    ]
    assert all("user_update" not in event for event in read_events(ws.root))


def test_paper_chain_has_no_report_dependency_and_retries_only_pdf(
    writing_run, tmp_path, monkeypatch
):
    ws, transport = writing_run
    request = tmp_path / "paper.txt"
    request.write_text("Write a paper about the existing checked theorem.")
    builds = []
    terminal = ws.root / ws.work_ref("final_report.json")
    terminal.write_text('{"result":"PASS","mathematical_status":"VERIFIED"}\n')
    original_verdict = terminal.read_bytes()

    def build(directory):
        builds.append(directory)
        payload = {
            "status": "FAILED" if len(builds) == 1 else "COMPLETE",
            "reason": "font tool failure" if len(builds) == 1 else "",
        }
        (directory / "build.json").write_text(json.dumps(payload))
        if len(builds) > 1:
            (directory / "main.pdf").write_bytes(b"%PDF-1.7\n")
        return payload

    monkeypatch.setattr(theory_paper, "build_paper", build)
    assert command(ws, "paper-outline", "--message", request) == 0
    assert command(ws, "paper") == 0
    assert theory_paper.paper_build_status(ws.root) == "FAILED"
    assert "paper_source" in writing_artifacts(ws.root)
    assert "paper_pdf" not in writing_artifacts(ws.root)
    before = len(transport.calls)
    assert command(ws, "resume") == 0
    assert len(transport.calls) == before
    assert len(builds) == 2
    assert theory_paper.paper_build_status(ws.root) == "COMPLETE"
    assert "paper_pdf" in writing_artifacts(ws.root)
    assert not (ws.root / "results/research_report.md").exists()
    assert terminal.read_bytes() == original_verdict
    assert [name for name, _, _ in transport.calls] == [
        "theory_paper_outline", "theory_paper_writer",
    ]


def test_writing_checkpoint_precedes_success_publication(writing_run, tmp_path, monkeypatch):
    ws, transport = writing_run
    request = tmp_path / "request.txt"
    request.write_text("Use original evidence.")
    outliner = research_report.ResearchReportOutliner(
        llm=transport, workdir=ws.root
    )
    outliner.request(request.read_text(), resource_variant="standard")
    original = research_report.commit_artifact_call_success
    checked = []

    def inspect(handle, **kwargs):
        state = json.loads((ws.root / research_report.outline_state_ref(ws.root)).read_text())
        assert state["outline_ref"] == kwargs["artifact_ref"]
        assert (ws.root / state["outline_ref"]).is_file()
        checked.append(state["outline_ref"])
        return original(handle, **kwargs)

    monkeypatch.setattr(research_report, "commit_artifact_call_success", inspect)
    assert outliner.update()
    assert len(checked) == 1


class ProofTransport(WritingTransport):
    def __init__(self, status, publication=None):
        super().__init__()
        self.status = status
        self.publication = publication or {"action": "CREATE"}

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        from pace.runtime.output.workspace import work_ref

        root = Path(workdir)
        if call_name == "problem_orientation_extract":
            payload = {
                "target_clauses": [{"start_line": 1, "end_line": 1}],
                "recommended_mode": "proof",
                "mode_reason": "An exact elementary equality.",
            }
        elif call_name == "problem_research_decision":
            reference = work_ref(root, "input/problem_brief.md")
            (root / reference).write_text(
                "# Integer addition\n\nProve the literal arithmetic equality.\n"
            )
            payload = {"brief_ref": reference}
        elif "proof_contract_auditor" in call_name:
            payload = {
                "contract_status": "PASS",
                "answer_mode": "POSITIVE_PROOF",
                "canonical_blockers": [],
            }
        elif call_name.startswith("math_verifier_kernel_"):
            payload = {
                "stage_status": self.status,
                "publication_eligible": self.status == "PASS",
                "publication": self.publication,
                "relation_checks": [
                    {
                        "relation_id": "R1",
                        "status": "PASS",
                        "statement": "2+2=4",
                        "conditions": [],
                        "dependency_relation_ids": [],
                        "reuse_authorized": True,
                        "allowed_success_contract_components": ["literal equality"],
                        "justification": "The integer addition calculation yields four.",
                    }
                ],
                "target_clause_checks": [
                    {
                        "status": "PASS" if self.status == "PASS" else "FAIL",
                        "evidence": "The literal equality is derived."
                        if self.status == "PASS"
                        else "The target remains open.",
                    }
                ],
                "final_assembly": {"derivation": "The checked equality proves the literal target."}
                if self.status == "PASS"
                else {"gap": "The full target remains open."},
                "canonical_blockers": []
                if self.status == "PASS"
                else [{"relation_id": "OPEN", "reason": "The target remains open."}],
            }
        elif call_name.startswith("finding_verifier_"):
            payload = {
                "stage_status": "PASS",
                "reuse_authorized": True,
                "evidence": "Checked the full arithmetic proof.",
                "publication": {"action": "NONE"},
            }
        else:
            raise AssertionError(call_name)
        self.calls.append((call_name, prompt, ledger_context))
        folder = attempt_directory(root, ledger_context["call_ref"], 1)
        response = json.dumps(payload)
        (folder / "response.txt").write_text(response)
        return TransportSuccess(
            text=response,
            invocation_id=f"proof-{len(self.calls)}",
            call_name=call_name,
            exit_code=0,
            terminal_event_type="turn.completed",
            accepted_output_source="file",
            output_schema_valid=True,
            attempt_count=1,
            artifact_dir=folder,
        )

    def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context):
        assert call_name.startswith(("director_led_survey_", "director_led_prover_")), call_name
        result = super().complete_unstructured_result(
            prompt, workdir=workdir, call_name=call_name, ledger_context=ledger_context
        )
        output = Path(workdir) / re.search(r"## Deliverable\nWrite `([^`]+)`", prompt)[1]
        output.write_text("By the definition of integer addition, 2 + 2 = 4.\n")
        if call_name.startswith("director_led_survey_"):
            output.with_name("source_manifest.json").write_text('{"sources": []}')
        return result


@pytest.mark.parametrize("status, expected_loops", [("PASS", 1), ("MATH_FAIL", 5)])
def test_public_cli_selects_proof_mode_and_preserves_five_loop_budget(
    tmp_path, monkeypatch, capsys, status, expected_loops
):
    transport = ProofTransport(status)
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **settings: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2 + 2 = 4.\n")
    assert (
        cli.main(
            [
                "run",
                "--problem",
                str(problem),
                "--problem-id",
                "proof",
                "--output",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    root = tmp_path / "proof"
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert report["execution_mode"] == "proof"
    assert report["mode_source"] == "ORIENTATION"
    assert report["mode_reason"] == "An exact elementary equality."
    assert report["max_proof_loops"] == 5
    assert report["proof_loops_used"] == expected_loops
    assert "report" not in report["artifact_refs"]
    assert not (root / "work/turn001/outcome.md").exists()
    assert (root / "work/turn001/round0001/proof.md").is_file()
    assert (root / f"work/turn001/round{expected_loops:04d}/proof.md").is_file()
    assert not (root / "work/turn001/stages").exists()
    assert sum(name.startswith("director_led_survey_") for name, _, _ in transport.calls) == 1
    assert (
        sum(name.startswith("director_led_prover_") for name, _, _ in transport.calls)
        == expected_loops
    )
    assert sum("proof_contract_auditor" in name for name, _, _ in transport.calls) == expected_loops
    assert sum(name.startswith("math_verifier_kernel_") for name, _, _ in transport.calls) == expected_loops
    assert len(transport.calls) == 3 + 3 * expected_loops
    for _, prompt, context in transport.calls:
        assert "work/turn001/stages/" not in prompt
        record = json.loads((root / context["call_ref"]).read_text())
        for ref in (*record["input_refs"], record["prompt_ref"], record["response_ref"], record["deliverable_ref"]):
            if ref:
                assert (root / ref).is_file(), ref
    assert not (root / "results/current_report.md").exists()
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[-1]["event"] == "run_completed"
    assert output[-1]["proof_loops_used"] == expected_loops
    assert output[-1]["mode_source"] == "ORIENTATION"
    before = list(transport.calls), (root / "work/turn001/final_report.json").read_bytes()
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert (transport.calls, (root / "work/turn001/final_report.json").read_bytes()) == before
