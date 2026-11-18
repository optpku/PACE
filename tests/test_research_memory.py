"""Research authority, committed replay and independent application checks."""
import json
from pathlib import Path

import pytest

from pace.contracts.research.director_protocol import parse_director_decision
from pace.contracts.research.memory import binding, digest, prepare_delta, public_file
from pace.orchestrator.director_led import DirectorLedEngine
from pace.orchestrator.finding_publication import publish_applications
from pace.runtime.output.events import read_events
from pace.runtime.output.memory import (initialize_research_memory, commit_research_memory,
    rebuild_research_index, research_brief, refresh_application_memory)
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.prompt_composer import PromptComposerResult
from pace.runtime.research_context import freeze_context, load_index
from pace.runtime.semantic import StructuredSemanticResult


@pytest.fixture
def ws(tmp_path):
    ws = RunWorkspace.create(tmp_path, 'memory')
    ws.write_text(ws.work_ref('input/problem.md'), 'Prove the requested uniform bound.')
    ws.write_text(ws.work_ref('input/request.md'), 'Keep all hypotheses.')
    ws.write_json(ws.work_ref('input/target_contract.json'), {'contract_id':'TARGET','literal_target':'Uniform bound'})
    ws.write_json(ws.work_ref('state/research_execution_policy.json'), {'research_memory_version':1})
    ws.write_json(ws.work_ref('state/proof_loop_budget.json'), {'max_total_proof_states':3})
    from pace.runtime.prompts import materialize_run_resources
    materialize_run_resources(ws.root, resource_variant="standard")
    initialize_research_memory(ws.root)
    return ws


def task(agent='research', **kw):
    return parse_director_decision({'action':'DISPATCH','agent_id':agent,'prompt_brief':'Investigate the fixed gap.',
        'input_refs':[], 'obligation_ids':['target'],'branch_id':'b-target','archive_refs':[],
        'next_step_kind':'INITIAL_PLAN' if agent=='planner' else 'CONTINUE','change_refs':[],
        **({'research_operation':'NEW_PROBLEM'} if agent=='smi' else {}), **kw})


def deliver(ws, number, *, application=False, agent='research'):
    folder=ws.work_ref(f'round{number}')
    output=f'{folder}/research.md'; result_ref=f'{folder}/agent_result.json'
    assignment=f'{folder}/assignment/task.json'
    freeze_context(ws.root, assignment, task(agent), [ws.work_ref('input/problem.md')])
    ws.write_json(assignment, {'round_index':number})
    ws.write_text(output, 'One complete original argument, with its hypotheses and missing bridge.')
    if agent=='planner':
        ws.write_json(f'{folder}/route_relations.json', {'schema_version':'route_relations/v1', 'change_ref':output,'obligations':[]})
    else:
        ws.write_json(f'{folder}/research_delta.json', {'schema_version':'research_delta/v1', 'branch_id':'b-target',
            'obligation_ids':['target'],'change_ref':output, 'applications':[
                {'id':'application','obligation_id':'target','argument_ref':output,'premise_refs':[]}] if application else [],
            'attempt':{'outcome':'APPLICATION_PROPOSED' if application else 'NOT_ESTABLISHED',
                       'obstacle_ref':'','reopen_condition_ref':output}})
    ws.write_json(result_ref, {'agent_id':agent,'status':'SUCCEEDED','deliverable_ref':output})
    state_path=ws.path(ws.work_ref('state/director_state.json'))
    state=json.loads(state_path.read_text()) if state_path.is_file() else {'completed_result_refs':[]}
    state['completed_result_refs'].append(result_ref)
    ws.write_json(ws.work_ref('state/director_state.json'),state)
    return result_ref


def test_author_proposal_never_closes_obligation_and_replay_is_idempotent(ws):
    ref=deliver(ws,1,application=True)
    commit_research_memory(ws.root,[ref])
    before=ws.path('memory/research_index.json').read_bytes()
    commit_research_memory(ws.root,[ref])
    assert ws.path('memory/research_index.json').read_bytes()==before
    assert load_index(ws.root)['obligations']['target']['status']=='OPEN'
    assert len(load_index(ws.root)['applications'])==1
    assert len([e for e in read_events(ws.root) if e['event_kind']=='RESEARCH_MEMORY_COMMITTED'])==1


