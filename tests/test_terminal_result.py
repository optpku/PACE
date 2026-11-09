"""Exercise mathematical completion without any Web presentation process."""
import json
from pathlib import Path

from pace import cli
from pace.orchestrator.pipeline import ProofPipeline
from pace.runtime.llm import TransportSuccess
from pace.runtime.output.calls import attempt_directory
from pace.runtime.output.events import read_events


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
            (root / 'work/turn001/input/problem_brief.md').write_text('# Classification\n\nThe original classification remains open.\n')
            payload = {'proof_assessment': {'difficulty': 'research', 'plan_node_level': 1, 'dependency_depth_level': 1}}
        elif call_name.startswith('global_research_director_'):
            payload = {'action': 'STOP'}
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
    monkeypatch.setattr('pace.orchestrator.pipeline.build_llm', lambda **settings: transport)
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
