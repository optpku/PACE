"""Compatible API tools enforce actual access boundaries, without network calls."""
import json
from pathlib import Path

import pytest

from pace.runtime.chat_api import ChatCompletionsLLM, WorkspaceTools
from pace.artifacts.calls import begin_call
from pace.artifacts.workspace import RunWorkspace
from pace.runtime.llm import RETRY_TRANSIENT, TransportSuccess


def test_native_transport_writes_only_assigned_artifact(tmp_path,monkeypatch):
    ws=RunWorkspace.create(tmp_path,'api');ref=ws.work_ref('round1/proof.md')
    from pace.runtime.prompts import materialize_run_resources
    from pace.agents.director.harness import prepare_agent_environments
    materialize_run_resources(ws.root, resource_variant='standard')
    prepare_agent_environments(ws.root, mode='proof')
    ws.write_text(ws.work_ref('input/problem.md'),'Prove 2+2=4.')
    call=begin_call(ws.root,call_id='api',role='WHOLE_PROVER',stage='proof',prompt='write the proof')
    outputs=[{'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'write','type':'function','function':{
        'name':'write_file','arguments':json.dumps({'path':ref,'content':'By addition 2+2=4.'})}}]},'finish_reason':'tool_calls'}]},
        {'choices':[{'message':{'role':'assistant','content':'Done'},'finish_reason':'stop'}]}]
    llm=ChatCompletionsLLM(model='test',base_url='https://example.invalid/v1')
    monkeypatch.setattr(llm,'_request',lambda messages,tools: outputs.pop(0))
    result=llm.complete_unstructured_result('write the proof',workdir=ws.root,call_name='proof',
        ledger_context={'call_id':'api','call_ref':call,'role':'WHOLE_PROVER','output_refs':[ref]})
    assert isinstance(result,TransportSuccess)
    assert ws.path(ref).read_text()=='By addition 2+2=4.'
    assert not outputs


def test_blind_transport_cannot_read_sibling_or_memory_or_fetch(tmp_path):
    root=RunWorkspace.create(tmp_path,'blind').root; (root/'probe.md').write_text('Self contained question')
    (root/'secret.md').write_text('Secret mechanism')
    tools=WorkspaceTools(root,{'role':'rci_blind_prover_probe','input_refs':['probe.md'],'output_refs':['answer.md']},web_search=True)
    assert tools.execute('read_file',{'path':'probe.md'})['content']=='Self contained question'
    for name,args in [('read_file',{'path':'secret.md'}),('write_file',{'path':'probe.md','content':'changed'}),
                      ('write_file',{'path':'../escape','content':'escape'}),('fetch_url',{'url':'https://example.com'})]:
        with pytest.raises(ValueError):tools.execute(name,args)
    assert tools.execute('list_files',{'path':'.'})['files']==['probe.md']


def test_only_survey_gets_network_tools(tmp_path):
    tmp_path=RunWorkspace.create(tmp_path,'tools').root
    for role in ('WHOLE_PROVER','PLANNER','RESEARCH','global_research_director','finding_verifier','RCI'):
        assert not WorkspaceTools(tmp_path,{'role':role},web_search=True).web
    assert WorkspaceTools(tmp_path,{'role':'SURVEY'},web_search=True).web
    assert not WorkspaceTools(tmp_path,{'role':'SURVEY'},web_search=False).web


@pytest.mark.parametrize('phase,action', [('BLIND_PROVER_PROBE','NO_FIRST_STEP'),
    ('REFUTE_PROVER_PROBE','NO_COUNTEREXAMPLE'), ('FRONTIER_REFUTE_PROBE','NO_COUNTEREXAMPLE')])
def test_actual_blind_phase_has_no_memory_and_resumes_without_model(ws,monkeypatch,phase,action):
    from pace.agents.rci.phase_runner import RciPhaseRunner
    from pace.agents.director.context import freeze_context
    from test_research_memory import task
    freeze_context(ws.root,ws.work_ref('round1/assignment/task.json'),task(),[ws.work_ref('input/problem.md')])
    ws.write_text('probe.md','Self-contained problem with permitted assumptions.')
    ws.write_text('memory/secret.md','PRIVATE_METHOD_SENTINEL')
    directory=ws.path(ws.work_ref('round1/rci/attempt_001/candidate_01/'+phase.lower()))
    review=str(directory.relative_to(ws.root)/'review.md'); requests=[]
    llm=ChatCompletionsLLM(model='fixture',base_url='https://example.invalid/v1',web_search=False)
    def respond(messages,tools):
        requests.append(messages[:])
        assert 'PRIVATE_METHOD_SENTINEL' not in json.dumps(messages)
        assert 'secret.md' not in json.dumps(messages)
        assert not any(t['function']['name']=='fetch_url' for t in tools)
        if len(requests)==1:
            msg={'role':'assistant','content':None,'tool_calls':[{'id':'write','type':'function','function':{
                'name':'write_file','arguments':json.dumps({'path':review,'content':'No complete proof found; this is not a refutation.'})}}]}
        else: msg={'role':'assistant','content':json.dumps({'action':action,'dependency_refs':[]})}
        return {'choices':[{'message':msg,'finish_reason':'tool_calls' if len(requests)==1 else 'stop'}]}
    monkeypatch.setattr(llm,'_request',respond)
    runner=RciPhaseRunner(llm=llm)
    kwargs=dict(phase=phase,workdir=ws.root,call_dir=directory,target_contract_id='TARGET',
        input_refs={'PROBE_TASK':['probe.md'],'MECHANISM':['memory/secret.md']},prompt_seed='Check the fixed problem.',round_index=1)
    result=runner.run(**kwargs)
    assert result.payload['action']==action and len(requests)==2
    assert runner.run(**kwargs)==result and len(requests)==2


