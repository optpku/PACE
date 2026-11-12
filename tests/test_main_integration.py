"""Exercise the fused production path and its saved authority boundaries."""
import json
from pathlib import Path

import pytest

from pace import cli
from pace.runtime.artifact_format import repair_artifact_batch
from pace.runtime.output.workspace import RunWorkspace
from pace.runtime.pinned_runtime import reexec_pinned_runtime as select_saved_runtime
from pace.runtime.prompts import materialize_run_resources
from test_finding_publication import ProofPublicationTransport


def test_research_format_repair_preserves_original_and_resumes_locally(tmp_path):
    ws = RunWorkspace.create(tmp_path, "format")
    ref = ws.work_ref("round0001/research.md")
    original = r"Let $\left{ x \right}$ denote the set."
    ws.write_text(ref, original)
    state = {}
    state_ref = ws.work_ref("round0001/delivery.json")
    repair_artifact_batch(llm=object(), root=ws.root, state=state,
                          state_ref=state_ref, refs=(ref,))
    assert ws.path(ref).read_text() == r"Let $\left\{ x \right\}$ denote the set."
    backups = list(ws.path(ws.work_ref("round0001/artifact_format/originals")).glob("*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    before = ws.path(ref).read_bytes()
    repair_artifact_batch(llm=object(), root=ws.root, state=state,
                          state_ref=state_ref, refs=(ref,))
    assert ws.path(ref).read_bytes() == before
    assert state["format_batch"]["applied"]


class PublicationCorrectionTransport(ProofPublicationTransport):
    def __init__(self):
        super().__init__("UPDATE")
        self.publication["finding_ref"] = "results/findings/missing.md"

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        if call_name.endswith("_publication_correction"):
            return self._reply(json.dumps({"publication": {"action": "CREATE"}}),
                               workdir=workdir, call_name=call_name,
                               ledger_context=ledger_context, structured=True)
        return super().complete_structured_result(
            prompt, workdir=workdir, call_name=call_name,
            ledger_context=ledger_context, **kwargs)


def test_publication_field_correction_reuses_the_completed_math_check(tmp_path, monkeypatch):
    transport = PublicationCorrectionTransport()
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: transport)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "publication",
                     "--output", str(tmp_path), "--json"]) == 0
    root = tmp_path / "publication"
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert report["result"] == "PASS"
    checks = [name for name in transport.calls if name.startswith("math_verifier_kernel_")]
    assert len(checks) == 2
    assert sum(name.endswith("_publication_correction") for name in checks) == 1
    assert (root / "results/findings/R0001.md").is_file()
    before = list(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == before


def test_resume_publishes_without_repeating_the_accepted_proof(tmp_path, monkeypatch):
    from pace.runtime.output import results

    transport = ProofPublicationTransport("CREATE")
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: transport)
    publish = results.publish_finding

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(results, "publish_finding", interrupted)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    assert cli.main(["run", "--problem", str(problem), "--problem-id", "resume",
                     "--output", str(tmp_path), "--json"]) == 130
    root = tmp_path / "resume"
    before = list(transport.calls)
    monkeypatch.setattr(results, "publish_finding", publish)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls == before
    report = json.loads((root / "work/turn001/final_report.json").read_text())
    assert report["result"] == "PASS"
    assert (root / "results/findings/R0001.md").is_file()


def test_snapshot_handoff_uses_exact_saved_code_and_preserves_checkpoint(tmp_path, monkeypatch):
    ws = RunWorkspace.create(tmp_path, "runtime")
    calls = []
    monkeypatch.setattr("os.execvpe", lambda exe, args, env: calls.append((args, env)))
    select_saved_runtime(ws.root, ["resume", "--run-dir", str(ws.root)])
    assert calls[0][0][1:3] == ["-m", "pace"]
    assert calls[0][1]["PYTHONPATH"].split(":")[0] == str(ws.path("work/runtime/initial"))
    checkpoint = ws.path("work/state/current_turn.json").read_bytes()
    source = ws.path("work/runtime/initial/pace/__main__.py")
    source.write_text(source.read_text() + "\n# changed executable\n")
    with pytest.raises(ValueError, match="Pinned runtime changed"):
        select_saved_runtime(ws.root, ["resume", "--run-dir", str(ws.root)])
    assert ws.path("work/state/current_turn.json").read_bytes() == checkpoint
    assert len(calls) == 1


def test_index_rebuild_links_saved_research_without_changing_evidence(tmp_path):
    ws = RunWorkspace.create(tmp_path, "navigation")
    ws.write_text(ws.work_ref("input/problem.md"), "The literal target.")
    source = ws.work_ref("round0001/research.md")
    ws.write_text(source, "# Partial result\n\nThe remaining implication is open.")
    ws.write_json(ws.work_ref("round0001/agent_result.json"),
                  {"agent_id": "research", "status": "SUCCEEDED", "deliverable_ref": source})
    before = ws.path(source).read_bytes()
    assert cli.main(["index", "--run-dir", str(ws.root)]) == 0
    assert "round0001" in ws.path(ws.work_ref("README.md")).read_text()
    assert "research.md" in ws.path(ws.work_ref("round0001/README.md")).read_text()
    assert ws.path(source).read_bytes() == before


