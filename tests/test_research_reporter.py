"""Exercise real handoff boundaries, file tools, and recovery without live inference."""

from pace.orchestrator.memory import commit_round_memory
from pace.orchestrator.results import publish_stage_findings
import json
from pathlib import Path

import pytest

from pace import cli
from pace.agents.prompt_composer import delivery_instruction
from pace.agents.reporter import run_research_reporter, restore_reporter_delivery
from pace.runtime.chat_api import ChatCompletionsLLM
from pace.artifacts.workspace import work_ref
from pace.agents.director.context import load_index
from test_research_memory import ws, deliver, task
from test_finding_publication import ProofPublicationTransport


@pytest.mark.parametrize('agent', ['research', 'prover', 'rci'])
def test_reporter_replay_never_calls_author_or_reporter_again(ws, agent, monkeypatch):
    ref = deliver(ws, 1, agent=agent, reported=False)
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
    from pace.orchestrator.memory import commit_research_memory
    commit_research_memory(ws.root, [ref]); commit_research_memory(ws.root, [ref])
    assert len(load_index(ws.root)['attempts']) == 1
    assert load_index(ws.root)['obligations']['target']['status'] == 'OPEN'
    assert 'Do not write a delta' in delivery_instruction(ws.root, str(folder / 'research.md'), with_reporter=True)


@pytest.mark.parametrize("ws", ["standard", "weak"], indirect=True)
def test_defer_only_the_incomplete_standard_manuscript(ws, monkeypatch):
    from pace.agents.director.findings import check_stage_findings
    from pace.artifacts.results import committed_finding_refs
    from pace.runtime.prompts import standard_workflow
    from pace.runtime.semantic import StructuredSemanticResult

    ref = deliver(ws, 1, reported=False)
    original = str(Path(ref).with_name("research.md"))
    standalone = str(Path(ref).parent / "findings/standalone.md")
    ws.write_text(standalone, "A separate, complete standalone result.")
    result = json.loads(ws.path(ref).read_text())
    ws.write_json(ref, {**result, "published_refs": [standalone]})
    llm = NativeFixture("research")
    original_request = llm._request

    def reply(messages, tools):
        response = original_request(messages, tools)
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        payload = json.loads(function["arguments"])
        payload["delta"]["attempt"]["outcome"] = "NOT_ESTABLISHED"
        payload["finding_refs"] = [original, standalone]
        function["arguments"] = json.dumps(payload)
        return response

    monkeypatch.setattr(llm, "_request", reply)
    saved = run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    expected = (standalone,) if standard_workflow(ws.root) else (original, standalone)
    assert committed_finding_refs(ws.root, ref) == expected
    assert saved["payload"]["delta"]["change_ref"] == original
    verified = []

    def verify(**kwargs):
        verified.append(kwargs["draft_ref"])
        return StructuredSemanticResult(payload={
            "stage_status": "PASS", "reuse_authorized": True, "publication": {"action": "NONE"},
        }, host={})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    for _ in range(2):
        assert check_stage_findings(llm=llm, workdir=ws.root, completed_result_refs=[ref])
    assert tuple(verified) == expected
    assert run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref) == saved
    assert llm.calls == ["research_reporter_round1"]


def test_reporter_acceptance_survives_projection_failure(ws, monkeypatch):
    ref = deliver(ws, 1, reported=False)
    llm = ProofPublicationTransport('NONE')
    import pace.agents.reporter as module
    original = module._write_text_atomic
    monkeypatch.setattr(module, '_write_text_atomic', lambda *_: (_ for _ in ()).throw(OSError('disk interruption')))
    with pytest.raises(OSError): run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    monkeypatch.setattr(module, '_write_text_atomic', original)
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert llm.calls == ['research_reporter_round1']