from test_research_memory import ws


@pytest.mark.parametrize('separate_reporter', [False, True])
def test_explorer_delta_and_originals_are_reused_together(ws, monkeypatch, separate_reporter):
    from pace.agents.rci.phase_runner import RciPhaseRunner
    from pace.agents.director.context import freeze_context
    from test_research_memory import task
    if separate_reporter:
        ws.write_json(ws.work_ref('state/research_execution_policy.json'),
                      {'research_memory_version': 1, 'research_delta_owner': 'reporter'})
    freeze_context(ws.root, ws.work_ref('round1/assignment/task.json'), task(), [ws.work_ref('input/problem.md')])
    ws.write_json(ws.work_ref('round1/assignment/task.json'), {'round_index': 1})
    ws.write_text('seed.md', 'A fixed self-contained local mathematical question.')
    directory=ws.path(ws.work_ref('round1/rci/attempt_001/candidate_01/frontier_explorer'))
    change=str(directory.relative_to(ws.root)/'change.md')
    delta={'schema_version':'research_delta/v1','obligation_ids':['target'],'branch_id':'b-target',
        'change_ref':change,'applications':[],'attempt':{'outcome':'NOT_ESTABLISHED','obstacle_ref':change,'reopen_condition_ref':change}}
    calls=[]; llm=ChatCompletionsLLM(model='fixture')
    def respond(messages,tools):
        calls.append(messages[:])
        if len(calls)==1:
            files={'change.md':'The missing connection and a concrete next test.', 'mechanism.md':'A proposed route, not a proved lemma.',
                   'research_delta.json':json.dumps(delta)}
            if separate_reporter:
                del files['research_delta.json']
            message={'role':'assistant','content':None,'tool_calls':[{'id':name,'type':'function','function':{
                'name':'write_file','arguments':json.dumps({'path':str(directory.relative_to(ws.root)/name),'content':content})}}
                for name,content in files.items()]}
        else: message={'role':'assistant','content':json.dumps({'action':'PROPOSE_MECHANISM','candidate_ref':'seed.md','dependency_refs':[]})}
        return {'choices':[{'message':message,'finish_reason':'tool_calls' if len(calls)==1 else 'stop'}]}
    monkeypatch.setattr(llm,'_request',respond)
    runner=RciPhaseRunner(llm=llm)
    kwargs=dict(phase='FRONTIER_EXPLORER',workdir=ws.root,call_dir=directory,target_contract_id='TARGET',
        input_refs={'TARGET_ANCHOR':[ws.work_ref('input/problem.md')],'REVIEWED_SEED':['seed.md']},
        prompt_seed='Investigate this fixed gap.',round_index=1)
    result=runner.run(**kwargs)
    if separate_reporter:
        assert 'research_delta_ref' not in result.payload
        assert not (directory / 'research_delta.json').exists()
    else:
        assert result.payload['research_delta_ref'].endswith('/research_delta.json')
    assert runner.run(**kwargs)==result and len(calls)==2


def test_kernel_native_writable_paths_match_prompt_without_host_receipt(ws, monkeypatch):
    from pace.agents.kernel import run_math_verifier_kernel
    from test_research_reporter import NativeFixture
    proof = ws.work_ref('round1/proof.md'); ws.write_text(proof,'A candidate requiring independent verification.')
    note = ws.work_ref('round1/verification/memory_note.md')
    receipt = ws.work_ref('round1/verification/math_verifier_kernel.json')
    llm = ChatCompletionsLLM(model='fixture')
    def reply(messages, tools):
        if messages[-1]['role']=='user':
            system = messages[0]['content']
            assert note in system and receipt not in system
            return NativeFixture.tool('write_file', {'path':note,'content':'The target still needs a connecting argument.'})
        assert 'error' not in json.loads(messages[-1]['content'])
        return NativeFixture.tool('submit_result', {'stage_status':'MATH_FAIL','gap':'Missing connection.'})
    monkeypatch.setattr(llm, '_request', reply)
    result = run_math_verifier_kernel(proof_id='P1',proof_ref=proof,target_contract_id='TARGET',
        relation_llm=llm,workdir=ws.root,artifact_refs={'proof_file':proof},output_ref=receipt)
    assert result['stage_status']=='MATH_FAIL' and ws.path(note).is_file()
    assert not ws.path(receipt).exists()


