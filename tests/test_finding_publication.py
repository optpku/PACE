"""Stable findings keep checked versions without publishing every successful draft."""

import hashlib
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
                                           target_contract_id="TARGET", selected_dependencies=[reference])
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
    assert json.loads(old_manifest)["entries"][0]["source_sha256"] == hashlib.sha256(first[2].encode()).hexdigest()
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
        ws.write_json(f"{directory}/reporter_result.json", {"result_ref": f"{directory}/agent_result.json", "payload": {"finding_refs": [source]}})
        completed.append(f"{directory}/agent_result.json")
        assert check_stage_findings(llm=object(), workdir=ws.root, completed_result_refs=completed)
        publish_stage_findings(workdir=ws.root, completed_result_refs=completed)
    assert len(checked) == 3
    assert len(finding_entries(ws.root)) == 1
    assert "Checked statement 2." in ws.path("results/findings/R0001.md").read_text()
    assert "round3/findings/bound.md" in ws.path("results/findings/archive.md").read_text()
    assert checked[-1]["draft_ref"] in checked[-1]["input_refs"]
    assert "results/findings/archive.md" not in checked[-1]["input_refs"]


def test_unsubmitted_files_do_not_schedule_reviews_or_enter_the_catalogue(tmp_path):
    from pace.artifacts.results import committed_finding_refs

    ws = RunWorkspace.create(tmp_path, "nomination")
    folder = ws.work_ref("round1")
    ref = f"{folder}/agent_result.json"
    draft = f"{folder}/findings/unused.md"
    ws.write_text(draft, "An unfinished attempt.")
    for role in ("research", "prover", "rci", "planner"):
        ws.write_json(ref, {"agent_id": role, "status": "SUCCEEDED", "deliverable_ref": draft})
        assert committed_finding_refs(ws.root, ref) == ()
    ws.write_json(ref, {"agent_id": "research", "status": "SUCCEEDED", "deliverable_ref": draft})
    ws.write_json(f"{folder}/reporter_result.json", {"result_ref": ref, "payload": {"finding_refs": []}})
    assert committed_finding_refs(ws.root, ref) == ()
    ws.write_json(f"{folder}/reporter_result.json", {"result_ref": "another/result.json", "payload": {"finding_refs": [draft]}})
    with pytest.raises(ValueError, match="different handoff"):
        committed_finding_refs(ws.root, ref)


def test_terminal_publication_requires_committed_verification_binding(tmp_path):
    ws = RunWorkspace.create(tmp_path, "research")
    proof = ws.work_ref("round2/candidate.md")
    receipt = ws.work_ref("round2/verification.json")
    kernel = ws.work_ref("round2/kernel.json")
    ws.write_text(proof, "# Bound\n\nThe bound is at most three.")
    ws.write_json(receipt, {"proof_ref": proof, "kernel_ref": kernel})
    ws.write_json(kernel, {
        "stage_status": "PASS", "publication_eligible": True,
        "publication": {"action": "CREATE"},
    })
    report = {
        "schema_version": "final_report/v8", "result": "PASS",
        "run_status": "completed", "mathematical_status": "VERIFIED",
        "artifact_refs": {"proof": proof, "verification": receipt, "kernel": kernel},
    }
    publish_terminal_report(ws, report)
    assert report["publication_status"] == "INCOMPLETE"
    assert report["run_status"] == "completed_with_operational_error"
    assert report["artifact_refs"]["findings"] == []
    assert not finding_entries(ws.root)


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
            request_ref = next(r for r in (*ledger_context['input_refs'], *ledger_context.get('archive_refs', ())) if r.endswith('/reporter_request.json'))
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
            result = {"stage_status": "PASS", "publication_eligible": True, "publication": self.publication}
            if "proof_location" in kwargs["output_schema"]["properties"]:
                result.update(summary="Integer addition yields the requested equality.", proof_location="First equation.")
            else:
                result.update(relation_checks=[{"status":"PASS","statement":"2+2=4"}],
                              target_clause_checks=[{"evidence":"Literal equality."}],
                              final_assembly={"derivation":"Addition gives the target."})
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