def test_changed_source_pauses_reporter_replay(ws):
    ref = deliver(ws, 1, reported=False)
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
            schema = next(t['function']['parameters'] for t in tools if t['function']['name'] == 'submit_result')
            if 'initial_context' in schema['properties']:
                payload['initial_context'] = {'brief_markdown': '# Target\n\nProve 2+2=4.'}
                if self.mode == 'research':
                    payload['initial_context']['proof_assessment'] = {'difficulty':'standard','plan_node_level':1,'dependency_depth_level':1}
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
            request_ref = next(r for r in (*ctx['input_refs'], *ctx.get('archive_refs', ())) if r.endswith('/reporter_request.json'))
            request = json.loads((root/request_ref).read_text())
            payload = {'summary':'The handoff derives the fixed equality; independent verification is pending.',
                'delta':{'schema_version':'research_delta/v1','obligation_ids':request['obligation_ids'],
                    'branch_id':request['branch_id'],'change_ref':request['original_refs'][0],'applications':[],
                    'attempt':{'outcome':'LOCAL_RESULT','obstacle_ref':'','reopen_condition_ref':''}}}
        elif 'proof_contract_auditor' in name:
            payload = {'contract_status':'PASS','answer_mode':'POSITIVE_PROOF'}
        elif name.startswith('math_verifier_kernel_'):
            payload = {'stage_status':'PASS','publication_eligible':True,'publication':{'action':'NONE'},
                'summary':'Integer addition gives the requested equality.', 'proof_location':'Final equation.'}
            schema = next(t['function']['parameters'] for t in tools if t['function']['name'] == 'submit_result')
            if 'proof_location' not in schema['properties']:
                payload.update(relation_checks=[{'status':'PASS','statement':'2+2=4'}],
                               target_clause_checks=[{'evidence':'The equality is derived.'}],
                               final_assembly={'derivation':'Integer addition gives the target.'})
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


def use_codex_receipt_validation(transport, monkeypatch):
    from subprocess import CompletedProcess
    from pace.runtime.llm import decide_transport_outcome, reduce_codex_event_timeline

    complete = transport.complete_structured_result

    def validate_before_handoff(prompt, **kwargs):
        delivered = complete(prompt, **kwargs)
        if kwargs["call_name"].endswith("_receipt_correction"):
            return delivered
        return decide_transport_outcome(
            completed=CompletedProcess([], 0, stdout="", stderr=""),
            timeline=reduce_codex_event_timeline('{"type":"turn.completed"}\n'),
            current_output_text=delivered.text, current_output_source="response.txt",
            output_schema=kwargs["output_schema"], invocation_id=delivered.invocation_id,
            call_name=kwargs["call_name"], attempt_count=1, artifact_dir=delivered.artifact_dir,
        )

    monkeypatch.setattr(transport, "complete_structured_result", validate_before_handoff)


@pytest.mark.parametrize('mode', ['proof','research'])
@pytest.mark.parametrize('interrupted', [False,True])
def test_native_cli_initialization_reporter_and_resume(tmp_path, monkeypatch, mode, interrupted):
    transport = NativeFixture(mode, interrupt_reporter=interrupted)
    monkeypatch.setattr('pace.agents.director.harness.build_llm', lambda **_: transport)
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
    assert transport.calls.count('problem_orientation_extract') == 1
    assert 'problem_research_decision' not in transport.calls
    assert not (root/'work/turn002').exists()
    assert len(list(turn.glob('round*/reporter_result.json'))) == (1 if mode=='proof' else 2)
    assert (turn/'input/problem_brief.md').read_text().strip()
    assert not (turn/'orientation/brief_diagnostics.json').exists()
    if mode == 'research':
        assert transport.correct_director
        view = json.loads((turn / 'round3/director_context.json').read_text())['decision_inputs']
        assert work_ref(root, 'round2/progress.md') in view['required_refs']
        latest = json.loads((turn / 'round1/agent_result.json').read_text())['deliverable_ref']
        assert latest in view['archive_refs'] and latest not in view['required_refs']
        assert view['latest_survey'] == ''
        frozen = json.loads((turn / 'round2/reporter_request.json').read_text())
        assert work_ref(root, 'round2/assignment/task.json') in frozen['required_refs']
        assert work_ref(root, 'round2/assignment/route_context.json') in frozen['archive_refs']
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
    summary = next(iter(originals)).with_name('progress.md')
    original_summary = summary.read_bytes()
    delta = summary.with_name('research_delta.json')
    delta.write_bytes(b'{broken')
    summary.write_text("The target was solved without additional hypotheses.")
    assert cli.main(['resume','--run-dir',str(root),'--json']) == 0
    assert delta.read_bytes() == originals[delta] and summary.read_bytes() == original_summary
    stamps = (delta.stat().st_mtime_ns, summary.stat().st_mtime_ns)
    assert cli.main(['resume','--run-dir',str(root),'--json']) == 0
    assert stamps == (delta.stat().st_mtime_ns, summary.stat().st_mtime_ns)
    assert transport.calls == completed_calls
    assert len(load_index(root)['attempts']) == (1 if mode=='proof' else 2)