def test_director_can_follow_host_dossier_and_committed_evidence(ws, monkeypatch):
    from pace.agents.director.agent import run_global_director
    from pace.runtime.semantic import StructuredSemanticResult
    from pace.orchestrator.memory import commit_research_memory
    from test_research_memory import deliver
    from test_research_reporter import NativeFixture
    orientation = ws.work_ref('orientation/problem_orientation_result.json')
    brief = ws.work_ref('input/problem_brief.md')
    ws.write_json(orientation, {'phase': 'READY'})
    ws.write_text(brief, 'The exact scope of the original question.')
    ws.write_json('memory/active_problem_dossier.json', {
        'orientation_ref': orientation, 'problem_brief_ref': brief,
        'problem_ref': ws.work_ref('input/problem.md'),
        'target_contract_ref': ws.work_ref('input/target_contract.json')})
    result_ref = deliver(ws, 1)
    commit_research_memory(ws.root, [result_ref])
    evidence = ws.work_ref('round1/research.md')
    llm = NativeFixture('research')
    original = llm._request
    pending = [orientation, brief, evidence]
    def reply(messages, tools):
        for message in messages:
            if message['role'] == 'tool':
                assert 'error' not in json.loads(message['content'])
        if pending:
            return NativeFixture.tool('read_file', {'path': pending.pop(0)})
        return original(messages, tools)
    monkeypatch.setattr(llm, '_request', reply)
    result = run_global_director(llm=llm, workdir=ws.root,
        input_refs=('memory/active_problem_dossier.json',), call_name='global_research_director_0001')
    assert isinstance(result, StructuredSemanticResult)
    record = json.loads(ws.path(result.host['call_ref']).read_text())
    assert {orientation, brief, evidence} <= set(record['archive_refs'])
    assert brief not in record['input_refs']


@pytest.mark.parametrize('kind', ['artifact', 'semantic'])
def test_both_call_interfaces_pass_readonly_archives(ws, monkeypatch, kind):
    from pace.runtime.semantic import begin_artifact_call, semantic_call, SemanticCallSpec, StructuredSemanticResult
    from test_research_reporter import NativeFixture
    archive = ws.work_ref('round0/context.md')
    ws.write_text(archive, 'Archived definition, read only when needed.')
    llm = ChatCompletionsLLM(model='fixture')
    def reply(messages, tools):
        if messages[-1]['role'] == 'user':
            return NativeFixture.tool('read_file', {'path': archive})
        assert 'error' not in json.loads(messages[-1]['content'])
        return NativeFixture.tool('submit_result', {'ok': True}) if kind == 'semantic' else {
            'choices': [{'message': {'role': 'assistant', 'content': 'Read the archive.'}, 'finish_reason': 'stop'}]}
    monkeypatch.setattr(llm, '_request', reply)
    metadata = {'archive_refs': [archive]}
    if kind == 'artifact':
        result = begin_artifact_call(llm, 'Use the optional context.', workdir=ws.root,
            call_name='context', role='RESEARCH', stage='context', input_refs=(), metadata=metadata)
        assert result.transport_status == 'SUCCESS'
    else:
        result = semantic_call(llm, 'Use the optional context.', workdir=ws.root,
            call_name='context', spec=SemanticCallSpec(role='RESEARCH_REPORTER',
                semantic_schema={'type':'object', 'properties': {'ok': {'type': 'boolean'}},
                                 'required':['ok'], 'additionalProperties':False},
                host_schema_version='test/v1', authority='SEARCH_CONTEXT_ONLY', fatal_scope='CALL'),
            input_refs=(), host_metadata=metadata)
        assert isinstance(result, StructuredSemanticResult)


@pytest.mark.parametrize('role', ['RESEARCH', 'PLANNER', 'WHOLE_PROVER', 'RESEARCH_REPORTER',
                                 'RCI_FRONTIER_EXPLORER', 'finding_verifier'])
def test_declared_archives_are_readonly_and_do_not_authorize_siblings(ws, role):
    archive = ws.work_ref('round1/archive.md')
    sibling = ws.work_ref('round1/private_draft.md')
    ws.write_text(archive, 'Exact archive version.')
    ws.write_text(sibling, 'Pending sibling task.')
    tools = WorkspaceTools(ws.root, {'role':role, 'archive_refs':[archive],
        'output_dirs':[str(Path(archive).parent)]})
    assert tools.execute('read_file', {'path': archive})['content'] == 'Exact archive version.'
    with pytest.raises(ValueError):
        tools.execute('write_file', {'path':archive, 'content':'changed'})
    with pytest.raises(ValueError):
        tools.execute('write_file', {'path':str(Path(archive).parent)+'/./archive.md', 'content':'changed'})
    # Test sibling access without an output-directory grant.
    tools = WorkspaceTools(ws.root, {'role':role, 'archive_refs':[archive]})
    with pytest.raises(ValueError):
        tools.execute('read_file', {'path':sibling})


