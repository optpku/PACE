"""Stable findings keep checked versions without publishing every successful draft."""

import json
import re
from pathlib import Path

import pytest

from pace import cli
from pace.orchestrator.results import publish_terminal_report
from pace.agents.director.findings import check_stage_findings
from pace.orchestrator.results import publish_stage_findings
from pace.runtime.llm import TransportSuccess
from pace.artifacts.calls import attempt_directory
from pace.artifacts.results import finding_entries
from pace.orchestrator.results import publish_finding
from pace.artifacts.workspace import RunWorkspace
from pace.artifacts.results import write_dependency_manifest
from pace.runtime.semantic import StructuredSemanticResult


def test_updates_keep_identity_and_exact_dependency_versions(tmp_path):
    ws = RunWorkspace.create(tmp_path, "findings")
    originals = []
    for index, text in enumerate(("# Bound\n\nThe bound is 4.\n", "# Bound\n\nThe improved bound is 3.\n"), 1):
        source = ws.work_ref(f"round{index}/findings/bound.md")
        receipt = source.replace(".md", ".verification.json")
        ws.write_text(source, text)
        ws.write_json(receipt, {"verdict": {"stage_status": "PASS", "reuse_authorized": True}})
        originals.append((source, receipt, text))
    first, second = originals
    reference = publish_finding(ws.root, source_ref=first[0], verification_ref=first[1],
                                round_index=1, publication={"action": "CREATE"})
    manifest, _ = write_dependency_manifest(workdir=ws.root, output_ref=ws.work_ref("round1/proof.md"),
                                           target_contract_id="TARGET")
    old_manifest = (ws.root / manifest).read_bytes()
    navigation = "# Results by mathematical topic\n\nA precise result and its remaining gap.\n"
    ws.write_text("results/findings/README.md", navigation)
    assert publish_finding(ws.root, source_ref=second[0], verification_ref=second[1], round_index=2,
                           publication={"action": "UPDATE", "finding_ref": reference}) == reference
    current = ws.path(reference).read_text()
    assert second[2] in current and second[1] in current
    assert len(finding_entries(ws.root)) == 1
    assert finding_entries(ws.root)[0]["source_ref"] == second[0]
    assert (ws.root / manifest).read_bytes() == old_manifest
    assert json.loads(old_manifest)["entries"][0]["source_text"] == first[2]
    for source, receipt, text in originals:
        assert ws.path(source).read_text() == text
        assert source in ws.path("results/findings/archive.md").read_text()
        assert receipt in ws.path("results/findings/archive.md").read_text()
    assert ws.path("results/findings/README.md").read_text() == navigation
    # The pipeline revisits completed stages on continuation and resume.
    publish_finding(ws.root, source_ref=first[0], verification_ref=first[1], round_index=1,
                    publication={"action": "CREATE"})
    assert ws.path(reference).read_text() == current
    assert len(json.loads(ws.path("work/state/findings.json").read_text())["publications"]) == 2


def test_stage_verifier_controls_create_update_and_supporting_records(tmp_path, monkeypatch):
    ws = RunWorkspace.create(tmp_path, "stages")
    ws.write_text(ws.work_ref("input/problem.md"), "Find the sharp bound.")
    ws.write_json(ws.work_ref("input/target_contract.json"), {"contract_id": "TARGET"})
    actions = [{"action": "CREATE"}, {"action": "UPDATE", "finding_ref": "results/findings/R0001.md"},
               {"action": "NONE", "reason": "A supporting computation, not another result."}]
    checked = []

    def verify(**kwargs):
        checked.append(kwargs)
        return StructuredSemanticResult(payload={"stage_status": "PASS", "reuse_authorized": True,
                                                 "publication": actions[len(checked) - 1]}, host={})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    completed = []
    for index in range(1, 4):
        directory = ws.work_ref(f"round{index}")
        source = f"{directory}/findings/bound.md"
        ws.write_text(source, f"# Bound\n\nChecked statement {index}.\n")
        ws.write_json(f"{directory}/agent_result.json",
                      {"agent_id": "research", "status": "SUCCEEDED", "deliverable_ref": source})
        ws.write_json(f"{directory}/assignment/task.json", {"round_index": index})
        completed.append(f"{directory}/agent_result.json")
        assert check_stage_findings(llm=object(), workdir=ws.root, completed_result_refs=completed)
        publish_stage_findings(workdir=ws.root, completed_result_refs=completed)
    assert len(checked) == 3
    assert len(finding_entries(ws.root)) == 1
    assert "Checked statement 2." in ws.path("results/findings/R0001.md").read_text()
    assert "round3/findings/bound.md" in ws.path("results/findings/archive.md").read_text()
    assert "results/findings/catalog.md" in checked[-1]["input_refs"]
    assert "results/findings/archive.md" in checked[-1]["input_refs"]