@pytest.mark.parametrize('parallel', [False, True])
def test_rci_outer_handoff_and_parallel_reporter_recover_without_author(ws, parallel, monkeypatch):
    from pace.agents.director.loop import DirectorLedEngine
    from pace.agents.prompt_composer import PromptComposerResult
    from pace.artifacts.delivery_pause import DeliveryPaused, delivery_pause
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
    review_attempts = []
    def review(**kwargs):
        state = json.loads(ws.path(ws.work_ref('state/director_state.json')).read_text())
        assert tuple(state['completed_result_refs']) == kwargs['completed_result_refs']
        assert len(load_index(ws.root)['attempts']) == (2 if parallel else 1)
        review_attempts.append(kwargs['retry_errors'])
        return len(review_attempts) > 1

    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: first,
        composer=lambda **_: PromptComposerResult(final_prompt='Work on the original question.', call_id='composer'),
        specialists={'rci':author,'research':author}, max_rounds=2 if parallel else 1, max_proof_states=2,
        reporter=reporter, review_findings=review, target_contract_id='TARGET', commit_memory=commit_round_memory, commit_results=publish_stage_findings)
    result = engine.run()
    assert result.phase == 'FAILED'
    assert len(author_calls) == (2 if parallel else 1)
    from pace.agents.director.recovery import _restore_operational_checkpoint
    from pace.orchestrator.writing.tasks import active_writing_request, writing_artifacts
    from test_writing_stage import WritingTransport
    writer = WritingTransport()
    monkeypatch.setattr("pace.pipeline.build_llm", lambda **_: writer)
    assert cli.main(["outline", "--run-dir", str(ws.root), "--message",
                     str(ws.root / ws.work_ref("input/problem.md")), "--json"]) == 0
    writing = active_writing_request(ws.root)
    assert writing["action"] == "outline"
    assert _restore_operational_checkpoint(ws, result.state.to_json())
    assert active_writing_request(ws.root) == writing
    assert "research_outline" in writing_artifacts(ws.root)
    assert writer.calls == ["research_report_outline"]
    engine.director = lambda **_: pytest.fail('Do not redispatch the reserved task')
    with pytest.raises(DeliveryPaused, match="Finding verification is incomplete"):
        engine.resume()
    assert delivery_pause(ws.root)['stage'] == 'findings'
    assert json.loads(engine.state_path.read_text())['phase'] == 'DIRECTOR'
    result = engine.resume()
    assert result.phase == 'FINALIZE'
    assert review_attempts == [True, True]
    assert not delivery_pause(ws.root)
    assert author_calls.count('rci') == 1
    assert author_calls.count('research') == (1 if parallel else 0)
    assert len(llm.calls) == (2 if parallel else 1)
    assert len(load_index(ws.root)['attempts']) == (2 if parallel else 1)


def test_native_director_repairs_empty_purpose_locally(ws):
    from pace.agents.director.agent import run_global_director
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
    from pace.agents.director.agent import run_global_director
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


@pytest.mark.parametrize("parallel", [False, True])
def test_director_repairs_both_scope_fields_without_changing_the_task(ws, monkeypatch, parallel):
    from pace.agents.director.agent import run_global_director
    from pace.runtime.semantic import StructuredSemanticResult

    accepted = task("research").to_json()
    bad_scope = {**accepted, "obligation_ids": [], "branch_id": " "}
    invalid = bad_scope
    prefix = ""
    if parallel:
        accepted = {**task("planner").to_json(), "parallel_tasks": [
            {key: value for key, value in accepted.items() if key != "parallel_tasks"},
        ]}
        invalid = {**accepted, "parallel_tasks": [
            {key: value for key, value in bad_scope.items() if key != "parallel_tasks"},
        ]}
        prefix = "/parallel_tasks/0"

    transport = NativeFixture("research")

    def reply(messages, _tools):
        if transport.calls[-1].endswith("_receipt_correction"):
            prompt = messages[1]["content"]
            assert f"{prefix}/branch_id" in prompt and f"{prefix}/obligation_ids" in prompt
            return transport.tool("submit_result", accepted)
        return transport.tool("submit_result", invalid)

    monkeypatch.setattr(transport, "_request", reply)
    result = run_global_director(llm=transport, workdir=ws.root)
    assert isinstance(result, StructuredSemanticResult)
    assert result.payload == accepted
    assert len(transport.calls) == 2