@pytest.mark.parametrize('role', ['RCI_BLIND_PROVER_PROBE', 'RCI_REFUTE_PROVER_PROBE', 'RCI_FRONTIER_REFUTE_PROBE'])
def test_blind_views_ignore_archive_permissions(ws, role):
    ws.write_text('probe.md', 'Self-contained statement.')
    ws.write_text('mechanism.md', 'Do not expose this mechanism.')
    tools = WorkspaceTools(ws.root, {'role':role, 'input_refs':['probe.md'], 'archive_refs':['mechanism.md']})
    assert tools.execute('list_files', {'path':'.'})['files'] == ['probe.md']
    with pytest.raises(ValueError):
        tools.execute('read_file', {'path':'mechanism.md'})


def test_network_retries_only_failed_request_without_replaying_tools(ws, monkeypatch):
    import io
    import urllib.error
    from test_research_reporter import NativeFixture
    output = ws.work_ref('round1/answer.md')
    payloads = []
    writes = []
    sleeps = []
    sent = []
    def open_request(opener, request, **kwargs):
        payloads.append(json.loads(request.data))
        if payloads[-1]['messages'] == [{'role':'user', 'content':'Reply with OK.'}]:
            assert 'tools' not in payloads[-1]
            response = {'choices':[{'message':{'role':'assistant','content':'OK'}}]}
        elif not sent:
            response = NativeFixture.tool('write_file', {'path':output, 'content':'Saved once.'})
            sent.append(request.data)
        elif len(sent) <= 5:
            sent.append(request.data)
            raise urllib.error.URLError(TimeoutError('temporary connection timeout'))
        else:
            sent.append(request.data)
            response = {'choices':[{'message':{'role':'assistant','content':'Done.'}, 'finish_reason':'stop'}]}
        stream = io.BytesIO(json.dumps(response).encode())
        stream.status, stream.headers = 200, {'x-request-id':'fixture-request-id'}
        return stream
    execute = WorkspaceTools.execute
    def execute_once(self, name, args):
        if name == 'write_file': writes.append(args['path'])
        return execute(self, name, args)
    monkeypatch.setenv('PACE_API_KEY', 'test-only-not-a-secret')
    monkeypatch.setattr('urllib.request.OpenerDirector.open', open_request)
    monkeypatch.setattr('pace.runtime.chat_api.time.sleep', sleeps.append)
    monkeypatch.setattr(WorkspaceTools, 'execute', execute_once)
    llm = ChatCompletionsLLM(model='fixture', base_url='https://example.invalid/v1')
    assert llm.max_retries == 8
    call = begin_call(ws.root, call_id='retry', role='RESEARCH', stage='retry', prompt='Save once.')
    result = llm.complete_unstructured_result('Save once.', workdir=ws.root, call_name='retry',
        ledger_context={'call_id':'retry', 'call_ref':call, 'role':'RESEARCH', 'output_refs':[output]})
    assert isinstance(result, TransportSuccess)
    assert len(sleeps) == 5 and writes == [output]
    assert len(payloads) == 8 and all(payload == sent[1] for payload in sent[2:])
    from pace.artifacts.events import read_events
    events = read_events(ws.root)
    failures = [row for row in events if row['event_kind'] == 'CHAT_REQUEST_FAILURE']
    assert [row['attempt'] for row in failures] == [1, 2, 3, 4, 5]
    assert len({row['http_attempt_id'] for row in failures}) == 5
    assert len({row['request_id'] for row in failures}) == 1
    assert any(row.get('server_request_id') == 'fixture-request-id'
               and row.get('request_kind') == 'probe' for row in events)
    assert json.loads((result.artifact_dir/'chat.json').read_text())[-2]['role'] == 'tool'


@pytest.mark.parametrize('status,attempts', [(None,4), (503,4), (401,1), (403,1)])
def test_request_retry_limit_and_permanent_http_errors(monkeypatch, status, attempts):
    import urllib.error
    calls = []
    def open_request(opener, request, **kwargs):
        calls.append(request)
        if status is None: raise TimeoutError('network timeout')
        raise urllib.error.HTTPError(request.full_url, status, 'test', {}, None)
    monkeypatch.setenv('PACE_API_KEY', 'test-only-not-a-secret')
    monkeypatch.setattr('urllib.request.OpenerDirector.open', open_request)
    monkeypatch.setattr('pace.runtime.chat_api.time.sleep', lambda _:None)
    llm = ChatCompletionsLLM(model='fixture', base_url='https://example.invalid/v1')
    with pytest.raises(RuntimeError): llm._request([], [])
    assert len(calls) == attempts
    if attempts == 4:
        assert json.loads(calls[-1].data)['messages'] == [{'role':'user', 'content':'Reply with OK.'}]


def test_resume_probes_before_replaying_failed_http_request(ws, monkeypatch):
    calls = []
    def unavailable(opener, request, **kwargs):
        calls.append(json.loads(request.data))
        raise ConnectionResetError()
    monkeypatch.setenv('PACE_API_KEY', 'test-only-not-a-secret')
    monkeypatch.setattr('urllib.request.OpenerDirector.open', unavailable)
    monkeypatch.setattr('pace.runtime.chat_api.time.sleep', lambda _:None)
    llm = ChatCompletionsLLM(model='fixture', base_url='https://example.invalid/v1')
    for run in ('first', 'next'):
        result = _checkpoint_call(ws, llm, run, 'Fixed task.')
        assert json.loads((result.artifact_dir/'provider_error.json').read_text())['recovery_probe_failed']
    assert len(calls) == 5 and calls[3] == calls[4]
    assert calls[3]['messages'] == [{'role':'user', 'content':'Reply with OK.'}]