@pytest.mark.parametrize("action", ["CREATE", "UPDATE", "NONE"])
def test_research_finalization_obeys_the_kernel_publication_action(tmp_path, action):
    ws = RunWorkspace.create(tmp_path, "research")
    original = ws.work_ref("round1/finding.md")
    original_check = ws.work_ref("round1/finding.verification.json")
    ws.write_text(original, "# Bound\n\nThe bound is at most four.")
    ws.write_json(original_check, {"stage_status": "PASS"})
    if action == "UPDATE":
        publish_finding(ws.root, source_ref=original, verification_ref=original_check,
                        round_index=1, publication={"action": "CREATE"})
    proof, receipt, kernel = (ws.work_ref(f"round2/{name}") for name in
                              ("candidate.md", "verification.json", "kernel.json"))
    ws.write_text(proof, "# Bound\n\nThe bound is at most three, hence also at most four.")
    ws.write_json(receipt, {"proof_ref": proof, "kernel_ref": kernel})
    ws.write_json(kernel, {"stage_status": "PASS", "publication_eligible": True,
                          "publication": {"action": action, "finding_ref": "results/findings/R0001.md"}})
    report = {"schema_version": "final_report/v8", "result": "PASS", "run_status": "completed",
              "mathematical_status": "VERIFIED", "director_state": {"round_index": 2},
              "artifact_refs": {"proof": proof, "verification": receipt, "kernel": kernel}}
    publish_terminal_report(ws, report)
    assert report["publication_status"] == ("NO_PUBLICATION" if action == "NONE" else "PUBLISHED")
    assert report["result"] == "PASS"
    assert "report" not in report["artifact_refs"]
    assert not (ws.root / "work/turn001/outcome.md").exists()
    assert len(report["artifact_refs"]["findings"]) == (0 if action == "NONE" else 1)
    if action == "UPDATE":
        assert finding_entries(ws.root)[0]["source_ref"] == proof
        assert ws.path(original).read_text().endswith("four.")