def test_director_can_repair_replan_purpose_and_evidence_together(ws, monkeypatch):
    from pace.agents.director.agent import run_global_director
    from pace.orchestrator.memory import commit_research_memory
    from pace.runtime.semantic import StructuredSemanticResult

    earlier = deliver(ws, 1, agent="planner")
    commit_research_memory(ws.root, [earlier])
    evidence = str(Path(earlier).with_name("research.md"))
    initial = task("planner").to_json()
    accepted = {**initial, "next_step_kind": "REPLAN", "change_refs": [evidence]}
    transport = NativeFixture("research")

    def reply(messages, _tools):
        if not transport.calls[-1].endswith("_receipt_correction"):
            return transport.tool("submit_result", initial)
        prompt = messages[1]["content"]
        assert "/next_step_kind" in prompt and "/change_refs" in prompt
        return transport.tool("submit_result", accepted)

    monkeypatch.setattr(transport, "_request", reply)
    response = run_global_director(llm=transport, workdir=ws.root)
    assert isinstance(response, StructuredSemanticResult) and response.payload == accepted
    assert len(transport.calls) == 2


@pytest.mark.parametrize("replace_valid_task", [False, True])
def test_director_corrects_mixed_primary_companion_and_reference_errors(
    ws, monkeypatch, replace_valid_task
):
    from pace.agents.director.agent import run_global_director
    from pace.runtime.semantic import SemanticRepresentationFailure, StructuredSemanticResult

    question = ws.work_ref("input/problem.md")
    valid = task(
        "rci", research_operation="CONTINUE_PROBLEM", research_problem_ref=question,
        input_refs=[question],
    ).to_json()
    companion = task("planner").to_json()
    companion.pop("parallel_tasks")
    valid["parallel_tasks"] = [companion]
    original = {
        **valid, "research_problem_ref": "missing.md", "input_refs": [],
        "archive_refs": ["missing-evidence.md"],
        "parallel_tasks": [{
            **companion, "next_step_kind": "WRONG", "obligation_ids": ["unknown"],
            "branch_id": " ", "spurious_control_field": True,
        }],
    }
    transport = NativeFixture("research")

    def reply(messages, _tools):
        if not transport.calls[-1].endswith("_receipt_correction"):
            return transport.tool("submit_result", original)
        for pointer in (
            "/research_problem_ref", "/input_refs", "/archive_refs",
            "/parallel_tasks/0/next_step_kind", "/parallel_tasks/0/obligation_ids",
            "/parallel_tasks/0/branch_id", "/parallel_tasks/0/spurious_control_field",
        ):
            assert pointer in messages[1]["content"]
        corrected = {**valid, "prompt_brief": "A different task."} if replace_valid_task else valid
        return transport.tool("submit_result", corrected)

    monkeypatch.setattr(transport, "_request", reply)
    if replace_valid_task:
        use_codex_receipt_validation(transport, monkeypatch)
    result = run_global_director(llm=transport, workdir=ws.root)
    assert len(transport.calls) == 2
    if replace_valid_task:
        assert isinstance(result, SemanticRepresentationFailure)
        assert any(row["json_pointer"] == "/prompt_brief" for row in result.diagnostics)
    else:
        assert isinstance(result, StructuredSemanticResult) and result.payload == valid


def test_reporter_receipt_repair_cannot_write_mathematical_files(ws):
    ref = deliver(ws, 1, reported=False)
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
    from pace.agents.director.loop import DirectorLedEngine
    from pace.agents.director import parallel as module
    from pace.agents.prompt_composer import PromptComposerResult
    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: None,
        composer=lambda **_: PromptComposerResult(final_prompt='Task', call_id='fixture'),
        specialists={'prover':lambda **_:None,'research':lambda **_:None}, max_proof_states=2, commit_memory=commit_round_memory, commit_results=publish_stage_findings)
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
    policy = json.loads(ws.path(ws.work_ref('state/research_execution_policy.json')).read_text())
    ws.write_json(ws.work_ref('state/research_execution_policy.json'), {**policy,'mode':'proof'})
    ref = deliver(ws,1, reported=False); llm = NativeFixture('proof'); original_request = llm._request
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