def test_http_attempt_classifies_real_wire_failures(ws, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from pace.artifacts.events import read_events

    payloads = []
    answer = b'{"choices":[{"message":{"role":"assistant","content":"Done."}}]}'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers['Content-Length'])))
            if len(payloads) == 1:
                return  # Close the connection before sending any response headers.
            self.send_response(200)
            self.send_header('Content-Length', str(len(answer)))
            self.send_header('X-Request-Id', f'server-{len(payloads)}')
            self.end_headers()
            self.wfile.write(answer[:10] if len(payloads) == 2 else answer)

        def log_message(self, *args):
            pass

    monkeypatch.setenv('PACE_API_KEY', 'test-only-not-a-secret')
    monkeypatch.setattr('pace.runtime.chat_api.time.sleep', lambda _:None)
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            llm = ChatCompletionsLLM(model='fixture', base_url=f'http://127.0.0.1:{server.server_port}/v1')
            ref = begin_call(ws.root, call_id='wire', role='RESEARCH', stage='wire', prompt='Fixed task.')
            result = llm.complete_unstructured_result('Fixed task.', workdir=ws.root, call_name='wire',
                ledger_context={'call_id':'wire', 'call_ref':ref, 'role':'RESEARCH'})
        finally:
            server.shutdown()
            thread.join()
    assert isinstance(result, TransportSuccess)
    assert len(payloads) == 3 and payloads[0] == payloads[1] == payloads[2]
    failures = [row for row in read_events(ws.root) if row['event_kind'] == 'CHAT_REQUEST_FAILURE']
    assert [(row['failure_stage'], row['status'], row['server_request_id']) for row in failures] == [
        ('wait_first_byte', None, ''), ('read_response', 200, 'server-2'),
    ]


@pytest.mark.parametrize('expired,stage', [(False, 'connect'), (True, 'role_deadline')])
def test_http_connect_failure_distinguishes_role_deadline(ws, monkeypatch, expired, stage):
    import http.client
    from pace.artifacts.events import read_events
    tick = 0

    def clock():
        return tick

    def connect(self):
        nonlocal tick
        tick = 11 if expired else 1
        raise TimeoutError()

    monkeypatch.setenv('PACE_API_KEY', 'test-only-not-a-secret')
    monkeypatch.setattr('pace.runtime.chat_http.time.monotonic', clock)
    monkeypatch.setattr(http.client.HTTPConnection, 'connect', connect)
    llm = ChatCompletionsLLM(model='fixture', base_url='http://127.0.0.1:1/v1',
                             timeout_seconds=10, max_retries=0)
    result = _checkpoint_call(ws, llm, 'deadline', 'Fixed task.')
    failure, = [row for row in read_events(ws.root) if row['event_kind'] == 'CHAT_REQUEST_FAILURE']
    assert failure['failure_stage'] == stage and failure['transport_stage'] == 'connect'
    assert failure['elapsed_seconds'] == tick and result.retry_class == RETRY_TRANSIENT


@pytest.mark.parametrize('agent', ['research', 'prover', 'planner'])
def test_specialist_uses_frozen_archive_even_when_live_navigation_changes(ws, monkeypatch, agent):
    from dataclasses import replace
    from pace.agents.director.dispatch import run_specialist
    from pace.agents.director.context import freeze_context
    from test_research_memory import task
    from test_research_reporter import NativeFixture, use_reporter
    use_reporter(ws)
    ws.write_text('memory/optional.md', 'Saved task-specific context.')
    decision = replace(task(agent), archive_refs=('memory/optional.md',))
    assignment = ws.work_ref('round1/assignment/task.json')
    frozen = freeze_context(ws.root, assignment, decision, [ws.work_ref('input/problem.md')])
    ws.write_json(assignment, {'round_index':1})
    ws.write_text('memory/optional.md', 'New navigation must not replace the saved input.')
    archive = frozen['archive_refs'][0]
    output = ws.work_ref('round1/route.md' if agent == 'planner' else 'round1/research.md')
    llm = ChatCompletionsLLM(model='fixture')
    count = 0
    def reply(messages, tools):
        nonlocal count
        count += 1
        if count == 1:
            return NativeFixture.tool('read_file', {'path':archive})
        if count == 2:
            assert json.loads(messages[-1]['content'])['content'] == 'Saved task-specific context.'
            return NativeFixture.tool('write_file', {'path':output, 'content':'# Current result\nThe original gap remains open.'})
        if agent == 'planner' and count == 3:
            return NativeFixture.tool('write_file', {'path':str(Path(output).with_name('route_relations.json')),
                'content':json.dumps({'schema_version':'route_relations/v1', 'change_ref':output, 'obligations':[]})})
        return {'choices':[{'message':{'role':'assistant','content':'Saved.'},'finish_reason':'stop'}]}
    monkeypatch.setattr(llm, '_request', reply)
    kwargs = dict(llm=llm, workdir=ws.root, prompt='Use the frozen optional context.', agent_id=agent,
        input_refs=frozen['required_refs'], round_index=1, output_ref=output)
    result = run_specialist(**kwargs)
    assert result['status'] == 'SUCCEEDED', result
    completed_count = count
    assert run_specialist(**kwargs)['status'] == 'SUCCEEDED'
    assert count == completed_count


