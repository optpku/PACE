"""Exercise turn ownership and outer shared commits through the real CLI."""

import json
import shutil

import pytest
from pace import cli
from pace.artifacts.workspace import work_ref
from pace.runtime import prompts
from test_terminal_result import ResearchTransport


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
