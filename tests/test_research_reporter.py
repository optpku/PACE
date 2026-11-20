"""Exercise real handoff boundaries, file tools, and recovery without live inference."""
import json
from pathlib import Path

import pytest

from pace import cli
from pace.contracts.research.memory import delivery_instruction, reporter_owned
from pace.reporting.research_progress import run_research_reporter, restore_reporter_delivery
from pace.runtime.chat_api import ChatCompletionsLLM
from pace.runtime.output.workspace import work_ref
from pace.runtime.research_context import load_index
from test_research_memory import ws, deliver, task
from test_finding_publication import ProofPublicationTransport


def use_reporter(ws):
    ws.write_json(ws.work_ref('state/research_execution_policy.json'),
                  {'research_memory_version': 1, 'research_delta_owner': 'reporter'})


@pytest.mark.parametrize('agent', ['research', 'prover', 'rci'])
def test_reporter_replay_never_calls_author_or_reporter_again(ws, agent, monkeypatch):
    use_reporter(ws)
    ref = deliver(ws, 1, agent=agent)
    folder = Path(ref).parent
    ws.path(str(folder / 'research_delta.json')).unlink()
    llm = ProofPublicationTransport('NONE')
    original = ws.path(str(folder / 'research.md')).read_bytes()
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert llm.calls == ['research_reporter_round1']
    for name in ('progress.md', 'research_delta.json'):
        ws.path(str(folder / name)).unlink()
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert llm.calls == ['research_reporter_round1']
    assert ws.path(str(folder / 'research.md')).read_bytes() == original
    from pace.runtime.output.memory import commit_research_memory
    commit_research_memory(ws.root, [ref]); commit_research_memory(ws.root, [ref])
    assert len(load_index(ws.root)['attempts']) == 1
    assert load_index(ws.root)['obligations']['target']['status'] == 'OPEN'
    assert 'Do not write a delta' in delivery_instruction(ws.root, str(folder / 'research.md'), agent)


def test_reporter_acceptance_survives_projection_failure(ws, monkeypatch):
    use_reporter(ws); ref = deliver(ws, 1)
    llm = ProofPublicationTransport('NONE')
    import pace.reporting.research_progress as module
    original = module._write_text_atomic
    monkeypatch.setattr(module, '_write_text_atomic', lambda *_: (_ for _ in ()).throw(OSError('disk interruption')))
    with pytest.raises(OSError): run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    monkeypatch.setattr(module, '_write_text_atomic', original)
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert llm.calls == ['research_reporter_round1']


def test_changed_source_pauses_reporter_replay(ws):
    use_reporter(ws); ref = deliver(ws, 1)
    llm = ProofPublicationTransport('NONE')
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    ws.path(str(Path(ref).parent / 'research.md')).write_text('Different mathematics')
    with pytest.raises(ValueError, match='version changed'):
        run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert len(llm.calls) == 1


