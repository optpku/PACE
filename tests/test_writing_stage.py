"""Writing progress belongs with its finalization calls, including on failure."""

import json
import re
from pathlib import Path

import pytest

from pace import cli
from pace.runtime.llm import TransportSuccess
from pace.runtime.chat_api import WorkspaceTools
from pace.artifacts.calls import attempt_directory
from pace.artifacts.events import read_updates
from test_terminal_result import ResearchTransport


class WritingTransport(ResearchTransport):
    def __init__(self, *, failed_action=None):
        super().__init__()
        self.failed_action = failed_action

    def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        self.calls.append(call_name)
        root = Path(workdir)
        if call_name == "theory_paper_writer":
            record = json.loads((root / ledger_context["call_ref"]).read_text())
            instructions = (Path(record["agent_home"]) / "AGENTS.md").read_text()
            for text in (instructions, prompt):
                assert "workspace-conventions" in text
                assert "latexmk -xelatex" in text
        output = root / re.search(r"## Deliverable\nWrite `([^`]+)`", prompt)[1]
        output.parent.mkdir(parents=True, exist_ok=True)
        if call_name != self.failed_action:
            WorkspaceTools(root, ledger_context).execute('write_file', {
                'path': output.relative_to(root).as_posix(), 'content':
                r"\documentclass{article}\begin{document}An open target.\end{document}"
                if output.suffix == ".tex" else "# Writing result\n\nThe original target remains open.\n"
            })
        folder = attempt_directory(root, ledger_context["call_ref"], 1)
        (folder / "response.txt").write_text("The writing task returned.")
        return TransportSuccess(
            text="The writing task returned.", invocation_id=f"writing-{len(self.calls)}",
            call_name=call_name, exit_code=0, terminal_event_type="turn.completed",
            accepted_output_source="file", output_schema_valid=True, attempt_count=1,
            artifact_dir=folder,
        )


def test_writing_role_uses_its_request_resource_snapshot(tmp_path):
    from pace.orchestrator.writing.report import ResearchReportOutliner, outline_state_ref
    from pace.agents.director.harness import prepare_agent_environments
    from pace.runtime.agent_environment import role_home
    from pace.artifacts.workspace import RunWorkspace
    from pace.runtime.prompts import materialize_run_resources, prompt_root

    ws = RunWorkspace.create(tmp_path, "writing")
    materialize_run_resources(ws.root, resource_variant="standard")
    prepare_agent_environments(ws.root, mode="research")
    marker = "Follow the resource snapshot selected for this writing request."

    class RequestTransport(WritingTransport):
        def complete_unstructured_result(self, prompt, *, workdir, ledger_context, **kwargs):
            record = json.loads((Path(workdir) / ledger_context["call_ref"]).read_text())
            assert marker in (Path(record["agent_home"]) / "AGENTS.md").read_text()
            return super().complete_unstructured_result(
                prompt, workdir=workdir, ledger_context=ledger_context, **kwargs,
            )

    transport = RequestTransport()
    writer = ResearchReportOutliner(llm=transport,  workdir=ws.root)
    writer.request("Explain the current research and its open questions.", resource_variant="weak")
    state = json.loads(ws.path(outline_state_ref(ws.root)).read_text())
    template = ws.path(state["resources_ref"]) / "prompts/report_writer/outline.md"
    template.write_text(template.read_text() + "\n" + marker)
    assert writer.update()
    assert marker in (role_home(ws.root, "RESEARCH_REPORT_OUTLINE") / "AGENTS.md").read_text()
    assert marker not in (prompt_root(ws.root) / "report_writer/outline.md").read_text()