@pytest.mark.parametrize("completed_transport", [False, True])
def test_proof_resume_distinguishes_completed_author_from_partial_draft(
    tmp_path, monkeypatch, completed_transport
):
    from pace.agents.director import dispatch, harness

    transport = ProofPublicationTransport("NONE")
    monkeypatch.setattr(harness, "build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    root = tmp_path / "proof-resume"
    author = "director_led_prover_01"
    begin = dispatch.begin_artifact_call
    complete = transport.complete_unstructured_result

    def interrupt_after_completion(*args, **kwargs):
        handle = begin(*args, **kwargs)
        if kwargs["call_name"] == author:
            assert handle.transport_status == "SUCCESS"
            raise KeyboardInterrupt()
        return handle

    def interrupt_before_completion(prompt, **kwargs):
        reply = complete(prompt, **kwargs)
        if kwargs["call_name"] == author:
            raise KeyboardInterrupt()
        return reply

    with monkeypatch.context() as scoped:
        if completed_transport:
            scoped.setattr(dispatch, "begin_artifact_call", interrupt_after_completion)
        else:
            scoped.setattr(transport, "complete_unstructured_result", interrupt_before_completion)
        assert cli.main(["run", "--mode", "proof", "--problem", str(problem),
                         "--problem-id", root.name, "--output", str(tmp_path), "--json"]) == 130

    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls.count(author) == (1 if completed_transport else 2)
    turn = root / "work/turn001"
    assert bool(list(turn.rglob("partial-*.md"))) != completed_transport
    state = json.loads((turn / "state/proof_state.json").read_text())
    assert not any("interrupted_attempts/" in ref for ref in state["completed_refs"])
    assert json.loads((turn / "final_report.json").read_text())["result"] == "PASS"


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


@pytest.mark.parametrize("mode,cap", [("proof", 4), ("research", 3)])
def test_soft_budget_does_not_discard_an_admitted_round(tmp_path, monkeypatch, mode, cap):
    from test_research_round_limit import RoundLimitTransport

    transport = ProofPublicationTransport("CREATE") if mode == "proof" else RoundLimitTransport("prover")
    transport.publication = {"action": "CREATE"}
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.")
    root = tmp_path / mode
    # The last admitted call has committed its result, but the parent checkpoint
    # has not yet advanced to Reporter (proof) or the assigned Prover (research).
    boundary = ("pace.agents.director.proof._report_progress" if mode == "proof"
                else "pace.agents.director.loop.snapshot_verified_progress")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as scoped:
        scoped.setattr(boundary, interrupt)
        assert cli.main(["run", "--mode", mode, "--problem", str(problem), "--problem-id", mode,
                         "--output", str(tmp_path), "--max-calls", str(cap), "--json"]) == 130
    assert len(transport.calls) == cap
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert (report["result"], report["publication_status"]) == ("PASS", "PUBLISHED")
    assert len(transport.calls) == len(set(transport.calls))
    calls = list(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == calls


@pytest.mark.parametrize("variant", ["standard", "weak"])
@pytest.mark.parametrize("mode", ["proof", "research"])
def test_terminal_publication_resumes_after_id_is_committed(tmp_path, monkeypatch, mode, variant):
    from pace.artifacts import results
    from test_research_round_limit import RoundLimitTransport

    transport = ProofPublicationTransport("CREATE") if mode == "proof" else RoundLimitTransport("prover")
    transport.publication = {"action": "CREATE"}
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.")
    root = tmp_path / mode
    journal = root / "work/state/findings.json"
    public = root / "results/findings/R0001.md"
    write = results._write_text_atomic

    def interrupt(path, content):
        if path == public:
            assert journal.is_file()
            raise KeyboardInterrupt
        write(path, content)

    with monkeypatch.context() as scoped:
        scoped.setattr(results, "_write_text_atomic", interrupt)
        assert cli.main(["run", "--mode", mode, "--resource-variant", variant, "--problem", str(problem),
                         "--problem-id", mode, "--output", str(tmp_path), "--json"]) == 130
    calls, ledger = list(transport.calls), journal.read_bytes()
    assert not public.exists()
    for _ in range(2):
        assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
        report = json.loads((root / "work/turn001/final_report.json").read_text())
        assert report["result"] == "PASS" and report["publication_status"] == "PUBLISHED"
        assert report["artifact_refs"]["findings"] == [public.relative_to(root).as_posix()]
        assert public.is_file() and report["artifact_refs"]["proof"] in public.read_text()
        assert journal.read_bytes() == ledger and len(json.loads(ledger)["publications"]) == 1
        assert transport.calls == calls
        assert not (root / "work/turn002").exists()


@pytest.mark.parametrize("wrong", ["draft_ref", "source_binding", "missing_binding", "target_contract_id"])
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
    ws.write_json(ref, {"agent_id": "research", "status": "SUCCEEDED", "deliverable_ref": source})
    ws.write_json(f"{folder}/reporter_result.json", {"result_ref": ref, "payload": {"finding_refs": [source]}})
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
    if wrong == "missing_binding":
        del record["source_binding"]
    else:
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


@pytest.mark.parametrize(
    ("variant", "hints", "expect_kernel"),
    [
        ("standard", [], False),
        ("standard", ["Whether a reduction covers the omitted case."], True),
        ("weak", [], True),
    ],
)
def test_scope_repair_only_defers_standard_kernel(
    tmp_path, monkeypatch, variant, hints, expect_kernel
):
    from test_research_reporter import NativeFixture

    transport = NativeFixture("proof")
    normal = transport._request

    def respond(messages, tools):
        name = transport.calls[-1]
        if (
            "proof_contract_auditor" in name
            and sum("proof_contract_auditor" in n for n in transport.calls) == 1
        ):
            return transport.tool(
                "submit_result",
                {
                    "contract_status": "FAIL",
                    "return_for_scope_repair": True,
                    "kernel_hints": hints,
                    "blocking_violations": [
                        {
                            "requirement": "Prove both cases.",
                            "candidate_evidence": "Only the first case is stated.",
                        }
                    ],
                },
            )
        return normal(messages, tools)

    transport._request = respond
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.")
    root = tmp_path / "cases"
    args = [
        "run",
        "--mode",
        "proof",
        "--resource-variant",
        variant,
        "--problem",
        str(problem),
        "--problem-id",
        root.name,
        "--output",
        str(tmp_path),
        "--json",
    ]
    if not expect_kernel:
        from pace.runtime import semantic

        finish = semantic.finish_call

        def interrupt(workdir, record_ref, **kwargs):
            finish(workdir, record_ref, **kwargs)
            if (
                json.loads((Path(workdir) / record_ref).read_text())["role"]
                == "proof_contract_auditor"
            ):
                raise KeyboardInterrupt()

        with monkeypatch.context() as scoped:
            scoped.setattr(semantic, "finish_call", interrupt)
            assert cli.main(args) == 130
        assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    else:
        assert cli.main(args) == 0
    assert sum("proof_contract_auditor" in name for name in transport.calls) == 2
    assert sum("math_verifier_kernel" in name for name in transport.calls) == 1 + int(
        expect_kernel
    )
    finals = sorted((root / "work/turn001").rglob("final_result.json"))
    first, repaired = [json.loads(f.read_text()) for f in finals]
    assert first["result"] == "FAILED" and first["failure_owner"] == "PROOF"
    assert first["verification_interface_failures"] == []
    assert first["kernel_status"] == ("PASS" if expect_kernel else "NOT_RUN")
    assert repaired["result"] == repaired["kernel_status"] == "PASS"
    calls = list(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == calls


@pytest.mark.parametrize("interrupt_after_patch", [False, True])
@pytest.mark.parametrize("defect", [
    "missing_publication", "unknown_update", "combined", "transport_schema", "non_object",
    "partial_judgment",
])
def test_kernel_control_patch_reuses_judgment_across_resume(
    tmp_path, monkeypatch, interrupt_after_patch, defect
):
    from pace.runtime import semantic

    judgment = {"stage_status": "PASS", "summary": "Integer addition yields four.",
                "proof_location": "Final equality.", "publication_eligible": True}
    transport = ProofPublicationTransport("NONE")
    complete = transport.complete_structured_result
    kernel_calls = []
    valid_relation = {
        "status": "PASS", "reuse_authorized": True, "statement": "The addition equals four.",
        "conditions": "Integer arithmetic", "supports": ["Final equality"],
        "allowed_success_contract_components": ["target"],
    }

    def serve(prompt, *, workdir, call_name, ledger_context, **kwargs):
        params = dict(workdir=workdir, call_name=call_name, ledger_context=ledger_context)
        if not call_name.startswith("math_verifier_kernel"):
            return complete(prompt, **params, **kwargs)
        kernel_calls.append(call_name)
        if call_name.endswith("_delivery_correction"):
            assert ledger_context["output_refs"] == [] and ledger_context["output_dirs"] == []
            payload = {"publication": {"action": "NONE"}}
            if defect == "non_object":
                assert "stage_status" in kwargs["output_schema"]["required"]
                payload.update(judgment)
            elif defect == "partial_judgment":
                assert set(kwargs["output_schema"]["required"]) == {
                    "publication", "publication_eligible", "proof_location",
                }
                assert "/proof_location" in prompt and "/publication_eligible" in prompt
                assert any(ref.endswith("/proof.md") for ref in ledger_context["input_refs"])
                payload.update(proof_location=judgment["proof_location"], publication_eligible=True)
            else:
                assert kwargs["output_schema"]["required"] == ["publication"]
                assert len(ledger_context["input_refs"]) == 1
                assert ledger_context["input_refs"][0].endswith("response.txt")
        else:
            assert set(map(lambda ref: Path(ref).name, ledger_context["input_refs"])) == {
                "problem.md", "target_contract.json", "proof.md"}
            assert any(ref.endswith("verification_methods.md") for ref in ledger_context["archive_refs"])
            payload = dict(judgment)
            if defect != "missing_publication":
                payload["publication"] = {
                    "action": "UPDATE", "finding_ref": "results/findings/R9999.md",
                }
                assert "no current findings to update" in prompt
            if defect in {"combined", "transport_schema", "partial_judgment"}:
                payload["relation_checks"] = [valid_relation, {**valid_relation, "statement": " "}]
            if defect == "partial_judgment":
                payload["proof_location"] = " "
                payload["publication_eligible"] = "yes"
            if defect == "transport_schema":
                payload["relation_checks"].append({"statement": "No scoped authorization."})
            if defect == "non_object":
                payload = ["not a receipt"]
        raw = json.dumps(payload)
        delivered = transport._reply(raw, **params, structured=True)
        if defect in {"transport_schema", "partial_judgment"} and not call_name.endswith("_delivery_correction"):
            from subprocess import CompletedProcess
            from pace.runtime.llm import decide_transport_outcome, reduce_codex_event_timeline

            return decide_transport_outcome(
                completed=CompletedProcess([], 0, stdout="", stderr=""),
                timeline=reduce_codex_event_timeline('{"type":"turn.completed"}\n'),
                current_output_text=raw, current_output_source="response.txt",
                output_schema=kwargs["output_schema"], invocation_id=delivered.invocation_id,
                call_name=call_name, attempt_count=1, artifact_dir=delivered.artifact_dir,
            )
        return delivered

    monkeypatch.setattr(transport, "complete_structured_result", serve)
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    question = tmp_path / "p.md"
    question.write_text("Prove 2+2=4.")
    root = tmp_path / "repair"
    finish = semantic.finish_call

    def interrupt(workdir, ref, **kwargs):
        finish(workdir, ref, **kwargs)
        record = json.loads((Path(workdir) / ref).read_text())
        if record["role"] == "math_verifier_kernel" and record["stage"].endswith("_delivery_correction") == interrupt_after_patch:
            raise KeyboardInterrupt

    with monkeypatch.context() as scoped:
        scoped.setattr(semantic, "finish_call", interrupt)
        assert cli.main(["run","--mode","proof","--resource-variant","standard","--output",str(tmp_path),
                         "--problem-id",root.name,"--problem",str(question),"--json"]) == 130
    assert cli.main(["resume","--run-dir",str(root),"--json"]) == 0
    assert kernel_calls == ["math_verifier_kernel_round_1", "math_verifier_kernel_round_1_delivery_correction"]
    report = json.loads(next((root / "work/turn001").rglob("math_verifier_kernel.json")).read_text())
    assert all(report[k] == v for k,v in judgment.items())
    assert report.get("relation_checks", []) == (
        [valid_relation] if defect in {"combined", "transport_schema", "partial_judgment"} else []
    )
    assert report["publication"]["action"] == "NONE"
    assert json.loads((root/"work/turn001/final_report.json").read_text())["result"] == "PASS"


def test_compact_kernel_has_one_judgment_and_no_implicit_lemma():
    from pace.agents.kernel import (attach_math_verifier_metadata, compact_kernel_schema,
                                    validate_compact_kernel)
    from pace.contracts.verification.decision import decide_final_result, verified_relations
    from pace.runtime.schema import validate_json_instance

    payload = {"stage_status":"MATH_FAIL", "summary":"The argument divides by x but x=0 is allowed.",
               "proof_location":"Equation (3)"}
    assert not validate_json_instance(payload, compact_kernel_schema())
    assert not validate_compact_kernel(payload)
    assert validate_compact_kernel({**payload, "summary":" "})
    assert validate_json_instance({**payload, "relation_checks":[{"status":"PASS","reuse_authorized":True}]}, compact_kernel_schema())
    bound = attach_math_verifier_metadata(payload, compact=True, proof_id="P", proof_ref="proof.md", target_contract_id="T")
    result = decide_final_result(auditor={"contract_status":"PASS"}, kernel=bound)
    assert result["failure_owner"] == "PROOF"
    assert result["canonical_blockers"] == [{"reason":payload["summary"],"proof_location":"Equation (3)"}]
    assert not verified_relations(bound, proof_id="P")


def test_compact_kernel_reuse_requires_evidence_and_tracks_scope():
    from pace.agents.director.verification_round import build_repair_progress
    from pace.agents.kernel import attach_math_verifier_metadata, validate_compact_kernel

    local = {
        "statement": "A uniform bound holds.",
        "conditions": "The step size is at most 1.",
        "supports": ["Lemma 1"],
        "allowed_success_contract_components": ["bound"],
        "status": "PASS",
        "reuse_authorized": True,
    }

    def report(row):
        return attach_math_verifier_metadata(
            {"stage_status": "MATH_FAIL", "summary": "The second conclusion remains open.",
             "proof_location": "Section 2", "relation_checks": [row]},
            compact=True, proof_id="P", proof_ref="proof.md", target_contract_id="T",
        )

    invalid = validate_compact_kernel(report({
        **local, "statement": "", "conditions": " ", "supports": [""],
        "allowed_success_contract_components": [" "],
    }))
    assert {d["json_pointer"].rsplit("/", 1)[1] for d in invalid} == {
        "statement", "conditions", "supports", "allowed_success_contract_components",
    }
    initial = report(local)
    assert not validate_compact_kernel(initial)

    def compare(current, previous=None):
        return build_repair_progress(
            current_proof_md="new draft", current_proof_id="P",
            current_kernel=current, previous_kernel=previous, final_result={},
        )

    first = compare(initial)["new_verified_lemma_ids"]
    assert len(first) == 1 and first[0]
    moved = compare(report({**local, "supports": ["Lemma 2"]}), initial)
    assert moved["new_verified_lemma_ids"] == moved["lost_proof_bound_lemma_ids"] == []
    changed = compare(report({**local, "conditions": "The step size is at most 2."}), initial)
    assert changed["lost_proof_bound_lemma_ids"] == first
    assert len(changed["new_verified_lemma_ids"]) == 1


@pytest.mark.parametrize("status", ["PASS", "MATH_FAIL"])
def test_invalid_optional_kernel_export_does_not_trigger_another_review(tmp_path, status):
    from pace.agents.kernel import run_math_verifier_kernel
    from pace.runtime.prompts import materialize_run_resources

    ws = RunWorkspace.create(tmp_path, "optional-export")
    materialize_run_resources(ws.root, resource_variant="standard")
    proof = ws.work_ref("round1/proof.md")
    ws.write_text(proof, "The candidate proof.")
    ws.write_text(ws.work_ref("input/problem.md"), "The original problem.")
    ws.write_json(ws.work_ref("input/target_contract.json"), {"contract_id": "TARGET"})
    judgment = {"stage_status": status, "summary": "The decisive inference.",
                "proof_location": "Equation 2"}

    class Transport(ProofPublicationTransport):
        def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
            assert call_name == "math_verifier_kernel_round_1"
            payload = {
                **judgment, "publication_eligible": True, "publication": {"action": "CREATE"},
                "relation_checks": [{"status": "PASS", "statement": "Lemma without proof"}],
            }
            return self._reply(
                json.dumps(payload), workdir=workdir, call_name=call_name,
                ledger_context=ledger_context, structured=True,
            )

    transport = Transport("CREATE")
    committed = []
    report = run_math_verifier_kernel(
        proof_id="P", proof_ref=proof, target_contract_id="TARGET", workdir=ws.root,
        relation_llm=transport, artifact_refs={"proof_file": proof},
        output_ref=ws.work_ref("round1/verification/kernel.json"), on_accept=committed.append,
    )
    assert transport.calls == ["math_verifier_kernel_round_1"]
    assert committed == [report]
    assert all(report[key] == value for key, value in judgment.items())
    assert report["relation_checks"] == []
    assert ("publication" in report) == (status == "PASS")