def test_missing_index_rebuilds_and_missing_event_is_repaired(ws):
    ref=deliver(ws,1); commit_research_memory(ws.root,[ref])
    expected=load_index(ws.root)['attempts']
    ws.path('memory/research_index.json').unlink()
    assert rebuild_research_index(ws.root)['attempts']==expected
    event=ws.path(ws.work_ref('events.jsonl')); event.write_text('')
    commit_research_memory(ws.root,[ref]); commit_research_memory(ws.root,[ref])
    assert len(read_events(ws.root))==1


@pytest.mark.parametrize('verdict,status', [('PASS','APPLICABLE'),('MATH_FAIL','NOT_ESTABLISHED')])
def test_application_acceptance_is_independent_of_publication(ws, monkeypatch, verdict, status):
    ref=deliver(ws,1,application=True);commit_research_memory(ws.root,[ref])
    calls=[]
    def verify(**kw):
        calls.append(kw)
        r=kw['application_request'];ws.write_text(r['review_ref'],'Independent scope and implication review.')
        return StructuredSemanticResult({'stage_status':verdict,'reuse_authorized':verdict=='PASS',
            'publication':{'action':'NONE'},'application_checks':[{'obligation_id':r['obligation_id'],
            'obligation_version':r['obligation_version'],'status':status,'review_ref':r['review_ref']}]},{})
    monkeypatch.setattr('pace.orchestrator.finding_publication.verify_finding',verify)
    assert publish_applications(llm=object(),root=ws.root)
    assert publish_applications(llm=object(),root=ws.root)
    assert len(calls)==1
    assert next(iter(load_index(ws.root)['applications'].values()))['status']==status
    assert not ws.path('work/state/findings.json').exists()
    assert rebuild_research_index(ws.root)['obligations']['target']['status']==('CLOSED' if verdict=='PASS' else 'OPEN')
    ws.write_text(ws.work_ref('round1/research.md'),'Changed argument')
    refresh_application_memory(ws.root)
    assert next(iter(load_index(ws.root)['applications'].values()))['status']=='INVALIDATED'
    assert load_index(ws.root)['obligations']['target']['status']=='OPEN'


def test_unrelated_archive_does_not_invalidate_and_no_false_progress(ws):
    refs=[]
    for n in range(1,4):
        refs.append(deliver(ws,n));commit_research_memory(ws.root,refs)
    ws.write_text('results/unrelated.md','Unrelated result')
    assert '"reassessment_due": ["target"]' in research_brief(ws.root)
    assert all(a['attempt']['outcome']=='NOT_ESTABLISHED' for a in load_index(ws.root)['attempts'])
    assert load_index(ws.root)['obligations']['target']['status']=='OPEN'


def test_parent_state_is_required_for_memory_and_route(ws):
    ref=deliver(ws,1,agent='planner')
    ws.write_json(ws.work_ref('state/director_state.json'),{'completed_result_refs':[]})
    commit_research_memory(ws.root,[ref])
    assert not ws.path(ws.work_ref('state/current_route.json')).exists()
    ws.write_json(ws.work_ref('state/director_state.json'),{'completed_result_refs':[ref]})
    commit_research_memory(ws.root,[ref])
    assert json.loads(ws.path(ws.work_ref('state/current_route.json')).read_text())['committed_result_ref']==ref


def test_resume_after_author_commit_does_not_repeat_author(ws):
    calls=[]
    def author(**kw):
        calls.append(kw['round_index']);folder=Path(kw['output_ref']).parent
        ws.write_text(kw['output_ref'],'Complete route and bounded next task.')
        ws.write_json(str(folder/'route_relations.json'), {'schema_version':'route_relations/v1','change_ref':kw['output_ref'],'obligations':[]})
        result={'agent_id':'planner','status':'SUCCEEDED','deliverable_ref':kw['output_ref']}
        kw['on_accept'](result)
        raise KeyboardInterrupt()
    engine=DirectorLedEngine(workdir=ws.root,director=lambda **_:task('planner'),
        composer=lambda **_:PromptComposerResult('Fixed instructions',''),specialists={'planner':author},
        target_contract_id='TARGET',max_rounds=1)
    with pytest.raises(KeyboardInterrupt):engine.run()
    assert not ws.path(ws.work_ref('state/current_route.json')).exists()
    assert engine.resume().phase=='FINALIZE'
    assert calls==[1]
    assert engine.resume().phase=='FINALIZE'
    assert calls==[1]
    assert not ws.path('work/turn002').exists()