@pytest.mark.parametrize("failed_action", [
    None, "research_report_outline", "research_report_writer",
    "theory_paper_outline", "theory_paper_writer",
])
def test_cli_keeps_writing_progress_out_of_orientation(tmp_path, monkeypatch, failed_action):
    transport = WritingTransport(failed_action=failed_action)
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **settings: transport)
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **settings: transport)
    builds = []

    def build_paper(directory):
        builds.append(directory)
        status = "UNAVAILABLE" if len(builds) == 1 else "COMPLETE"
        result = {"status": status, "reason": ""}
        (directory / "build.json").write_text(json.dumps(result))
        if status == "COMPLETE":
            (directory / "main.pdf").write_bytes(b"%PDF-writing-stage")
        return result

    monkeypatch.setattr("pace.orchestrator.writing.paper.build_paper", build_paper)
    problem = tmp_path / "problem.md"
    problem.write_text("Classify all structures satisfying the stated axioms.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "topic",
                     "--output", str(tmp_path), "--json"]) == 0
    root = tmp_path / "topic"
    final_report = root / "work/turn001/final_report.json"
    original = final_report.read_bytes()
    for action, agent in (
        ("outline", "research_report_outline"), ("report", "research_report_writer"),
        ("paper-outline", "theory_paper_outline"), ("paper", "theory_paper_writer"),
    ):
        arguments = [action, "--run-dir", str(root), "--json"]
        if action in {"outline", "paper-outline"}:
            arguments.extend(("--message", str(problem)))
        if agent == failed_action:
            assert cli.main(arguments) == 2
            break
        assert cli.main(arguments) == 0
    if failed_action is None:
        # Rebuilding an already delivered paper also publishes a finalization event.
        assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
        assert len(builds) == 2
    updates = [event for event in read_updates(root) if event["stage"] == "reporter"]
    assert updates
    assert all(event["stage_ref"] == "work/turn001/finalization" for event in updates)
    assert all(not event["terminal"] for event in updates)
    assert updates[-1]["status"] == ("failed" if failed_action else "completed")
    assert final_report.read_bytes() == original


@pytest.mark.parametrize("mode", ["proof", "research"])
def test_completed_outline_yields_to_interrupted_math(tmp_path, monkeypatch, capsys, mode):
    from test_research_round_limit import RoundLimitTransport

    from test_finding_publication import ProofPublicationTransport

    class InterruptedVerifier(RoundLimitTransport):
        interrupted = False

        def complete_structured_result(self, prompt, *, call_name, **kwargs):
            if "proof_contract_auditor" in call_name and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            if mode == "proof":
                return ProofPublicationTransport.complete_structured_result(
                    self, prompt, call_name=call_name, **kwargs)
            return super().complete_structured_result(prompt, call_name=call_name, **kwargs)

    transport = InterruptedVerifier("prover")
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: transport)
    source = tmp_path / "problem.md"
    source.write_text("Prove 2+2=4.\n")
    root = tmp_path / "example"
    assert cli.main(["run", "--problem", str(source), "--problem-id", root.name,
                     "--output", str(tmp_path), "--mode", mode, "--model", "math-model",
                     "--provider", "chat-completions", "--web-search-model", "survey-model",
                     "--web-search-reasoning-effort", "medium", "--web-search-mode", "off",
                     "--timeout-seconds", "91", "--json"]) == 130
    turn = root / "work/turn001"
    state = turn / "state" / ("proof_state.json" if mode == "proof" else "director_state.json")
    assert json.loads(state.read_text())["phase"] == "VERIFY"
    with monkeypatch.context() as scoped:
        writer = WritingTransport()
        scoped.setattr("pace.agents.director.harness.build_llm", lambda **_: writer)
        scoped.setattr("pace.pipeline.build_llm", lambda **_: writer)
        assert cli.main(["outline", "--run-dir", str(root), "--message", str(source), "--json"]) == 0
    before = list(transport.calls)
    outline = (root / "results/outline.md").read_bytes()
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert json.loads((turn / "final_report.json").read_text())["result"] == "PASS"
    assert set(before).isdisjoint(transport.calls[len(before):])
    assert (root / "results/outline.md").read_bytes() == outline
    settings = json.loads((turn / "state/sessions.json").read_text())[-1]["effective_config"]
    assert (settings["provider"], settings["model"], settings["reasoning_effort"]) == ("chat-completions", "math-model", "xhigh")
    assert (settings["web_search_model"], settings["web_search_reasoning_effort"], settings["web_search_mode"]) == ("survey-model", "medium", "off")
    assert settings["timeout_seconds"] == 91
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["research_outline"] == str(root / "results/outline.md")