def test_rci_archive_is_readable_but_not_required_and_recovery_reuses_phase(ws, monkeypatch):
    from pace.agents.rci.phase_runner import RciPhaseRunner
    from pace.agents.director.context import freeze_context
    from test_research_memory import task
    from test_research_reporter import NativeFixture, use_reporter
    use_reporter(ws)
    freeze_context(ws.root, ws.work_ref('round1/assignment/task.json'), task(), [ws.work_ref('input/problem.md')])
    ws.write_json(ws.work_ref('round1/assignment/task.json'), {'round_index':1})
    ws.write_text('seed.md', 'Self-contained local question.')
    archive = ws.work_ref('orientation/optional-definition.md')
    ws.write_text(archive, 'A direct archived definition.')
    directory = ws.path(ws.work_ref('round1/rci/attempt_001/candidate_01/frontier_explorer'))
    llm = ChatCompletionsLLM(model='fixture'); calls = []
    def reply(messages, tools):
        calls.append(messages[:])
        if len(calls) == 1:
            assert archive not in messages[-1]['content']
            return NativeFixture.tool('read_file', {'path':archive})
        if len(calls) == 2:
            assert 'error' not in json.loads(messages[-1]['content'])
            return NativeFixture.tool('write_file', {'path':str(directory.relative_to(ws.root)/'change.md'),
                                                   'content':'The local obstruction is still unresolved.'})
        if len(calls) == 3:
            return NativeFixture.tool('write_file', {'path':str(directory.relative_to(ws.root)/'mechanism.md'),
                                                   'content':'A new proposed mechanism, not a theorem.'})
        return NativeFixture.tool('submit_result', {'action':'PROPOSE_MECHANISM','candidate_ref':'seed.md',
                                                   'dependency_refs':[archive]})
    monkeypatch.setattr(llm, '_request', reply)
    runner = RciPhaseRunner(llm=llm)
    kwargs = dict(phase='FRONTIER_EXPLORER', workdir=ws.root, call_dir=directory, target_contract_id='TARGET',
        input_refs={'TARGET_ANCHOR':[ws.work_ref('input/problem.md')], 'REVIEWED_SEED':['seed.md'],
                    'ARCHIVE_SOURCE':[archive]}, prompt_seed='Investigate the fixed task.', round_index=1)
    result = runner.run(**kwargs)
    assert runner.run(**kwargs) == result and len(calls) == 4
    ws.write_text(archive, 'Changed archived definition.')
    with pytest.raises(ValueError, match='version changed'):
        runner.run(**kwargs)
    assert len(calls) == 4


def test_reporter_can_read_request_context_and_optional_archive(ws, monkeypatch):
    from dataclasses import replace
    from pace.agents.reporter import run_research_reporter
    from pace.agents.director.context import freeze_context
    from test_research_memory import task
    from test_research_reporter import NativeFixture, use_reporter
    use_reporter(ws)
    archive = ws.work_ref('orientation/source.md'); ws.write_text(archive, 'Optional source.')
    assignment = ws.work_ref('round1/assignment/task.json')
    frozen = freeze_context(ws.root, assignment, replace(task(),archive_refs=(archive,)), [ws.work_ref('input/problem.md')])
    ws.write_json(assignment, {'round_index':1})
    output = ws.work_ref('round1/research.md'); ws.write_text(output, 'A bounded mathematical result.')
    result_ref = ws.work_ref('round1/agent_result.json')
    ws.write_json(result_ref, {'status':'SUCCEEDED','agent_id':'research','deliverable_ref':output})
    llm = NativeFixture('research'); original = llm._request
    pending = [str(Path(assignment).with_name('context.json')), *frozen['archive_refs']]
    def reply(messages, tools):
        for message in messages:
            if message['role']=='tool': assert 'error' not in json.loads(message['content'])
        if pending: return NativeFixture.tool('read_file', {'path':pending.pop(0)})
        return original(messages, tools)
    monkeypatch.setattr(llm, '_request', reply)
    first = run_research_reporter(llm=llm, workdir=ws.root, result_ref=result_ref)
    assert run_research_reporter(llm=llm, workdir=ws.root, result_ref=result_ref) == first
    assert llm.calls == ['research_reporter_round1']


