"""Exercise turn ownership and outer shared commits through the real CLI."""

import json
import shutil

import pytest
from pace import cli
from pace.artifacts.workspace import work_ref
from pace.runtime import prompts
from test_terminal_result import ResearchTransport


@pytest.mark.parametrize("variant", ["standard", "weak"])
def test_resume_rejects_resources_with_retired_verification_protocol(tmp_path, variant):
    destination = tmp_path / "resources"
    prompts.materialize_run_resources(tmp_path, resource_variant=variant, destination=destination)
    prompts.validate_resource_snapshot(destination, resource_variant=variant)
    path = destination / "manifest.json"
    manifest = json.loads(path.read_text())
    del manifest["verification_policy"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsupported resource snapshot protocol"):
        prompts.validate_resource_snapshot(destination, resource_variant=variant)


@pytest.mark.parametrize("requested_mode", [None, "research"])
def test_orientation_resume_keeps_snapshot_and_continue_selects_new_resources(
    tmp_path, monkeypatch, requested_mode
):
    from pace.agents import orientation
    from pace.runtime.agent_environment import role_home

    packaged = tmp_path / "packaged"
    shutil.copytree(prompts._PACKAGED_RESOURCES, packaged)
    role = packaged / "prompts/director/role.standard.md"
    role.write_text(role.read_text() + "\nFIRST_SNAPSHOT_RULE\n")
    monkeypatch.setattr(prompts, "_PACKAGED_RESOURCES", packaged)
    transport = ResearchTransport()
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    original_call = orientation.semantic_call
    interrupted = False

    def interrupt_after_accept(*args, **kwargs):
        nonlocal interrupted
        result = original_call(*args, **kwargs)
        if kwargs["call_name"] == "problem_orientation_extract" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(orientation, "semantic_call", interrupt_after_accept)
    problem = tmp_path / "problem.md"
    problem.write_text("Classify structures satisfying the stated axioms.\n")
    root = tmp_path / "snapshot"
    arguments = [
        "run",
        "--problem",
        str(problem),
        "--problem-id",
        root.name,
        "--output",
        str(tmp_path),
        "--resource-variant",
        "standard",
        "--json",
    ]
    if requested_mode:
        arguments.extend(("--mode", requested_mode))
    assert cli.main(arguments) == 130
    checkpoint = root / work_ref(root, "state/orientation_state.json")
    assert json.loads(checkpoint.read_text())["phase"] == "TARGET"
    (role_home(root, "PROBLEM_ORIENTATION_EXTRACTOR") / "AGENTS.md").unlink()
    role.write_text(role.read_text().replace("FIRST_SNAPSHOT_RULE", "NEXT_TURN_RULE"))
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert (role_home(root, "PROBLEM_ORIENTATION_EXTRACTOR") / "AGENTS.md").is_file()
    assert transport.calls.count("problem_orientation_extract") == 1
    home = role_home(root, "GLOBAL_RESEARCH_DIRECTOR")
    assert "FIRST_SNAPSHOT_RULE" in (home / "AGENTS.md").read_text()

    follow_up = tmp_path / "follow-up.md"
    follow_up.write_text("Continue with the same original scope.")
    assert (
        cli.main(
            [
                "continue",
                "--run-dir",
                str(root),
                "--message",
                str(follow_up),
                "--resource-variant",
                "standard",
                "--json",
            ]
        )
        == 0
    )
    assert transport.calls.count("problem_orientation_extract") == 2
    assert (
        "NEXT_TURN_RULE"
        in (role_home(root, "GLOBAL_RESEARCH_DIRECTOR") / "AGENTS.md").read_text()
    )
    context = json.loads(
        (root / work_ref(root, "input/continuation_context.json")).read_text()
    )
    assert context["source_turn"] == "turn001"
    assert context["authority"] == "SEARCH_CONTEXT_ONLY"


def test_proof_terminal_publication_retries_only_on_resume(tmp_path, monkeypatch):
    from pace.orchestrator import results
    from test_finding_publication import ProofPublicationTransport

    transport = ProofPublicationTransport("CREATE")
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    original = results.persist_finding
    attempts = []

    def interrupt_once(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError("publication interrupted after independent proof acceptance")
        return original(*args, **kwargs)

    monkeypatch.setattr(results, "persist_finding", interrupt_once)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    root = tmp_path / "proof"
    assert (
        cli.main(
            [
                "run",
                "--problem",
                str(problem),
                "--problem-id",
                root.name,
                "--output",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    assert len(attempts) == 1
    calls = list(transport.calls)
    report = json.loads((root / work_ref(root, "final_report.json")).read_text())
    assert report["result"] == "PASS" and report["publication_status"] == "INCOMPLETE"
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == calls
    assert len(attempts) == 2
    assert (
        len(json.loads((root / "work/state/findings.json").read_text())["publications"])
        == 1
    )


@pytest.mark.parametrize("missing", ["brief", "budget", "neither"])
def test_combined_orientation_repairs_only_missing_context_after_extraction(tmp_path, monkeypatch, missing):
    from pace.agents import orientation
    from pace.artifacts.workspace import RunWorkspace
    from pace.contracts.problem.loader import Problem

    ws = RunWorkspace.create(tmp_path, "combined")
    prompts.materialize_run_resources(ws.root, resource_variant="standard")
    problem = Problem("combined", "Determine all such groups.\n", "test")
    ws.write_text(ws.work_ref("input/problem.md"), problem.text)
    budget = {"difficulty":"research", "plan_node_level":2, "dependency_depth_level":1}
    brief = "# Finite groups\n\nClassify the specified groups.\n"
    brief_ref = ws.work_ref("input/problem_brief.md")
    calls = []

    def reply(llm, prompt, *, call_name, spec, host_metadata, **kwargs):
        calls.append(call_name)
        if call_name == "problem_orientation_extract":
            context = {"brief_markdown":brief, "proof_assessment": budget}
            if missing == "brief":
                context["brief_markdown"] = "#"
            if missing == "budget":
                context["proof_assessment"] = {"difficulty":"invalid"}
            spec.on_accept({"target_clauses":[{"start_line":1,"end_line":1}],
                            "initial_context":context})
            raise KeyboardInterrupt
        if missing == "budget":
            assert ws.path(brief_ref).read_text() == brief
            assert not host_metadata.get("output_refs")
            payload = {"proof_assessment":budget}
        else:
            assert missing == "brief" and host_metadata["output_refs"] == [brief_ref]
            ws.write_text(brief_ref, brief)
            payload = {"brief_ref":brief_ref}
        spec.on_accept(payload)

    monkeypatch.setattr(orientation, "semantic_call", reply)
    def start():
        return orientation.run_problem_orientation(llm=None, workdir=ws.root, problem=problem,
                                                   requested_mode="research")
    with pytest.raises(KeyboardInterrupt):
        start()
    result = start()
    assert start() == result and result["execution_mode"] == "research"
    assert calls.count("problem_orientation_extract") == 1
    assert calls.count("problem_research_decision") == int(missing != "neither")
    assert ws.path(result["problem_brief_ref"]).read_text() == brief
    assert json.loads(ws.path(result["assessment_ref"]).read_text()) == {"proof_assessment":budget}


def test_kernel_binding_uses_own_turn_across_protocol_change(tmp_path, monkeypatch):
    from pace.contracts.proof.binding import ProofBindingError, resolve_verification_binding
    from test_finding_publication import ProofPublicationTransport

    transport = ProofPublicationTransport("NONE")
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.")
    root = tmp_path / "protocols"
    assert cli.main(["run", "--problem", str(problem), "--problem-id", root.name, "--output", str(tmp_path),
                     "--resource-variant", "standard", "--json"]) == 0
    assert cli.main(["continue", "--run-dir", str(root), "--message", str(problem),
                     "--resource-variant", "weak", "--json"]) == 0
    def check(turn):
        path = next((root/"work"/turn).rglob("verification_binding.json"))
        return resolve_verification_binding(root, verification_binding_ref=path.relative_to(root).as_posix())
    # Each turn validates its own scoped answer. An earlier PASS cannot verify a follow-up.
    with pytest.raises(ProofBindingError, match="target_contract_ref"):
        check("turn001")
    assert check("turn002")["status"] == "PASS"
    assert json.loads((root / "work/turn001/final_report.json").read_text())["result"] == "PASS"
    res = root / "work/turn002/resources/manifest.json"
    original_manifest = res.read_text()
    doc = json.loads(original_manifest)
    doc.update(resource_variant="standard", workflow_profile="compact/v1")
    res.write_text(json.dumps(doc))
    with pytest.raises(ProofBindingError, match="unsupported resource snapshot protocol"):
        check("turn002")
    res.write_text(original_manifest)
    report = next((root/"work/turn002").rglob("math_verifier_kernel.json"))
    forged = json.loads(report.read_text())
    forged.update(schema_version="math_verifier_kernel/v18", summary="A claim.", proof_location="Conclusion.")
    report.write_text(json.dumps(forged))
    with pytest.raises(ProofBindingError, match="protocol"):
        check("turn002")