@pytest.mark.parametrize(("action", "boundary"), [
    (action, boundary)
    for action in ("outline", "report", "paper-outline", "paper")
    for boundary in ("author", "projection")
] + [("paper", "compiled"), ("paper", "build_receipt")])
def test_accepted_writer_resumes_pending_local_delivery(tmp_path, monkeypatch, capsys, action, boundary):
    from pace.agents import paper_writer, report_writer
    from pace.orchestrator.writing import tasks
    from pace.orchestrator.writing.paper import TheoryPaperWriter

    class DraftTransport(WritingTransport):
        def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
            reply = super().complete_unstructured_result(
                prompt, workdir=workdir, call_name=call_name, ledger_context=ledger_context, **kwargs,
            )
            if call_name == "theory_paper_writer":
                bundle = (Path(workdir) / ledger_context["output_refs"][0]).parent
                (bundle / "build.json").write_text('{"status":"COMPLETE"}')
                (bundle / "main.pdf").write_bytes(b"author preview")
            return reply

    transport = DraftTransport()
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Classify the admissible structures.")
    root = tmp_path / "topic"
    assert (
        cli.main(
            [
                "run",
                "--problem",
                str(problem),
                "--problem-id",
                "topic",
                "--output",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    if action in ("report", "paper"):
        pre = "outline" if action == "report" else "paper-outline"
        assert (
            cli.main([pre, "--run-dir", str(root), "--message", str(problem), "--json"])
            == 0
        )

    builds = []

    def build(directory):
        builds.append(directory)
        (directory / "build.json").write_text('{"status":"COMPLETE"}')
        (directory / "main.pdf").write_bytes(b"%PDF-test")

    monkeypatch.setattr("pace.orchestrator.writing.paper.build_paper", build)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    args = [action, "--run-dir", str(root), "--json"]
    if action.endswith("outline"):
        args.extend(["--message", str(problem)])
    with monkeypatch.context() as scoped:
        if boundary == "author":
            role = paper_writer if action.startswith("paper") else report_writer
            run = role.run

            def stop_after_reply(*args, **kwargs):
                assert run(*args, **kwargs).transport_status == "SUCCESS"
                raise KeyboardInterrupt()

            scoped.setattr(role, "run", stop_after_reply)
        elif boundary == "build_receipt":
            def interrupt_after_build(directory):
                build(directory)
                raise KeyboardInterrupt()

            scoped.setattr("pace.orchestrator.writing.paper.build_paper", interrupt_after_build)
        elif boundary == "compiled":
            import shutil
            copy = shutil.copytree

            def stop_before_publication(src, dst, *args, **kwargs):
                if Path(dst) == root / "results/paper":
                    raise KeyboardInterrupt()
                return copy(src, dst, *args, **kwargs)

            scoped.setattr(shutil, "copytree", stop_before_publication)
        elif action == "paper":
            scoped.setattr(TheoryPaperWriter, "_build_delivery", interrupt)
        else:
            scoped.setattr(tasks, "_write_text_atomic", interrupt)
        assert cli.main(args) == 130
    before = list(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == before
    dest = {
        "paper": "paper/main.pdf",
        "paper-outline": "paper_outline.md",
        "outline": "outline.md",
        "report": "research_report.md",
    }[action]
    assert (root / "results" / dest).is_file()
    if action == "paper":
        assert (root / "results" / dest).read_bytes() == b"%PDF-test"
        (root / "results" / dest).unlink()
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    if action == "paper":
        # This resume restored the missing reader PDF; the next resumes mathematics.
        assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    key = {"paper": "paper_pdf", "paper-outline": "paper_outline",
           "outline": "research_outline", "report": "research_report"}[action]
    assert terminal["event"] == "run_completed" and terminal[key] == str(root / "results" / dest)
    assert transport.calls == before
    assert (root / "results" / dest).is_file()
    assert len(builds) == int(action == "paper")
    events = [json.loads(line) for line in (root / "work/turn001/events.jsonl").read_text().splitlines()]
    succeeded = [event["call_id"] for event in events if event["event_kind"] == "SEMANTIC_CALL_SUCCEEDED"]
    assert len(succeeded) == len(set(succeeded))


@pytest.mark.parametrize(("defect", "outcome"), [
    ("format", "REPRESENTATION_FAILED"), ("transport", "INTERFACE_FAILED"),
])
def test_resume_retries_unaccepted_writing_even_if_the_file_exists(tmp_path, monkeypatch, defect, outcome):
    from pace.orchestrator.writing.report import outline_state_ref
    from pace.runtime.llm import TransportFailure

    class InterruptedAuthor(WritingTransport):
        def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
            reply = super().complete_unstructured_result(
                prompt, workdir=workdir, call_name=call_name, ledger_context=ledger_context, **kwargs,
            )
            if self.calls.count(call_name) == 1:
                draft = Path(workdir) / ledger_context["output_refs"][0]
                assert draft.is_file()
                if defect == "format":
                    draft.write_text("The formula is $a quad b$.")
                else:
                    return TransportFailure(
                        invocation_id=reply.invocation_id, call_name=call_name, error_code="INTERRUPTED",
                        retry_class="TRANSPORT", retryable=True, exit_code=1, terminal_event_type="error",
                        attempt_count=1, diagnostics=(), artifact_dir=reply.artifact_dir,
                    )
            return reply

    transport = InterruptedAuthor()
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    writer_settings = []
    def writing_model(**settings):
        writer_settings.append((settings["model"], settings["timeout_seconds"]))
        return transport
    monkeypatch.setattr("pace.pipeline.build_llm", writing_model)
    problem = tmp_path / "problem.md"
    problem.write_text("Classify the admissible structures.")
    root = tmp_path / "topic"
    assert cli.main(["run", "--problem", str(problem), "--problem-id", root.name,
                     "--output", str(tmp_path), "--json"]) == 0
    assert cli.main(["outline", "--run-dir", str(root), "--message", str(problem), "--model", "writing-model", "--timeout-seconds", "123", "--json"]) == 2
    state = json.loads((root / outline_state_ref(root)).read_text())
    assert not state["outline_ref"] and "accepted_source_ref" not in state
    events = [json.loads(row) for row in (root / "work/turn001/events.jsonl").read_text().splitlines()]
    assert any(event.get("call_name") == "research_report_outline"
               and event.get("terminal_outcome") == outcome for event in events)
    calls = list(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == calls + ["research_report_outline"]
    assert writer_settings == [("writing-model", 123)] * 2

@pytest.mark.parametrize("outline,write,publication", [
    ("outline", "report", "research_report"), ("paper-outline", "paper", "paper_source"),
])
def test_writing_uses_the_selected_outlines_request(tmp_path, monkeypatch, outline, write, publication):
    from pace.orchestrator.writing.tasks import active_writing_request, writing_artifacts
    def build(directory):
        (directory / "build.json").write_text('{"status":"COMPLETE"}')
        (directory / "main.pdf").write_bytes(b"%PDF-published")
    monkeypatch.setattr("pace.orchestrator.writing.paper.build_paper", build)
    transport = WritingTransport()
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: transport)
    message = tmp_path / "topic.md"
    message.write_text("FIRST TOPIC")
    root = tmp_path / "topic"
    args = ["--run-dir", str(root), "--json"]
    assert cli.main(["run", "--problem", str(message), "--output", str(tmp_path), "--json"]) == 0
    assert cli.main([outline, *args, "--message", str(message)]) == 0
    assert cli.main([write, *args]) == 0
    first = writing_artifacts(root)[publication]
    first_bytes = (root / first).read_bytes()
    message.write_text("SECOND TOPIC")
    assert cli.main([outline, *args, "--message", str(message)]) == 0
    assert writing_artifacts(root).get(publication) == first
    assert (root / first).read_bytes() == first_bytes
    standalone = tmp_path / "independent.md"
    standalone.write_text("THIRD OUTLINE")
    before = list(transport.calls)
    assert cli.main([write, *args, "--outline", str(standalone)]) == 2
    assert transport.calls == before
    message.write_text("THIRD REQUEST")
    assert cli.main([write, *args, "--outline", str(standalone), "--message", str(message)]) == 0
    request = active_writing_request(root)["request"]
    assert (root / request["outline_ref"]).read_text() == "THIRD OUTLINE"
    saved = (root / request["request_ref"]).read_text()
    assert "THIRD REQUEST" in saved and "SECOND TOPIC" not in saved


def test_failed_replacement_preserves_published_pdf_until_resume(tmp_path, monkeypatch, capsys):
    transport = WritingTransport()
    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: transport)
    source = tmp_path / "question.md"
    source.write_text("Classify the possible solutions.")
    root = tmp_path / "case"
    args = ["--run-dir", str(root), "--json"]
    assert cli.main(["run", "--problem", str(source), "--problem-id", root.name, "--output", str(tmp_path), "--json"]) == 0
    assert cli.main(["paper-outline", *args, "--message", str(source)]) == 0

    builds = []
    def build(directory):
        builds.append(directory)
        status = "FAILED" if len(builds) == 2 else "COMPLETE"
        (directory / "build.json").write_text(json.dumps({"status": status}))
        if status == "COMPLETE":
            (directory / "main.pdf").write_bytes(f"%PDF-{len(builds)}".encode())
    monkeypatch.setattr("pace.orchestrator.writing.paper.build_paper", build)
    assert cli.main(["paper", *args]) == 0
    pdf = root / "results/paper/main.pdf"
    published = pdf.read_bytes()
    assert cli.main(["paper", *args]) == 0
    attempt = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert pdf.read_bytes() == published
    assert attempt["paper_build"] == "FAILED" and "paper_pdf" not in attempt
    assert Path(attempt["paper_source"]) == builds[-1] / "main.tex"
    calls = list(transport.calls)
    assert cli.main(["resume", *args]) == 0
    assert cli.main(["resume", *args]) == 0
    assert builds[1] == builds[2] and len(builds) == 3
    assert pdf.read_bytes() == b"%PDF-3" and transport.calls == calls