def test_public_finding_links_reach_exact_prior_turn_evidence_only(ws):
    source = 'work/turn000/round1/findings/theorem.md'
    review = source + '.verification.json'
    neighbor = 'work/turn000/round1/other-task.md'
    ws.write_text(source, 'Original mathematical manuscript.')
    ws.write_json(review, {'stage_status':'PASS'})
    ws.write_text(neighbor, 'Unselected task.')
    ws.write_text('results/findings/R0001.md', '# Published entry')
    ws.write_json('work/state/findings.json', {'entries':[
        {'ref':'results/findings/R0001.md', 'source_ref':source, 'verification_ref':review}]})
    for role in ('RESEARCH', 'RESEARCH_REPORTER', 'MATH_VERIFIER_KERNEL', 'RESEARCH_REPORT_WRITER'):
        tools = WorkspaceTools(ws.root, {'role':role})
        assert tools.readable(source) and tools.readable(review)
        assert not tools.readable(neighbor)
        with pytest.raises(ValueError): tools.execute('write_file', {'path':source,'content':'changed'})
    blind = WorkspaceTools(ws.root, {'role':'RCI_BLIND_PROVER_PROBE'})
    assert not blind.readable(source) and not blind.readable(review)


def _checkpoint_call(ws, llm, call_id, prompt, inputs=(), outputs=(), role='RESEARCH'):
    from pace.artifacts.events import append_event
    ref = begin_call(ws.root, call_id=call_id, role=role, stage='checkpoint', prompt=prompt,
                     input_refs=inputs, metadata={'stage_ref': ws.work_ref('round1')})
    append_event(ws.root, 'SEMANTIC_CALL_STARTED', call_id=call_id, call_ref=ref, role=role)
    return llm._complete_transport_owned(prompt, workdir=ws.root, call_name='checkpoint', output_schema=None,
        ledger_context={'call_id':call_id, 'call_ref':ref, 'role':role, 'input_refs':inputs, 'output_refs':outputs})


@pytest.mark.parametrize('interrupt_batch', [False, True])
def test_chat_resume_does_not_repeat_completed_tools(ws, monkeypatch, interrupt_batch):
    from test_research_reporter import NativeFixture
    from pace.runtime.llm import TransportFailure
    a=ws.work_ref('round1/a.md'); b=ws.work_ref('round1/b.md')
    llm=ChatCompletionsLLM(model='fixture'); requests=[]; executed=[]; first=[True]
    execute=WorkspaceTools.execute
    def tools(self,name,args):
        if interrupt_batch and args['path']==b and first[0]:
            first[0]=False; raise KeyboardInterrupt
        executed.append(args['path']); return execute(self,name,args)
    def reply(messages, tools):
        requests.append(messages[:])
        if len(requests)==1:
            out=NativeFixture.tool('write_file', {'path':a, 'content':'First draft.'})
            out['choices'][0]['message']['tool_calls'][0]['id']='a'
            if interrupt_batch:
                second=NativeFixture.tool('write_file', {'path':b, 'content':'Second draft.'})
                second['choices'][0]['message']['tool_calls'][0]['id']='b'
                out['choices'][0]['message']['tool_calls']+=second['choices'][0]['message']['tool_calls']
            return out
        if len(requests)==2 and not interrupt_batch: raise RuntimeError('HTTP 504 fixture')
        assert messages[-1]['role']=='tool'
        return {'choices':[{'message':{'role':'assistant','content':'Done.'},'finish_reason':'stop'}]}
    monkeypatch.setattr(llm,'_request',reply);monkeypatch.setattr(WorkspaceTools,'execute',tools)
    if interrupt_batch:
        with pytest.raises(KeyboardInterrupt): _checkpoint_call(ws,llm,'first','Fixed task',outputs=(a,b))
    else:
        assert isinstance(_checkpoint_call(ws,llm,'first','Fixed task',outputs=(a,b)),TransportFailure)
    assert isinstance(_checkpoint_call(ws,llm,'second','Fixed task',outputs=(a,b)),TransportSuccess)
    assert executed.count(a)==1
    if interrupt_batch: assert executed.count(b)==1


def test_chat_resume_rejects_changed_read_input(ws, monkeypatch):
    from test_research_reporter import NativeFixture
    from pace.runtime.llm import TransportFailure
    source=ws.work_ref('round1/source.md');ws.write_text(source,'Original theorem.')
    llm=ChatCompletionsLLM(model='fixture');count=[]
    def reply(messages, tools):
        count.append(1)
        if len(count)==1:return NativeFixture.tool('read_file', {'path':source})
        raise RuntimeError('network lost')
    monkeypatch.setattr(llm,'_request',reply)
    _checkpoint_call(ws,llm,'first','Read theorem',inputs=(source,))
    ws.write_text(source,'Different theorem.')
    result=_checkpoint_call(ws,llm,'second','Read theorem',inputs=(source,))
    assert isinstance(result,TransportFailure) and len(count)==2
    assert 'input changed' in result.diagnostics[0]['message']