def test_prover_reporter_repairs_a_duplicate_manuscript_reference(ws, monkeypatch):
    ref = deliver(ws, 1, agent="prover", reported=False)
    candidate = str(Path(ref).with_name("research.md"))
    duplicate = str(Path(ref).parent / "findings/copy.md")
    ws.write_text(duplicate, ws.path(candidate).read_text())
    result = json.loads(ws.path(ref).read_text())
    ws.write_json(ref, {**result, "published_refs": [duplicate]})
    llm = NativeFixture("research")
    original_request = llm._request
    proposed = {}

    def reply(messages, tools):
        response = original_request(messages, tools)
        if not llm.calls[-1].endswith("_receipt_correction"):
            function = response["choices"][0]["message"]["tool_calls"][0]["function"]
            payload = json.loads(function["arguments"])
            payload["delta"]["change_ref"] = duplicate
            payload["delta"]["applications"] = [
                {
                    "id": "same-proof",
                    "obligation_id": "target",
                    "argument_ref": duplicate,
                    "premise_refs": [],
                }
            ]
            proposed.update(payload)
            function["arguments"] = json.dumps(payload)
            return response
        payload = json.loads(json.dumps(proposed))
        payload["delta"]["change_ref"] = candidate
        payload["delta"]["applications"][0]["argument_ref"] = candidate
        return NativeFixture.tool("submit_result", payload)

    monkeypatch.setattr(llm, "_request", reply)
    saved = run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert len(llm.calls) == 2
    assert saved["payload"]["delta"]["change_ref"] == candidate
    assert saved["payload"]["delta"]["applications"][0]["argument_ref"] == candidate
    assert saved["payload"]["summary"] == proposed["summary"]


def test_all_annotated_references_repaired_once_with_frozen_meaning(ws, monkeypatch):
    ref = deliver(ws, 1, reported=False)
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


