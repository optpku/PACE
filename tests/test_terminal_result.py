"""Exercise mathematical completion without any Web presentation process."""
import json
from pathlib import Path

import pytest

from pace import cli
from pace.pipeline import ProofPipeline
from pace.artifacts.workspace import work_ref
from pace.runtime.llm import TransportSuccess
from pace.artifacts.calls import attempt_directory
from pace.artifacts.events import read_events


@pytest.mark.parametrize("damage", ["missing", "empty", "outside", "oversized"])
def test_terminal_delivery_checks_the_referenced_proof_file(tmp_path, damage):
    from pace.reporting.terminal_report import MAX_RESULT_BYTES, validate_terminal_artifact

    root = tmp_path / "run"
    root.mkdir()
    report = {"schema_version": "final_report/v8", "run_status": "completed", "result": "PASS",
              "artifact_refs": {"proof": "proof.md"}}
    if damage == "outside":
        (tmp_path / "outside.md").write_text("Proof outside this run.")
        report["artifact_refs"]["proof"] = "../outside.md"
    elif damage != "missing":
        (root / "proof.md").write_bytes(b"P" * (MAX_RESULT_BYTES + 1) if damage == "oversized" else b"")
    with pytest.raises(ValueError):
        validate_terminal_artifact(root, report)


class ResearchTransport:
    def __init__(self):
        self.calls = []

    def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
        root = Path(workdir)
        self.calls.append(call_name)
        if call_name == 'problem_orientation_extract':
            payload = {'target_clauses': [{'start_line': 1, 'end_line': 1}],
                       'recommended_mode': 'research', 'mode_reason': 'The requested classification needs research.'}
        elif call_name == 'problem_research_decision':
            (root / work_ref(root, 'input/problem_brief.md')).write_text('# Classification\n\nThe original classification remains open.\n')
            payload = {'proof_assessment': {'difficulty': 'research', 'plan_node_level': 1, 'dependency_depth_level': 1}}
        elif call_name.startswith('global_research_director_'):
            payload = {'action': 'STOP', 'stop_reason': 'NO_ACTIONABLE_TASK',
                       'rationale': 'The stated axioms omit the operation needed to define the classification.'}
        else:
            raise AssertionError(call_name)
        folder = attempt_directory(root, ledger_context['call_ref'], 1)
        response = json.dumps(payload)
        (folder / 'response.txt').write_text(response)
        return TransportSuccess(text=response, invocation_id=f'research-{len(self.calls)}',
            call_name=call_name, exit_code=0, terminal_event_type='turn.completed',
            accepted_output_source='file', output_schema_valid=True, attempt_count=1, artifact_dir=folder)


def test_research_terminal_and_resume_use_only_the_final_report(tmp_path, monkeypatch, capsys):
    transport = ResearchTransport()
    monkeypatch.setattr('pace.agents.director.harness.build_llm', lambda **settings: transport)
    source = tmp_path / 'problem.md'
    source.write_text('Classify all structures satisfying the stated axioms.\n')
    assert cli.main(['run', '--problem', str(source), '--problem-id', 'research', '--output', str(tmp_path), '--json']) == 0
    root = tmp_path / 'research'
    final = root / 'work/turn001/final_report.json'
    report = json.loads(final.read_text())
    assert report['execution_mode'] == 'research'
    assert report['mode_source'] == 'ORIENTATION'
    assert report['mathematical_status'] == 'OPEN'
    assert report['run_status'] == 'completed'
    assert 'report' not in report['artifact_refs']
    assert not (root / 'work/turn001/outcome.md').exists()
    assert not (root / 'results/current_report.md').exists()
    before = final.read_bytes(), len(transport.calls)
    assert cli.main(['resume', '--run-dir', str(root), '--json']) == 0
    assert (final.read_bytes(), len(transport.calls)) == before
    assert all('user_update' not in event for event in read_events(root))


def test_terminal_evidence_pause_resumes_without_a_director_call(tmp_path, monkeypatch):
    from pace.agents.director.findings import check_stage_findings
    from pace.artifacts.delivery_pause import delivery_pause

    transport = ResearchTransport()
    retries = []

    def review(**kwargs):
        retries.append(kwargs["retry_errors"])
        if len(retries) == 1:
            raise OSError("interrupted publication")
        return check_stage_findings(**kwargs)

    monkeypatch.setattr("pace.agents.director.harness.build_llm", lambda **_: transport)
    monkeypatch.setattr("pace.agents.director.harness.check_stage_findings", review)
    source = tmp_path / "problem.md"
    source.write_text("Classify all structures satisfying the stated axioms.")
    cli.main(["run", "--problem", str(source), "--problem-id", "research", "--output", str(tmp_path)])
    root = tmp_path / "research"
    turn = root / "work/turn001"
    assert delivery_pause(root)["stage"] == "findings"
    assert json.loads((turn / "state/director_state.json").read_text())["phase"] == "STOPPED"
    assert json.loads((turn / "final_report.json").read_text())["run_status"] == "completed_with_operational_error"
    calls = list(transport.calls)
    with monkeypatch.context() as scoped:
        def interrupt(**kwargs):
            assert not delivery_pause(root)
            raise KeyboardInterrupt
        scoped.setattr("pace.agents.director.workflow.finalize_director_led", interrupt)
        assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 130
    assert not delivery_pause(root)
    for _ in range(2):
        assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert retries == [False, True, True]
    assert not delivery_pause(root)
    assert json.loads((turn / "final_report.json").read_text())["run_status"] == "completed"
    assert transport.calls == calls