@pytest.mark.parametrize('changed', [None, 'original', 'original.txt'])
def test_chat_resume_binds_download_and_reading_copy_separately(ws, monkeypatch, changed):
    from hashlib import sha256
    from test_research_reporter import NativeFixture
    from pace.runtime.llm import TransportFailure, TransportSuccess

    original = ws.work_ref('round1/sources/web_cache/paper/original')
    reading = original + '.txt'
    files = {original: b'%PDF example bytes', reading: b'Extracted mathematical text.'}
    for ref, content in files.items():
        ws.path(ref).parent.mkdir(parents=True, exist_ok=True)
        ws.path(ref).write_bytes(content)
    url = 'https://example.org/paper.pdf'
    tool_responses = []

    def fetch(*args, **kwargs):
        tool_responses.append(kwargs['url'])
        return {'artifact_ref': original, 'sha256': sha256(files[original]).hexdigest(),
                'read_ref': reading, 'source_sha256': sha256(files[reading]).hexdigest(),
                'content': 'Extracted mathematical text.'}

    llm = ChatCompletionsLLM(model='fixture', web_search=True)
    requests = []

    def reply(messages, tools):
        requests.append(len(messages))
        if len(requests) == 1:
            return NativeFixture.tool('fetch_url', {'url': url})
        if len(requests) == 2:
            raise RuntimeError('HTTP 504 fixture')
        assert messages[-1]['role'] == 'tool'
        return {'choices': [{'message': {'role': 'assistant', 'content': 'Continued.'}, 'finish_reason': 'stop'}]}

    monkeypatch.setattr('pace.runtime.chat_api.fetch_source', fetch)
    monkeypatch.setattr(llm, '_request', reply)
    arguments = {'outputs': (ws.work_ref('round1/sources'),), 'role': 'SURVEY'}
    assert isinstance(_checkpoint_call(ws, llm, 'first', 'Survey', **arguments), TransportFailure)
    if changed:
        ws.path(str(Path(original).with_name(changed))).write_bytes(b'Substituted version')
    result = _checkpoint_call(ws, llm, 'second', 'Survey', **arguments)
    if changed:
        assert isinstance(result, TransportFailure) and 'input changed' in result.diagnostics[0]['message']
    else:
        assert isinstance(result, TransportSuccess) and len(requests) == 3
    assert tool_responses == [url]


@pytest.mark.parametrize('change_evidence', [False, True])
def test_chat_resume_allows_navigation_bookkeeping_changes_only(ws, monkeypatch, change_evidence):
    from test_research_reporter import NativeFixture
    from pace.runtime.llm import TransportFailure, TransportSuccess

    ref = 'memory/research_index.json'
    before = {'commits': {'binding': 'accepted source'}, 'costs': {}, 'cost_cursor': 1}
    ws.write_json(ref, before)
    llm = ChatCompletionsLLM(model='fixture')
    requests = []

    def reply(messages, tools):
        requests.append(len(messages))
        if len(requests) == 1:
            return NativeFixture.tool('read_file', {'path': ref})
        if len(requests) == 2:
            raise RuntimeError('HTTP 504 fixture')
        assert json.loads(json.loads(messages[-1]['content'])['content']) == before
        return {'choices': [{'message': {'role': 'assistant', 'content': 'Continued.'}, 'finish_reason': 'stop'}]}

    monkeypatch.setattr(llm, '_request', reply)
    assert isinstance(_checkpoint_call(ws, llm, 'first', 'Survey', inputs=(ref,)), TransportFailure)
    updated = {**before, 'costs': {'failed_request': 1}, 'cost_cursor': 2}
    if change_evidence:
        updated['commits'] = {'binding': 'replacement source'}
    ws.write_json(ref, updated)
    result = _checkpoint_call(ws, llm, 'second', 'Survey', inputs=(ref,))
    if change_evidence:
        assert isinstance(result, TransportFailure) and len(requests) == 2
    else:
        assert isinstance(result, TransportSuccess) and len(requests) == 3


def test_search_directory_returns_actionable_error_without_exposing_files(ws):
    tools=WorkspaceTools(ws.root, {'role':'RCI_BLIND_PROVER_PROBE','input_refs':[ws.work_ref('input/problem.md')]})
    with pytest.raises(ValueError,match='use list_files'):
        tools.execute('search_file',{'path':ws.work_ref('input'),'query':'secret'})
    assert tools.execute('list_files',{'path':ws.work_ref('input')})['files']==[ws.work_ref('input/problem.md')]


@pytest.mark.parametrize("reply", [[], {"choices": []}, {"choices": [{"message": None}]}])
def test_malformed_completion_is_recorded_as_an_interface_failure(ws, monkeypatch, reply):
    from pace.runtime.chat_http import HTTPAttempt
    from pace.runtime.llm import TransportFailure

    monkeypatch.setenv("PACE_API_KEY", "test-only")
    requests = []

    def malformed_response(attempt, request, key):
        requests.append(request)
        attempt.phase = "decode_response"
        return reply

    monkeypatch.setattr(HTTPAttempt, "run", malformed_response)
    result = _checkpoint_call(ws, ChatCompletionsLLM(model="fixture", base_url="https://example.invalid"),
                              "malformed", "Check the current task.")
    assert isinstance(result, TransportFailure)
    detail = json.loads((result.artifact_dir / "provider_error.json").read_text())
    assert detail["failure_stage"] == "decode_response"
    assert detail["transient"] is False and len(requests) == 1