@pytest.mark.parametrize("interrupted", [False, True])
def test_reporter_corrects_mixed_scope_summary_and_application_shape(ws, monkeypatch, interrupted):
    ref = deliver(ws, 1, reported=False)
    manuscript = str(Path(ref).with_name("research.md"))
    llm = NativeFixture("research")
    original_request = llm._request
    fixed_application = {
        "id": "supported", "obligation_id": "target",
        "argument_ref": manuscript, "premise_refs": [manuscript],
    }

    def reply(messages, tools):
        response = original_request(messages, tools)
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        payload = json.loads(function["arguments"])
        if llm.calls[-1].endswith("_receipt_correction"):
            for pointer in (
                "/summary", "/delta/branch_id", "/delta/obligation_ids",
                "/delta/applications/0", "/delta/applications/1/premise_refs",
                "/delta/applications/1/argument_ref",
            ):
                assert pointer in messages[1]["content"]
            assert manuscript in llm.context["input_refs"]
            if interrupted and len(llm.calls) == 2:
                raise KeyboardInterrupt()
            payload["delta"]["applications"] = [fixed_application]
        else:
            payload["summary"] = " "
            payload["delta"].update(
                branch_id="wrong", obligation_ids=["unknown"],
                applications=[
                    {"id": "", "obligation_id": "unknown", "argument_ref": manuscript,
                     "premise_refs": []},
                    {**fixed_application, "argument_ref": manuscript + " §1",
                     "premise_refs": [manuscript, manuscript]},
                ],
            )
        function["arguments"] = json.dumps(payload)
        return response

    monkeypatch.setattr(llm, "_request", reply)
    if interrupted:
        use_codex_receipt_validation(llm, monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    result = run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert result["payload"]["delta"]["applications"] == [fixed_application]
    assert llm.calls.count("research_reporter_round1") == 1
    assert len(llm.calls) == (3 if interrupted else 2)
    assert not load_index(ws.root)["applications"]


@pytest.mark.parametrize("defect", ["reference", "missing_summary", "blank_summary"])
def test_resume_reporter_uses_saved_failed_receipt_instead_of_new_summary(ws, monkeypatch, defect):
    ref=deliver(ws,1, reported=False); llm=NativeFixture('research'); original=llm._request
    def wrong_path(messages, tools):
        if llm.calls[-1].endswith('_receipt_correction'): raise KeyboardInterrupt
        response=original(messages,tools)
        f=response['choices'][0]['message']['tool_calls'][0]['function']
        p = json.loads(f["arguments"])
        if defect == "reference":
            p["delta"]["change_ref"] += " §1"
        elif defect == "missing_summary":
            del p["summary"]
        else:
            p["summary"] = " "
        f["arguments"] = json.dumps(p)
        return response
    monkeypatch.setattr(llm,'_request',wrong_path)
    with pytest.raises(KeyboardInterrupt): run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    monkeypatch.setattr(llm,'_request',original)
    result = run_research_reporter(llm=llm,workdir=ws.root,result_ref=ref)
    assert result["payload"]["summary"].startswith("The handoff derives")
    bound = [row["ref"] for row in result["bindings"]]
    assert len(bound) == len(set(bound))
    request = json.loads(ws.path(ref).with_name("reporter_request.json").read_text())
    from pace.agents.reporter import reporter_schema
    applications = reporter_schema(request)["properties"]["delta"]["properties"]["applications"]
    premises = applications["items"]["properties"]["premise_refs"]["items"]["enum"]
    assert ws.path(ref).with_name("research.md").relative_to(ws.root).as_posix() in premises
    assert not any("/assignment/" in premise or premise == ref for premise in premises)
    assert llm.calls.count('research_reporter_round1')==1
    assert llm.calls.count('research_reporter_round1_receipt_correction')==2


def test_rejected_content_change_cannot_be_accepted_by_resume(ws, monkeypatch):
    ref=deliver(ws,1, reported=False); llm=NativeFixture('research'); original=llm._request
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


def test_resume_reporter_repairs_unparseable_response_without_repeating_summary(ws):
    ref = deliver(ws, 1, reported=False)

    class ReporterTransport(ProofPublicationTransport):
        def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
            if call_name == "research_reporter_round1":
                return self._reply(
                    "Receipt did not serialize.", workdir=workdir, call_name=call_name,
                    ledger_context=ledger_context, structured=True,
                )
            if len(self.calls) == 1:
                self.calls.append(call_name)
                raise KeyboardInterrupt()
            return super().complete_structured_result(
                prompt, workdir=workdir, call_name=call_name, ledger_context=ledger_context,
                **kwargs,
            )

    llm = ReporterTransport("NONE")
    with pytest.raises(KeyboardInterrupt):
        run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    receipt = run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert receipt["payload"]["summary"].startswith("The original argument")
    assert llm.calls.count("research_reporter_round1") == 1


def test_json_only_rci_handoff_schema_is_valid_and_disallows_applications():
    from pace.agents.reporter import reporter_schema
    from pace.runtime.schema import preflight_output_schema,validate_json_instance
    schema=reporter_schema({'original_refs':['outcome.json'],'bindings':[{'ref':'outcome.json'}]})
    assert preflight_output_schema(schema).ok
    assert schema['properties']['delta']['properties']['applications']['maxItems']==0


def test_rci_receipt_correction_removes_forbidden_proposal_and_preserves_summary(ws, monkeypatch):
    ref = deliver(ws, 1, agent="rci", reported=False)
    outcome = str(Path(ref).with_name("outcome.json"))
    ws.write_json(outcome, {"status": "HANDOFF"})
    ws.write_json(ref, {
        "agent_id": "rci", "status": "SUCCEEDED", "deliverable_ref": outcome,
        "manuscript_refs": [],
    })
    llm = NativeFixture("research")
    original_request = llm._request
    original_summary = []

    def reply(messages, tools):
        response = original_request(messages, tools)
        name = llm.calls[-1]
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        payload = json.loads(function["arguments"])
        if name == "research_reporter_round1":
            original_summary.append(payload["summary"])
            payload["delta"]["applications"] = [{
                "id": "inadmissible", "obligation_id": "target",
                "argument_ref": outcome, "premise_refs": [],
            }]
            payload["delta"]["attempt"]["outcome"] = "APPLICATION_PROPOSED"
            function["arguments"] = json.dumps(payload)
        else:
            assert "requires applications=[]" in messages[1]["content"]
        return response

    monkeypatch.setattr(llm, "_request", reply)
    saved = run_research_reporter(llm=llm, workdir=ws.root, result_ref=ref)
    assert llm.calls == ["research_reporter_round1", "research_reporter_round1_receipt_correction"]
    assert saved["payload"]["delta"]["applications"] == []
    assert saved["payload"]["delta"]["attempt"]["outcome"] == "LOCAL_RESULT"
    assert saved["payload"]["summary"] == original_summary[0]


@pytest.mark.parametrize("interrupt_round", [1, 2])
def test_cli_resumes_a_director_receipt_accepted_before_dispatch(tmp_path, monkeypatch, interrupt_round):
    from pace.agents.director import harness as pipeline
    transport = NativeFixture("research")
    monkeypatch.setattr(pipeline, "build_llm", lambda **_: transport)
    original = pipeline.run_global_director
    name = f"global_research_director_{interrupt_round:04d}"
    def interrupt_after_accept(**kwargs):
        result = original(**kwargs)
        if kwargs["call_name"] == name:
            raise KeyboardInterrupt
        return result

    root = tmp_path / "accepted"
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    turn = root / "work/turn001"
    with monkeypatch.context() as scoped:
        scoped.setattr(pipeline, "run_global_director", interrupt_after_accept)
        assert cli.main(["run", "--problem", str(problem), "--problem-id", "accepted",
                         "--output", str(tmp_path), "--json"]) == 130
    decision = turn / f"round{interrupt_round}/director_decision.json"
    saved = decision.read_bytes()
    old_calls = list(transport.calls)
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert decision.read_bytes() == saved
    assert all(transport.calls.count(call) == old_calls.count(call) for call in set(old_calls))
    assert json.loads((turn / "final_report.json").read_text())["result"] == "PASS"
    assert not (root / "work/turn002").exists()


@pytest.mark.parametrize("interrupt_stage", ["after_rejection", "during_correction", "after_acceptance"])
def test_cli_resumes_director_multifield_correction_without_rescheduling(
    tmp_path, monkeypatch, interrupt_stage
):
    from pace.agents.director import harness
    from pace.runtime import semantic

    transport = NativeFixture("research")
    monkeypatch.setattr(harness, "build_llm", lambda **_: transport)
    initial = "global_research_director_0001"
    correction = initial + "_receipt_correction"
    request = transport._request
    finish = semantic.finish_call

    def receive(messages, tools):
        body = request(messages, tools)
        if transport.calls[-1] == initial:
            function = body["choices"][0]["message"]["tool_calls"][0]["function"]
            payload = json.loads(function["arguments"])
            payload.update(branch_id=" ", obligation_ids=["unknown"], archive_refs=["missing.md"])
            function["arguments"] = json.dumps(payload)
        if transport.calls[-1] == correction:
            for pointer in ("/next_step_kind", "/branch_id", "/obligation_ids", "/archive_refs"):
                assert pointer in messages[1]["content"]
            if interrupt_stage == "during_correction" and transport.calls.count(correction) == 1:
                raise KeyboardInterrupt()
        return body

    def interrupt_after_receipt(workdir, ref, **kwargs):
        finish(workdir, ref, **kwargs)
        stage = json.loads((Path(workdir) / ref).read_text())["stage"]
        if interrupt_stage == "after_rejection" and stage == initial:
            assert kwargs["status"] == "REPRESENTATION_FAILED"
            raise KeyboardInterrupt()
        if interrupt_stage == "after_acceptance" and stage == correction:
            assert kwargs["status"] == "SUCCEEDED"
            raise KeyboardInterrupt()

    monkeypatch.setattr(transport, "_request", receive)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove 2+2=4.\n")
    root = tmp_path / "director-resume"
    with monkeypatch.context() as scoped:
        scoped.setattr(semantic, "finish_call", interrupt_after_receipt)
        assert cli.main(["run", "--problem", str(problem), "--problem-id", root.name,
                         "--output", str(tmp_path), "--json"]) == 130
    assert cli.main(["resume", "--run-dir", str(root), "--json"]) == 0
    assert transport.calls.count(initial) == 1
    assert transport.calls.count(correction) == (2 if interrupt_stage == "during_correction" else 1)
    assert transport.calls.count("director_led_survey_01") == 1
    assert json.loads((root / "work/turn001/final_report.json").read_text())["result"] == "PASS"


def test_director_rejected_receipt_is_bound_to_the_original_request(ws, monkeypatch):
    from pace.agents.director.agent import run_global_director
    from pace.runtime import semantic

    transport = NativeFixture("research")
    accepted = []
    finish = semantic.finish_call
    initial = "global_research_director_0001"

    def interrupt_after_rejection(workdir, ref, **kwargs):
        finish(workdir, ref, **kwargs)
        assert kwargs["status"] == "REPRESENTATION_FAILED"
        raise KeyboardInterrupt()

    kwargs = {
        "llm": transport, "workdir": ws.root, "call_name": initial,
        "on_accept": accepted.append,
        "host_metadata": {"round_index": 1, "task_id": "round-0001-director"},
    }
    with monkeypatch.context() as scoped:
        scoped.setattr(semantic, "finish_call", interrupt_after_rejection)
        with pytest.raises(KeyboardInterrupt):
            run_global_director(**kwargs, control_context="First bounded scheduling request.")
    result = run_global_director(**kwargs, control_context="Updated bounded scheduling request.")
    assert result.payload == accepted[0]
    assert transport.calls == [initial, initial, initial + "_receipt_correction"]


@pytest.mark.parametrize(
    ("ws", "purpose", "expected"),
    [
        ("standard", "CONTINUE", {"target"}),
        ("standard", "REASSESS", {"target", "aux"}),
        ("weak", "CONTINUE", {"target", "aux"}),
    ],
    indirect=["ws"],
)
def test_variant_controls_review_queue_and_rebuild(ws, purpose, expected):
    from pace.agents.director.context import freeze_context
    from pace.runtime.prompts import standard_workflow
    from pace.orchestrator.memory import commit_research_memory, rebuild_research_index

    planner = deliver(ws, 0, agent="planner")
    route = str(Path(planner).with_name("research.md"))
    ws.write_json(
        str(Path(planner).with_name("route_relations.json")),
        {
            "schema_version": "route_relations/v1",
            "change_ref": route,
            "obligations": [
                {
                    "id": "aux",
                    "kind": "ROUTE_SUFFICIENT",
                    "parent_ids": ["target"],
                    "scope_ref": route,
                    "statement_ref": route,
                    "branch_id": "b-target",
                    "premise_groups": [],
                }
            ],
        },
    )
    commit_research_memory(ws.root, [planner])
    folder = ws.work_ref("round1")
    assignment = f"{folder}/assignment/task.json"
    manuscript = f"{folder}/research.md"
    result = f"{folder}/agent_result.json"
    ids = ["target", "aux"]
    frozen = freeze_context(
        ws.root, assignment, task(obligation_ids=ids, next_step_kind=purpose), []
    )
    variant = json.loads(ws.path(ws.work_ref("resources/manifest.json")).read_text())[
        "resource_variant"
    ]
    assert standard_workflow(ws.root) == (variant == "standard")
    ws.write_json(assignment, {"round_index": 1})
    ws.write_text(
        manuscript,
        "One manuscript supplies an auxiliary lemma and its application to the target.",
    )
    ws.write_json(
        result,
        {"status": "SUCCEEDED", "agent_id": "research", "deliverable_ref": manuscript},
    )
    llm = NativeFixture("research")

    def report(messages, tools):
        return llm.tool(
            "submit_result",
            {
                "summary": "Both uses were proposed.",
                "delta": {
                    "schema_version": "research_delta/v1",
                    "obligation_ids": ids,
                    "branch_id": "b-target",
                    "change_ref": manuscript,
                    "applications": [
                        {
                            "id": ident,
                            "obligation_id": ident,
                            "argument_ref": manuscript,
                            "premise_refs": [],
                        }
                        for ident in ids
                    ],
                    "attempt": {
                        "outcome": "APPLICATION_PROPOSED",
                        "obstacle_ref": "",
                        "reopen_condition_ref": "",
                    },
                },
            },
        )

    llm._request = report
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=result)
    ws.write_json(
        ws.work_ref("state/director_state.json"),
        {"completed_result_refs": [planner, result]},
    )
    commit_research_memory(ws.root, [planner, result])
    for index in (load_index(ws.root), rebuild_research_index(ws.root)):
        assert {a["obligation_id"] for a in index["applications"].values()} == expected
        assert {
            a["obligation_id"] for a in index["attempts"][-1]["applications"]
        } == set(ids)
        assert index["obligations"]["aux"]["status"] == "OPEN"
    assert llm.calls == ["research_reporter_round1"]


def test_focused_director_can_open_complete_memory_index(ws):
    from pace.agents.director.context import director_decision_inputs
    from pace.orchestrator.memory import RESEARCH_INDEX_REF

    view = director_decision_inputs(ws.root, [])
    assert RESEARCH_INDEX_REF in view["archive_refs"]
    assert RESEARCH_INDEX_REF not in view["required_refs"]