class FindingTransport(ProofPublicationTransport):
    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        return self._reply(json.dumps({"stage_status": "PASS", "reuse_authorized": False,
                                      "publication": {"action": "NONE"}}),
                           workdir=workdir, call_name=call_name,
                           ledger_context=ledger_context, structured=True)


def test_finding_check_reuses_the_exact_binary_source_version(tmp_path):
    from pace.agents.kernel import verify_finding

    ws = RunWorkspace.create(tmp_path, "binary")
    materialize_run_resources(ws.root, resource_variant="standard")
    draft = ws.work_ref("round0001/findings/bound.md")
    ws.write_text(draft, "# Bound\n\nAn independently checked local calculation.")
    source = ws.path("sources/paper.pdf")
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.5\n%\x92\n%%EOF\n")
    transport = FindingTransport("NONE")
    args = dict(llm=transport, workdir=ws.root, draft_ref=draft,
                input_refs=(draft, "sources/paper.pdf"),
                round_index=1, target_contract_id="TARGET")
    assert verify_finding(**args).payload["stage_status"] == "PASS"
    assert verify_finding(**args).payload["stage_status"] == "PASS"
    assert len(transport.calls) == 1
    source.write_bytes(b"%PDF-1.5\nA different source version.\n%%EOF\n")
    with pytest.raises(ValueError, match="another manuscript or target"):
        verify_finding(**args)


def test_finding_format_repair_retains_frozen_original_and_reuses_verdict(tmp_path):
    from pace.orchestrator.finding_publication import publish_stage_findings
    from pace.runtime.output.results import finding_publications

    ws = RunWorkspace.create(tmp_path, "finding-format")
    materialize_run_resources(ws.root, resource_variant="standard")
    ws.write_text(ws.work_ref("input/problem.md"), "Determine the sharp bound.")
    ws.write_json(ws.work_ref("input/target_contract.json"), {"contract_id": "TARGET"})
    directory = ws.work_ref("round0001")
    source = directory + "/findings/bound.md"
    original = r"The bound concerns $\left{ x \right}$."
    ws.write_text(source, original)
    result = directory + "/agent_result.json"
    ws.write_json(result, {"status": "SUCCEEDED", "deliverable_ref": source})
    ws.write_json(directory + "/task.json", {"round_index": 1})
    transport = FindingTransport("NONE")
    args = dict(llm=transport, workdir=ws.root, completed_result_refs=(result,))
    assert publish_stage_findings(**args)
    assert publish_stage_findings(**args)
    assert ws.path(source).read_text() == original
    row, = finding_publications(ws.root)
    assert row["source_ref"] != source
    assert r"\left\{" in ws.path(row["source_ref"]).read_text()
    assert len(transport.calls) == 1


class PhaseTransport(ProofPublicationTransport):
    def __init__(self, review):
        super().__init__("NONE")
        self.review = review

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        self.review.write_text("# Independent review\n\nThe stated local question is coherent.")
        return self._reply(json.dumps({"action": "ACCEPT", "dependency_refs": []}),
                           workdir=workdir, call_name=call_name,
                           ledger_context=ledger_context, structured=True)


def test_rci_phase_commits_and_reuses_its_review_without_another_call(tmp_path):
    from pace.agents.rci.phase_runner import RciPhaseRunner

    ws = RunWorkspace.create(tmp_path, "rci")
    materialize_run_resources(ws.root, resource_variant="standard")
    target = ws.work_ref("input/problem.md")
    seed = ws.work_ref("round0001/question.md")
    ws.write_text(target, "Determine the literal sharp bound.")
    ws.write_text(seed, "# Local question\n\nDetermine whether the bound is at most four.")
    directory = ws.path(ws.work_ref("round0001/rci/attempt_001/candidate_01/critic"))
    directory.mkdir(parents=True)
    transport = PhaseTransport(directory / "review.md")
    runner = RciPhaseRunner(llm=transport)
    args = dict(phase="RAW_SEED_CRITIC_MATH", workdir=ws.root, call_dir=directory,
                target_contract_id="TARGET", input_refs={"TARGET_ANCHOR": target, "SEED": seed},
                prompt_seed="Review this exact local question.", round_index=1)
    checked = runner.run(**args)
    assert checked.payload["action"] == "ACCEPT"
    assert runner.run(**args) == checked
    assert len(transport.calls) == 1
    assert (directory / "completed_phase.json").is_file()