@pytest.mark.parametrize('ref',['../escape.md','/tmp/x','work/turn001/round1/calls/x.md','private_data.md','x/../y'])
def test_memory_rejects_private_or_escaping_sources(ws,ref):
    with pytest.raises(ValueError):public_file(ws.root,ref)


def test_memory_rejects_symlink(ws,tmp_path):
    outside=tmp_path/'outside.md';outside.write_text('secret')
    ws.path('escape.md').symlink_to(outside)
    with pytest.raises(ValueError):public_file(ws.root,'escape.md')


def test_interrupted_handoff_repair_reuses_original_author(ws):
    from pace.orchestrator.director_led_adapters import run_default_specialist
    from test_finding_publication import ProofPublicationTransport
    from test_research_round_limit import write_delta
    output = ws.work_ref('round1/research.md')
    assignment = ws.work_ref('round1/assignment/task.json')
    context = freeze_context(ws.root, assignment, task(), [ws.work_ref('input/problem.md')])
    ws.write_json(assignment, {'round_index': 1})
    class Writer(ProofPublicationTransport):
        def __init__(self): super().__init__('NONE'); self.authors=0; self.repairs=0
        def complete_unstructured_result(self,prompt,*,workdir,call_name,ledger_context):
            if call_name.endswith('_handoff_correction'):
                self.repairs+=1
                if self.repairs==1: raise KeyboardInterrupt()
                write_delta(ws.root,ws.path(output))
            else:
                self.authors+=1
                ws.write_text(output,'A complete original argument; the remaining bridge is explicit.')
            return self._reply('Delivered',workdir=workdir,call_name=call_name,ledger_context=ledger_context,structured=False)
    llm=Writer()
    kwargs=dict(llm=llm,workdir=ws.root,prompt='Deliver the original argument.',agent_id='research',
        input_refs=tuple(context['required_refs']),output_ref=output,round_index=1,context={'target_contract_id':'TARGET'})
    with pytest.raises(KeyboardInterrupt):run_default_specialist(**kwargs)
    before=ws.path(output).read_bytes()
    assert ws.path(str(Path(output).with_name('author_delivery.json'))).is_file()
    result=run_default_specialist(**kwargs)
    assert result['status']=='SUCCEEDED' and llm.authors==1 and llm.repairs==2
    assert ws.path(output).read_bytes()==before


def test_navigation_snapshot_cannot_become_a_mathematical_premise(ws):
    ref = deliver(ws, 1, application=True)
    folder = Path(ref).parent
    snapshot = str(folder / 'assignment/snapshot/memory/summary.md')
    ws.write_text(snapshot, 'A summary that asserts the result without its proof.')
    delta_ref = str(folder / 'research_delta.json')
    delta = json.loads(ws.path(delta_ref).read_text())
    delta['applications'][0]['premise_refs'] = [snapshot]
    ws.write_json(delta_ref, delta)
    with pytest.raises(ValueError, match='immutable manuscripts'):
        prepare_delta(ws.root, delta_ref, load_index(ws.root)['obligations'])


def test_route_reports_all_bad_reference_fields_at_once(ws):
    from pace.contracts.research.memory import prepare_route
    route=ws.work_ref('round1/route_relations.json')
    ws.write_text(ws.work_ref('round1/route.md'),'Route with the next task.')
    ws.write_json(route,{'schema_version':'route_relations/v1','change_ref':ws.work_ref('round1/route.md')+' §1',
        'obligations':[{'id':'local','kind':'ROUTE_SUFFICIENT','statement_ref':'missing-statement.md',
            'scope_ref':'missing-scope.md','parent_ids':['target'],'branch_id':'branch','premise_groups':[]}]})
    with pytest.raises(ValueError) as exc: prepare_route(ws.root,route,{})
    assert all(pointer in str(exc.value) for pointer in ['/change_ref','/obligations/0/statement_ref','/obligations/0/scope_ref'])