class NativeFixture(ChatCompletionsLLM):
    """Only HTTP is substituted. Tool execution and all semantic validators are real."""
    def __init__(self, mode, *, interrupt_reporter=False):
        super().__init__(model='fixture', web_search=True)
        self.mode = mode; self.calls = []; self.context = {}; self.interrupt_reporter = interrupt_reporter
        self.correct_director = False

    def _complete_transport_owned(self, prompt, **kwargs):
        self.context = kwargs['ledger_context']; self.root = Path(kwargs['workdir'])
        self.calls.append(kwargs['call_name'])
        if kwargs['call_name'].startswith('research_reporter_') and self.interrupt_reporter:
            self.interrupt_reporter = False
            raise KeyboardInterrupt
        return super()._complete_transport_owned(prompt, **kwargs)

    def _request(self, messages, tools):
        ctx = self.context; name = self.calls[-1]; root = self.root
        available = {t['function']['name'] for t in tools}
        role = ctx['role'].upper()
        assert ('fetch_url' in available) == (role == 'SURVEY')
        if 'receipt_correction' in name:
            assert 'write_file' not in available
            assert '## JSON receipt correction' in messages[1]['content'] or '## Director receipt correction' in messages[1]['content']
            assert 'Remaining shared proof states' not in messages[1]['content']
        if messages[-1]['role'] == 'tool':
            assert all('error' not in json.loads(m['content']) for m in messages if m['role'] == 'tool')
        if name == 'problem_orientation_extract':
            payload = {'target_clauses':[{'start_line':1,'end_line':1}], 'recommended_mode':self.mode, 'mode_reason':'Fixture choice'}
        elif name == 'problem_research_decision':
            brief = work_ref(root, 'input/problem_brief.md')
            assert ctx['output_refs'] == [brief]
            if messages[-1]['role'] == 'user': return self.tool('write_file', {'path':brief,'content':'# Target\nProve 2+2=4.\n'})
            payload = {'brief_ref':brief} if self.mode == 'proof' else {
                'proof_assessment':{'difficulty':'research','plan_node_level':1,'dependency_depth_level':1}}
        elif name.startswith('global_research_director_'):
            number = int(name.split('_')[3])
            agent = {1:'survey',2:'research',3:'prover'}[number]
            payload = {'action':'DISPATCH','agent_id':agent,'prompt_brief':'Investigate the fixed equality.',
                'input_refs':[work_ref(root,'input/problem.md')], 'obligation_ids':['target'], 'branch_id':'b-target',
                'archive_refs':[], 'next_step_kind':'CONTINUE', 'change_refs':[]}
            if number == 1 and not name.endswith('_receipt_correction'):
                payload['next_step_kind'] = 'INITIAL_PLAN'  # Recorded Atria failure shape.
            elif number == 1:
                self.correct_director = True
        elif name.startswith('research_reporter_'):
            assert 'write_file' not in available
            request_ref = next(r for r in ctx['input_refs'] if r.endswith('/reporter_request.json'))
            request = json.loads((root/request_ref).read_text())
            payload = {'summary':'The handoff derives the fixed equality; independent verification is pending.',
                'delta':{'schema_version':'research_delta/v1','obligation_ids':request['obligation_ids'],
                    'branch_id':request['branch_id'],'change_ref':request['original_refs'][0],'applications':[],
                    'attempt':{'outcome':'LOCAL_RESULT','obstacle_ref':'','reopen_condition_ref':''}}}
        elif 'proof_contract_auditor' in name:
            payload = {'contract_status':'PASS','answer_mode':'POSITIVE_PROOF'}
        elif name.startswith('math_verifier_kernel_'):
            payload = {'stage_status':'PASS','publication_eligible':True,'publication':{'action':'NONE'},
                'relation_checks':[{'status':'PASS','statement':'2+2=4','justification':'Addition.'}],
                'target_clause_checks':[{'status':'PASS','evidence':'The equality is derived.'}],
                'final_assembly':{'derivation':'Integer addition gives the target.'}}
        elif name.startswith('director_led_'):
            forbidden = {'research_delta.json'} | ({'memory_note.md'} if role != 'SURVEY' else set())
            assert not any(Path(ref).name in forbidden for ref in ctx['output_refs'])
            if messages[-1]['role'] == 'user':
                output = ctx['output_refs'][0]
                return self.tool('write_file', {'path':output,'content':'By integer addition, 2+2=4.\n'})
            return {'choices':[{'message':{'role':'assistant','content':'Delivered.'},'finish_reason':'stop'}]}
        else: raise AssertionError(name)
        assert 'submit_result' in available
        return self.tool('submit_result', payload, prefix='This explanatory preamble is not the receipt.')

    @staticmethod
    def tool(name, arguments, prefix=None):
        return {'choices':[{'message':{'role':'assistant','content':prefix,'tool_calls':[
            {'id':'tool','type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}]},'finish_reason':'tool_calls'}]}


@pytest.mark.parametrize('mode', ['proof','research'])
@pytest.mark.parametrize('interrupted', [False,True])
def test_native_cli_initialization_reporter_and_resume(tmp_path, monkeypatch, mode, interrupted):
    transport = NativeFixture(mode, interrupt_reporter=interrupted)
    monkeypatch.setattr('pace.orchestrator.pipeline.build_llm', lambda **_: transport)
    problem = tmp_path/'problem.md'; problem.write_text('Prove 2+2=4.\n')
    args = ['run','--problem',str(problem),'--problem-id','native','--output',str(tmp_path),'--json']
    if interrupted:
        assert cli.main(args) == 130
        calls_before = list(transport.calls)
        assert cli.main(['resume','--run-dir',str(tmp_path/'native'),'--json']) == 0
        for name in calls_before:
            if not name.startswith('research_reporter_'):
                assert transport.calls.count(name) == 1
    else: assert cli.main(args) == 0
    root = tmp_path/'native'; turn=root/'work/turn001'
    report = json.loads((turn/'final_report.json').read_text())
    assert report['result'] == 'PASS'
    assert not (root/'work/turn002').exists()
    assert len(list(turn.glob('round*/reporter_result.json'))) == (1 if mode=='proof' else 2)
    assert (turn/'input/problem_brief.md').read_text().strip()
    assert not (turn/'orientation/brief_diagnostics.json').exists()
    if mode == 'research': assert transport.correct_director
    assert all(a['attempt']['outcome']=='LOCAL_RESULT' for a in load_index(root)['attempts'])
    completed_calls = list(transport.calls)
    # Completed runs also rebuild navigation/deltas without calling any model.
    originals = {p: p.read_bytes() for p in turn.glob('round*/research_delta.json')}
    for path in originals:
        path.unlink(); path.with_name('progress.md').unlink()
    (root/'memory/research_index.json').unlink()
    (root/'memory/README.md').unlink()
    assert cli.main(['resume','--run-dir',str(root),'--json']) == 0
    assert transport.calls == completed_calls
    assert all(p.read_bytes() == data and p.with_name('progress.md').is_file() for p,data in originals.items())
    assert len(load_index(root)['attempts']) == (1 if mode=='proof' else 2)


@pytest.mark.parametrize('parallel', [False, True])
def test_rci_outer_handoff_and_parallel_reporter_recover_without_author(ws, parallel):
    from pace.orchestrator.director_led import DirectorLedEngine
    from pace.runtime.prompt_composer import PromptComposerResult
    use_reporter(ws)
    author_calls = []; reporter_calls = []; interrupted = False
    llm = ProofPublicationTransport('NONE')
    first = task('rci').to_json()
    if parallel: first['parallel_tasks'] = [{k:v for k,v in task('research').to_json().items() if k != 'parallel_tasks'}]
    def author(**kw):
        author_calls.append(kw['agent_id'])
        ws.write_text(kw['output_ref'], 'The local mechanism remains incomplete; the original target is unchanged.')
        return {'agent_id':kw['agent_id'],'status':'SUCCEEDED','deliverable_ref':kw['output_ref']}
    def reporter(ref):
        nonlocal interrupted
        reporter_calls.append(ref)
        if not interrupted and 'round1/' in ref:
            interrupted = True
            raise OSError('Interruption after mathematical delivery')
        return run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: first,
        composer=lambda **_: PromptComposerResult(final_prompt='Work on the original question.', call_id='composer'),
        specialists={'rci':author,'research':author}, max_rounds=2 if parallel else 1, max_proof_states=2,
        reporter=reporter, target_contract_id='TARGET')
    result = engine.run()
    assert result.phase == 'FAILED'
    assert len(author_calls) == (2 if parallel else 1)
    from pace.orchestrator.recovery import _restore_operational_checkpoint
    assert _restore_operational_checkpoint(ws, result.state.to_json())
    engine.director = lambda **_: pytest.fail('Do not redispatch the reserved task')
    result = engine.resume()
    assert result.phase == 'FINALIZE'
    assert author_calls.count('rci') == 1
    assert author_calls.count('research') == (1 if parallel else 0)
    assert len(llm.calls) == (2 if parallel else 1)
    assert len(load_index(ws.root)['attempts']) == (2 if parallel else 1)


def test_native_director_repairs_empty_purpose_locally(ws):
    from pace.agents.research.global_director import run_global_director
    from pace.runtime.semantic import StructuredSemanticResult
    llm = NativeFixture('research')
    request = llm._request
    def blank_purpose(messages, tools):
        reply = request(messages, tools)
        if not llm.calls[-1].endswith('_receipt_correction'):
            function = reply['choices'][0]['message']['tool_calls'][0]['function']
            payload = json.loads(function['arguments']); payload['next_step_kind'] = ''
            function['arguments'] = json.dumps(payload)
        return reply
    llm._request = blank_purpose
    reply = run_global_director(llm=llm, workdir=ws.root, call_name='global_research_director_0001')
    assert isinstance(reply, StructuredSemanticResult)
    assert reply.payload['agent_id'] == 'survey' and reply.payload['next_step_kind'] == 'CONTINUE'
    assert len(llm.calls) == 2


@pytest.mark.parametrize(("agent", "purpose", "expected", "needs_correction"), [
    ("research", None, "CONTINUE", False),
    ("research", "", "CONTINUE", False),
    ("research", "APPLY_EXISTING", "APPLY_EXISTING", False),
    ("research", "REASSESS", "REASSESS", False),
    ("research", "INITIAL_PLAN", "CONTINUE", True),
    ("planner", None, "INITIAL_PLAN", True),
])
def test_native_director_parallel_purpose(ws, monkeypatch, agent, purpose, expected, needs_correction):
    from pace.agents.research.global_director import run_global_director
    from pace.runtime.semantic import StructuredSemanticResult

    primary = task("research" if agent == "planner" else "planner").to_json()
    companion = task(agent).to_json()
    companion.pop("parallel_tasks")
    if purpose is None:
        companion.pop("next_step_kind")
    else:
        companion["next_step_kind"] = purpose
    primary["parallel_tasks"] = [companion]
    requests = []
    transport = NativeFixture("research")

    def reply(messages, _tools):
        prompt = messages[1]["content"]
        requests.append(prompt)
        if len(requests) == 1:
            assert "including each entry in parallel_tasks" in prompt
            return transport.tool("submit_result", primary)
        assert needs_correction and "/parallel_tasks/0/next_step_kind" in prompt
        corrected = {**companion, "next_step_kind": expected}
        return transport.tool("submit_result", {**primary, "parallel_tasks": [corrected]})

    monkeypatch.setattr(transport, "_request", reply)
    result = run_global_director(llm=transport, workdir=ws.root)
    assert isinstance(result, StructuredSemanticResult)
    assert result.payload["parallel_tasks"] == [{**companion, "next_step_kind": expected}]
    assert result.payload["next_step_kind"] == primary["next_step_kind"]
    assert len(requests) == (2 if needs_correction else 1)
    if not needs_correction:
        assert json.loads(ws.path(result.host["response_ref"]).read_text()) == primary


def test_reporter_receipt_repair_cannot_write_mathematical_files(ws):
    use_reporter(ws); ref = deliver(ws, 1)
    llm = NativeFixture('research')
    request = llm._request
    def wrong_branch(messages, tools):
        reply = request(messages, tools)
        if not llm.calls[-1].endswith('_receipt_correction'):
            function = reply['choices'][0]['message']['tool_calls'][0]['function']
            payload = json.loads(function['arguments']); payload['delta']['branch_id'] = 'wrong-branch'
            function['arguments'] = json.dumps(payload)
        return reply
    llm._request = wrong_branch
    original = ws.path(str(Path(ref).parent/'research.md')).read_bytes()
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert llm.calls == ['research_reporter_round1', 'research_reporter_round1_receipt_correction']
    assert ws.path(str(Path(ref).parent/'research.md')).read_bytes() == original
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert len(llm.calls) == 2


def test_parallel_pass_does_not_drop_unfinished_reporter(ws, monkeypatch):
    from dataclasses import replace
    from pace.orchestrator.director_led import DirectorLedEngine
    from pace.orchestrator import director_parallel as module
    from pace.runtime.prompt_composer import PromptComposerResult
    use_reporter(ws)
    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: None,
        composer=lambda **_: PromptComposerResult(final_prompt='Task', call_id='fixture'),
        specialists={'prover':lambda **_:None,'research':lambda **_:None}, max_proof_states=2)
    initial = engine._new_state()
    decision = replace(task('prover'), parallel_tasks=(task('research'),))
    reserved = module.reserve_batch(engine, initial, decision)
    def completed_or_failed(parent, snapshot, task, index, first, context):
        return replace(snapshot, round_index=index, phase='FINALIZE' if index==1 else 'FAILED',
                       terminal_result='PASS' if index==1 else 'FAILED')
    monkeypatch.setattr(module, '_execute', completed_or_failed)
    result = module.run_batch(engine, reserved, control_context='')
    assert result.phase == 'FAILED'
    diagnostic = json.loads(ws.path(result.diagnostics_ref).read_text())
    assert diagnostic['resume_checkpoint']['phase'] == 'PENDING_BATCH'
    assert not load_index(ws.root)['commits']


def test_proof_reporter_does_not_create_unconsumed_application_queue(ws):
    use_reporter(ws)
    policy = json.loads(ws.path(ws.work_ref('state/research_execution_policy.json')).read_text())
    ws.write_json(ws.work_ref('state/research_execution_policy.json'), {**policy,'mode':'proof'})
    ref = deliver(ws,1); llm = NativeFixture('proof'); original_request = llm._request
    def propose_application(messages, tools):
        reply = original_request(messages, tools)
        if not llm.calls[-1].endswith('_receipt_correction'):
            function = reply['choices'][0]['message']['tool_calls'][0]['function']
            payload = json.loads(function['arguments'])
            payload['delta']['applications'] = [{'id':'unneeded-application','obligation_id':'target',
                'argument_ref':payload['delta']['change_ref'],'premise_refs':[]}]
            function['arguments'] = json.dumps(payload)
        return reply
    llm._request = propose_application
    result = run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    assert result['payload']['delta']['applications']==[]
    assert len(llm.calls)==2


def test_all_annotated_references_repaired_once_with_frozen_meaning(ws, monkeypatch):
    use_reporter(ws); ref = deliver(ws, 1)
    llm = NativeFixture('research'); original_request = llm._request
    draft = {}; calls = []
    def reply(messages, tools):
        calls.append(llm.calls[-1])
        if not llm.calls[-1].endswith('_receipt_correction'):
            response = original_request(messages, tools)
            function = response['choices'][0]['message']['tool_calls'][0]['function']
            payload = json.loads(function['arguments']); path = payload['delta']['change_ref']
            payload['delta']['applications'] = [{'id':'a1','obligation_id':'target',
                'argument_ref':path+' §4 (Lemma 1)', 'premise_refs':[path+' (dependency)']}]
            payload['delta']['change_ref'] += ' (original manuscript)'
            payload['delta']['attempt'].update(obstacle_ref=path+' §7', reopen_condition_ref=path+' §8')
            draft.update(payload)
            function['arguments'] = json.dumps(payload)
            return response
        prompt = messages[1]['content']
        for pointer in ['/delta/change_ref', '/delta/applications/0/argument_ref',
                        '/delta/applications/0/premise_refs/0', '/delta/attempt/obstacle_ref',
                        '/delta/attempt/reopen_condition_ref']:
            assert pointer in prompt
        assert llm.context['read_scope'] == 'declared'
        assert len(llm.context['input_refs']) == 2
        access = __import__('pace.runtime.chat_api', fromlist=['WorkspaceTools']).WorkspaceTools(ws.root, llm.context)
        with pytest.raises(ValueError): access.execute('read_file', {'path':str(Path(ref).parent/'research.md')})
        payload=json.loads(json.dumps(draft)); delta=payload['delta']; path=str(Path(ref).parent/'research.md')
        delta['change_ref']=path; delta['attempt'].update(obstacle_ref=path,reopen_condition_ref=path)
        delta['applications'][0].update(argument_ref=path,premise_refs=[path])
        return NativeFixture.tool('submit_result', payload)
    monkeypatch.setattr(llm, '_request', reply)
    saved=run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert len(calls)==2 and saved['payload']['summary']==draft['summary']
    assert saved['payload']['delta']['attempt']['outcome']=='LOCAL_RESULT'
    assert not load_index(ws.root)['applications']  # A receipt is not independent acceptance.
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert len(calls)==2


def test_resume_reporter_uses_saved_failed_receipt_instead_of_new_summary(ws, monkeypatch):
    use_reporter(ws); ref=deliver(ws,1); llm=NativeFixture('research'); original=llm._request
    def wrong_path(messages, tools):
        if llm.calls[-1].endswith('_receipt_correction'): raise KeyboardInterrupt
        response=original(messages,tools)
        f=response['choices'][0]['message']['tool_calls'][0]['function']
        p=json.loads(f['arguments']);p['delta']['change_ref']+=' §1';f['arguments']=json.dumps(p)
        return response
    monkeypatch.setattr(llm,'_request',wrong_path)
    with pytest.raises(KeyboardInterrupt): run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    monkeypatch.setattr(llm,'_request',original)
    run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    assert llm.calls.count('research_reporter_round1')==1
    assert llm.calls.count('research_reporter_round1_receipt_correction')==2


def test_rejected_content_change_cannot_be_accepted_by_resume(ws, monkeypatch):
    use_reporter(ws); ref=deliver(ws,1); llm=NativeFixture('research'); original=llm._request
    def altered(messages, tools):
        response=original(messages,tools); f=response['choices'][0]['message']['tool_calls'][0]['function']
        p=json.loads(f['arguments'])
        if llm.calls[-1].endswith('_receipt_correction'):
            p['summary']='A stronger theorem is now verified.'
        else:p['delta']['change_ref']+=' §1'
        f['arguments']=json.dumps(p);return response
    monkeypatch.setattr(llm,'_request',altered)
    with pytest.raises(RuntimeError,match='reporter_repair_changed_content'):
        run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    assert not ws.path(str(Path(ref).parent/'reporter_result.json')).exists()
    monkeypatch.setattr(llm,'_request',original)
    result=run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    assert result['payload']['summary']=='The handoff derives the fixed equality; independent verification is pending.'
    assert llm.calls==['research_reporter_round1','research_reporter_round1_receipt_correction','research_reporter_round1_receipt_correction']


def test_json_only_rci_handoff_schema_is_valid_and_disallows_applications():
    from pace.reporting.research_progress import reporter_schema
    from pace.runtime.schema import preflight_output_schema,validate_json_instance
    schema=reporter_schema({'original_refs':['outcome.json'],'bindings':[{'ref':'outcome.json'}]})
    assert preflight_output_schema(schema).ok
    assert schema['properties']['delta']['properties']['applications']['maxItems']==0