class ProofPublicationTransport:
    """Exercise the actual CLI, proof workflow, verifier and artifact publishers."""

    def __init__(self, action):
        self.publication = {"action": action}
        self.calls = []

    def _reply(self, body, *, workdir, call_name, ledger_context, structured):
        self.calls.append(call_name)
        folder = attempt_directory(Path(workdir), ledger_context["call_ref"], 1)
        (folder / "response.txt").write_text(body)
        return TransportSuccess(text=body, invocation_id=f"finding-test-{len(self.calls)}",
                                call_name=call_name, exit_code=0, terminal_event_type="turn.completed",
                                accepted_output_source="file", output_schema_valid=structured,
                                attempt_count=1, artifact_dir=folder)

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        from pace.artifacts.workspace import work_ref

        root = Path(workdir)
        if call_name.startswith('research_reporter_'):
            request_ref = next(r for r in ledger_context['input_refs'] if r.endswith('/reporter_request.json'))
            request = json.loads((root / request_ref).read_text())
            original = request['original_refs'][0]
            result = {'summary': 'The original argument is submitted for independent verification.',
                'delta': {'schema_version': 'research_delta/v1', 'obligation_ids': request['obligation_ids'],
                    'branch_id': request['branch_id'], 'change_ref': original, 'applications': [],
                    'attempt': {'outcome': 'LOCAL_RESULT', 'obstacle_ref': '', 'reopen_condition_ref': ''}}}
        elif call_name == "problem_orientation_extract":
            result = {"target_clauses": [{"start_line": 1, "end_line": 1}],
                      "recommended_mode": "proof", "mode_reason": "An exact equality."}
        elif call_name == "problem_research_decision":
            ref = work_ref(root, "input/problem_brief.md")
            (root / ref).write_text("# Integer addition\n\nProve the literal equality.")
            result = {"brief_ref": ref}
        elif "proof_contract_auditor" in call_name:
            result = {"contract_status": "PASS", "answer_mode": "POSITIVE_PROOF"}
        elif call_name.startswith("math_verifier_kernel_"):
            result = {"stage_status": "PASS", "publication_eligible": True,
                      "publication": self.publication,
                      "relation_checks": [{"status": "PASS", "statement": "2+2=4",
                                           "justification": "Integer addition gives four."}],
                      "target_clause_checks": [{"status": "PASS", "evidence": "The literal equality is derived."}],
                      "final_assembly": {"derivation": "The checked equality is the target."}}
        else:
            raise AssertionError(call_name)
        return self._reply(json.dumps(result), workdir=root, call_name=call_name,
                           ledger_context=ledger_context, structured=True)

    def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context):
        output = Path(workdir) / re.search(r"## Deliverable\nWrite `([^`]+)`", prompt)[1]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("By integer addition, 2+2=4. Consequently, 4=2+2.\n")
        from test_research_round_limit import write_delta
        write_delta(Path(workdir), output)
        if call_name.startswith("director_led_survey_"):
            output.with_name("source_manifest.json").write_text('{"sources": []}')
        else:
            assert call_name.startswith("director_led_prover_")
        return self._reply("Submitted the complete proof.", workdir=workdir, call_name=call_name,
                           ledger_context=ledger_context, structured=False)


@pytest.mark.parametrize("action", ["CREATE", "NONE"])
def test_public_cli_keeps_publication_separate_from_pass_and_updates_on_continue(tmp_path, monkeypatch, capsys, action):
    transport = ProofPublicationTransport(action)
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **settings: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "proof",
                     "--output", str(tmp_path), "--json"]) == 0
    root = tmp_path / "proof"
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert report["publication_status"] == ("NO_PUBLICATION" if action == "NONE" else "PUBLISHED")
    assert report["result"] == "PASS"
    assert len(report["artifact_refs"]["findings"]) == (1 if action == "CREATE" else 0)
    if action == "NONE":
        assert "round1/proof.md" in (root / "results/findings/archive.md").read_text()
        return
    first = (root / "work/turn001/round1/proof.md").read_bytes()
    transport.publication = {"action": "UPDATE", "finding_ref": "results/findings/R0001.md"}
    request = tmp_path / "follow-up.md"
    request.write_text("Complete the proof of the same result, including the reverse equality.")
    assert cli.main(["continue", "--run-dir", str(root), "--message", str(request), "--json"]) == 0
    state = json.loads((root / "work/state/findings.json").read_text())
    assert len(state["entries"]) == 1 and len(state["publications"]) == 2
    assert state["entries"][0]["source_ref"].startswith("work/turn002/")
    assert (root / "work/turn001/round1/proof.md").read_bytes() == first


@pytest.mark.parametrize("wrong", ["draft_ref", "source_binding", "target_contract_id"])
def test_standalone_finding_resume_and_publication_check_same_identity(
    tmp_path, monkeypatch, wrong
):
    from pace.artifacts.workspace import RunWorkspace
    from pace.contracts.research.memory import binding

    ws = RunWorkspace.create(tmp_path, "stages")
    folder = ws.work_ref("round1")
    source = f"{folder}/findings/sample.md"
    ref = f"{folder}/agent_result.json"
    ws.write_json(ws.work_ref("input/target_contract.json"), {"contract_id": "TARGET"})
    ws.write_text(source, "A standalone lemma.")
    ws.write_json(ref, {"status": "SUCCEEDED", "deliverable_ref": source})
    ws.write_json(f"{folder}/assignment/task.json", {"round_index": 1})
    count = []

    def interrupt(**kw):
        count.append(1)
        kw["on_accept"](
            {
                "stage_status": "PASS",
                "reuse_authorized": True,
                "publication": {"action": "CREATE"},
            }
        )
        raise KeyboardInterrupt()

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", interrupt)
    with pytest.raises(KeyboardInterrupt):
        check_stage_findings(llm=None, workdir=ws.root, completed_result_refs=[ref])
    assert (
        check_stage_findings(llm=None, workdir=ws.root, completed_result_refs=[ref])
        and len(count) == 1
    )
    receipt = ws.path(source).with_suffix(".verification.json")
    record = json.loads(receipt.read_text())
    record[wrong] = binding(ws.root, ref) if wrong == "source_binding" else "unrelated"
    receipt.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="another manuscript|different manuscript"):
        publish_stage_findings(workdir=ws.root, completed_result_refs=[ref])
    assert not finding_entries(ws.root)


@pytest.mark.parametrize("mode", ["proof", "research"])
@pytest.mark.parametrize("role", ["proof_contract_auditor", "math_verifier_kernel"])
def test_accepted_verifier_resumes_without_repeating_inference(
    tmp_path, monkeypatch, mode, role
):
    from pace.runtime import semantic
    from test_research_round_limit import RoundLimitTransport

    transport = (
        ProofPublicationTransport("CREATE")
        if mode == "proof"
        else RoundLimitTransport("prover")
    )
    monkeypatch.setattr(
        "pace.agents.director.harness.build_llm", lambda **kw: transport
    )
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **kw: transport)
    problem = tmp_path / "p.md"
    problem.write_text("Prove 2+2=4.")
    root = tmp_path / "result"
    finish = semantic.finish_call

    def interrupt(workdir, record_ref, **kw):
        finish(workdir, record_ref, **kw)
        if json.loads((Path(workdir) / record_ref).read_text())["role"] == role:
            raise KeyboardInterrupt()

    with monkeypatch.context() as scoped:
        scoped.setattr(semantic, "finish_call", interrupt)
        assert (
            cli.main(
                [
                    "run",
                    "--problem",
                    str(problem),
                    "--output",
                    str(tmp_path),
                    "--problem-id",
                    root.name,
                    "--mode",
                    mode,
                    "--json",
                ]
            )
            == 130
        )
    from pace.runtime.agent_environment import role_home

    roles = ("RESEARCH_REPORTER", "PROOF_CONTRACT_AUDITOR", "MATH_VERIFIER_KERNEL")
    instructions = {role_home(root, name) / "AGENTS.md" for name in roles}
    expected = {path: path.read_bytes() for path in instructions}
    for path in instructions:
        path.unlink()
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert all(path.read_bytes() == content for path, content in expected.items())
    assert sum(role in name for name in transport.calls) == 1
    assert (
        json.loads((root / "work/turn001/final_report.json").read_text())["result"]
        == "PASS"
    )


def test_terminal_rejects_swapped_kernel_and_changed_auditor(tmp_path, monkeypatch):
    from pace.artifacts.workspace import RunWorkspace

    transport = ProofPublicationTransport("CREATE")
    monkeypatch.setattr(
        "pace.agents.director.harness.build_llm", lambda **kw: transport
    )
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **kw: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.")
    root = tmp_path / "example"
    assert (
        cli.main(
            [
                "run",
                "--problem",
                str(problem),
                "--output",
                str(tmp_path),
                "--problem-id",
                root.name,
                "--mode",
                "proof",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    ws = RunWorkspace(root=root, problem_id=root.name)
    refs = report["artifact_refs"]
    alternate = Path(refs["kernel"]).with_name("other-kernel.json").as_posix()
    ws.write_json(alternate, json.loads(ws.path(refs["kernel"]).read_text()))
    swapped = {**report, "artifact_refs": {**refs, "kernel": alternate}}
    assert publish_terminal_report(ws, swapped)["publication_status"] == "INCOMPLETE"
    auditor = json.loads(ws.path(refs["auditor"]).read_text())
    ws.write_json(
        refs["auditor"], {**auditor, "observation": "A different audit version."}
    )
    assert publish_terminal_report(ws, report)["publication_status"] == "INCOMPLETE"
