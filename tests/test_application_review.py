"""Replay durable applications and account for work without promoting author claims."""
import json
from pathlib import Path

import pytest

from test_research_memory import ws, deliver, task
from pace.contracts.research.memory import prepare_route
from pace.orchestrator.finding_publication import publish_applications, pending_finding_checks
from pace.runtime.output.memory import commit_research_memory, rebuild_research_index, research_brief, update_research_costs
from pace.runtime.research_context import load_index
from pace.runtime.output.events import append_event
from pace.runtime.output.calls import begin_call
from pace.runtime.semantic import StructuredSemanticResult


@pytest.fixture
def accepted_review(ws, monkeypatch):
    requests = []

    def verify(**kwargs):
        request = kwargs['application_request']
        requests.append(request)
        ws.write_text(request['review_ref'], 'Complete independent application review.')
        return StructuredSemanticResult({'stage_status': 'PASS', 'reuse_authorized': True,
            'publication': {'action': 'NONE'}, 'application_checks': [{
                'obligation_id': request['obligation_id'], 'obligation_version': request['obligation_version'],
                'status': 'APPLICABLE', 'review_ref': request['review_ref']}]}, {})

    monkeypatch.setattr('pace.orchestrator.finding_publication.verify_finding', verify)
    return requests


def application_handoff(ws, number, proposals):
    result = deliver(ws, number)
    delta_ref = str(Path(result).with_name('research_delta.json'))
    delta = json.loads(ws.path(delta_ref).read_text())
    delta['applications'] = [{
        'id': label, 'obligation_id': 'target',
        'argument_ref': argument, 'premise_refs': premises,
    } for label, argument, premises in proposals]
    ws.write_json(delta_ref, delta)
    return result, delta_ref


def test_one_review_per_argument_and_obligation_in_a_handoff(ws, accepted_review):
    argument = ws.work_ref('round1/research.md')
    first, second = ws.work_ref('input/first.md'), ws.work_ref('input/second.md')
    alternate = ws.work_ref('round1/alternative.md')
    for ref in (first, second, alternate):
        ws.write_text(ref, f'Mathematical text in {ref}.')
    result, delta_ref = application_handoff(ws, 1, [
        ('high-dimensional', argument, [first]),
        ('fixed-dimensional', argument, [second, first]),
        ('alternative-proof', alternate, [first]),
    ])
    original = {ref: ws.path(ref).read_bytes() for ref in (argument, alternate, delta_ref)}
    commit_research_memory(ws.root, [result])
    assert load_index(ws.root)['obligations']['target']['status'] == 'OPEN'
    assert len(load_index(ws.root)['applications']) == 2
    app = next(app for app in load_index(ws.root)['applications'].values() if app['argument_ref'] == argument)
    assert app['proposal_ids'] == ['high-dimensional', 'fixed-dimensional']
    assert publish_applications(llm=object(), root=ws.root)
    assert len(accepted_review) == 2
    merged = next(request for request in accepted_review if request['argument_ref'] == argument)
    assert [row['ref'] for row in merged['bindings']].count(first) == 1
    assert [row['ref'] for row in merged['bindings']].count(second) == 1
    assert all(ws.path(ref).read_bytes() == content for ref, content in original.items())
    assert rebuild_research_index(ws.root)['obligations']['target']['status'] == 'CLOSED'
    assert publish_applications(llm=object(), root=ws.root)
    assert len(accepted_review) == 2


def test_application_reuse_ignores_labels_and_order_but_not_new_evidence(ws, accepted_review):
    argument = ws.work_ref('round1/research.md')
    first, second, extra = [ws.work_ref(f'input/premise-{number}.md') for number in range(3)]
    for ref in (first, second, extra):
        ws.write_text(ref, f'Mathematical text in {ref}.')
    target = ws.work_ref('input/target_contract.json')
    for number, premises in enumerate(([first, second], [second, target, first], [first, second, extra]), 1):
        result, _ = application_handoff(ws, number, [(f'proposal-{number}', argument, premises)])
        commit_research_memory(ws.root, [result])
        assert publish_applications(llm=object(), root=ws.root)
        assert len(accepted_review) == (1 if number < 3 else 2)
    assert len({request['application_key'] for request in accepted_review}) == 2
    assert extra in [row['ref'] for row in accepted_review[-1]['bindings']]


def test_equivalent_new_proposal_reuses_existing_legacy_receipt(ws, accepted_review, monkeypatch):
    from pace.contracts.research import memory

    prepare = memory.prepare_delta

    def legacy_delta(*args, **kwargs):
        delta = prepare(*args, **kwargs)
        for app in delta['applications']:
            del app['proposal_ids']
        return delta

    argument = ws.work_ref('round1/research.md')
    old, _ = application_handoff(ws, 1, [('legacy-label', argument, [])])
    with monkeypatch.context() as scoped:
        scoped.setattr(memory, 'prepare_delta', legacy_delta)
        commit_research_memory(ws.root, [old])
    assert publish_applications(llm=object(), root=ws.root)
    record_ref = next(iter(load_index(ws.root)['applications'].values()))['review_ref']
    receipt_path = ws.path(record_ref)
    receipt = receipt_path.read_bytes()
    new, _ = application_handoff(ws, 2, [('different-new-label', argument, [])])
    commit_research_memory(ws.root, [new])
    assert publish_applications(llm=object(), root=ws.root)
    assert len(accepted_review) == 1 and receipt_path.read_bytes() == receipt
    assert rebuild_research_index(ws.root)['obligations']['target']['status'] == 'CLOSED'
    assert not pending_finding_checks(ws.root)


def test_replay_commit_before_index_without_repeating_author(ws, monkeypatch):
    ref = deliver(ws, 1)
    import pace.runtime.output.workspace as persistence
    write = persistence._write_json_atomic
    def interrupt(path, value):
        if path.name == 'research_index.json' and value['commits']:
            raise KeyboardInterrupt()
        write(path, value)
    with monkeypatch.context() as scoped:
        scoped.setattr(persistence, '_write_json_atomic', interrupt)
        with pytest.raises(KeyboardInterrupt): commit_research_memory(ws.root, [ref])
    assert ws.path(str(Path(ref).with_name('memory_commit.json'))).is_file()
    assert not load_index(ws.root)['commits']
    commit_research_memory(ws.root, [ref])
    assert len(load_index(ws.root)['commits']) == 1


def test_application_receipt_survives_interruption_before_index(ws, monkeypatch):
    ref = deliver(ws, 1, application=True); commit_research_memory(ws.root, [ref])
    calls = []
    def verify(**kw):
        calls.append(kw)
        r = kw['application_request']; ws.write_text(r['review_ref'], 'Complete independent application review.')
        return StructuredSemanticResult({'stage_status': 'PASS', 'reuse_authorized': True,
            'publication': {'action': 'NONE'}, 'application_checks': [{
                'obligation_id': r['obligation_id'], 'obligation_version': r['obligation_version'],
                'status': 'APPLICABLE', 'review_ref': r['review_ref']}]}, {})
    monkeypatch.setattr('pace.orchestrator.finding_publication.verify_finding', verify)
    with monkeypatch.context() as scoped:
        def interrupt(*args): raise KeyboardInterrupt()
        scoped.setattr('pace.runtime.output.memory.refresh_application_memory', interrupt)
        with pytest.raises(KeyboardInterrupt): publish_applications(llm=object(), root=ws.root)
    assert pending_finding_checks(ws.root)
    assert publish_applications(llm=object(), root=ws.root)
    assert len(calls) == 1 and not pending_finding_checks(ws.root)
    app = next(iter(load_index(ws.root)['applications'].values()))
    record = json.loads(ws.path(app['review_ref']).read_text())
    ws.write_text(record['review_binding']['ref'], 'Altered review')
    with pytest.raises(ValueError, match='version changed'): publish_applications(llm=object(), root=ws.root)
    assert len(calls) == 1


def test_root_target_cannot_be_redefined_by_planner(ws):
    ref = ws.work_ref('round1/route_relations.json')
    target = ws.work_ref('input/target_contract.json')
    ws.write_json(ref, {'schema_version': 'route_relations/v1', 'change_ref': target,
        'obligations': [{'id': 'target', 'kind': 'TARGET_REQUIRED', 'statement_ref': target,
                         'scope_ref': target, 'branch_id': 'b-target', 'parent_ids': [], 'premise_groups': []}]})
    with pytest.raises(ValueError, match='root target'): prepare_route(ws.root, ref, load_index(ws.root)['obligations'])


def test_costs_use_transport_intervals_once_and_report_missing_time(ws):
    deliver(ws, 1)
    for name in ('finished', 'unobserved'):
        ref = begin_call(ws.root, call_id=name, role='RESEARCH', stage='test', prompt='Work', metadata={'round_index': 1})
        append_event(ws.root, 'SEMANTIC_CALL_STARTED', call_id=name, call_ref=ref)
    append_event(ws.root, 'TRANSPORT_TIMING', call_id='finished', elapsed_seconds=3.5)
    append_event(ws.root, 'PROFILE_SPAN', call_id='finished', elapsed_seconds=9)
    ws.write_json(ws.work_ref('state/sessions.json'), [
        {'ended_at': '2026-11-17', 'elapsed_seconds': 5, 'gap_since_previous_session_seconds': 200},
        {'status': 'end_unobserved', 'elapsed_seconds': 0}])
    update_research_costs(ws.root); update_research_costs(ws.root)
    costs = load_index(ws.root)['costs']; branch = costs['branches']['b-target']
    assert branch['cumulative_call_seconds'] == 3.5
    assert branch['missing_timing_calls'] == 1 and branch['logical_calls'] == 2
    assert costs['completed_session_seconds'] == 5 and not costs['session_measurement_complete']


def test_retired_obligation_stays_retired_after_review_refresh(ws):
    from pace.runtime.output.memory import refresh_application_memory
    ref=deliver(ws,1,agent='planner')
    original=ws.work_ref('round1/research.md')
    ws.write_json(ws.work_ref('round1/route_relations.json'), {'schema_version':'route_relations/v1',
        'change_ref':original,'obligations':[{'id':'old-route','kind':'ROUTE_SUFFICIENT',
            'statement_ref':original,'scope_ref':original,'branch_id':'b-route',
            'parent_ids':['target'],'premise_groups':[],'disposition':'SUPERSEDED'}]})
    commit_research_memory(ws.root,[ref]);refresh_application_memory(ws.root)
    assert load_index(ws.root)['obligations']['old-route']['status']=='SUPERSEDED'
    assert load_index(ws.root)['obligations']['target']['status']=='OPEN'


def test_cross_task_source_requires_committed_owner_in_either_mode(ws):
    from pace.contracts.research.memory import binding,check_source_ownership
    original=ws.work_ref('round2/proof.md');owner=ws.work_ref('round2/agent_result.json')
    ws.write_text(original,'Complete original proof.')
    ws.write_json(ws.work_ref('round2/assignment/task.json'), {'round_index':2})
    ws.write_json(owner,{'status':'SUCCEEDED','deliverable_ref':original})
    row=binding(ws.root,original)
    with pytest.raises(ValueError,match='uncommitted'):check_source_ownership(ws.root,row)
    ws.write_json(ws.work_ref('state/proof_state.json'),{'completed_refs':[original]})
    check_source_ownership(ws.root,row)


def test_application_review_is_saved_before_semantic_success_publication(ws, monkeypatch):
    from pace.runtime.chat_api import ChatCompletionsLLM
    from test_research_reporter import NativeFixture
    ref = deliver(ws, 1, application=True); commit_research_memory(ws.root, [ref])
    requests = []
    llm = ChatCompletionsLLM(model='fixture')
    def reply(messages, tools):
        requests.append(messages)
        app = next(iter(load_index(ws.root)['applications'].values()))
        review = str(Path(ref).parent/'verification'/f"application-{app['application_key']}.md")
        if messages[-1]['role'] == 'user':
            return NativeFixture.tool('write_file', {'path':review,'content':'Independent check of the exact bound application.'})
        assert all('error' not in json.loads(m['content']) for m in messages if m['role']=='tool')
        return NativeFixture.tool('submit_result', {'stage_status':'PASS','reuse_authorized':True,
            'publication':{'action':'NONE'}, 'application_checks':[{'obligation_id':app['obligation_id'],
            'obligation_version':app['obligation_version'],'status':'APPLICABLE','review_ref':review}]})
    monkeypatch.setattr(llm, '_request', reply)
    with monkeypatch.context() as scoped:
        def interrupt(*args, **kwargs): raise KeyboardInterrupt
        scoped.setattr('pace.runtime.semantic.finish_call', interrupt)
        with pytest.raises(KeyboardInterrupt): publish_applications(llm=llm, root=ws.root)
    assert len(requests)==2
    assert publish_applications(llm=llm, root=ws.root)
    assert len(requests)==2
    assert load_index(ws.root)['obligations']['target']['status']=='CLOSED'

def test_application_interface_failure_remains_pending_on_rebuild(ws, accepted_review):
    ref = deliver(ws, 1, application=True)
    commit_research_memory(ws.root, [ref])
    assert publish_applications(llm=object(), root=ws.root)
    app = next(iter(load_index(ws.root)['applications'].values()))
    record = json.loads(ws.path(app['review_ref']).read_text())
    record.update(verdict={'stage_status': 'VERIFIER_ERROR'}, review_binding=None)
    ws.write_json(app['review_ref'], record)
    rebuilt = rebuild_research_index(ws.root)
    assert next(iter(rebuilt['applications'].values()))['status'] == 'PROPOSED'
    assert rebuilt['obligations']['target']['status'] == 'OPEN' and pending_finding_checks(ws.root)
    assert not publish_applications(llm=object(), root=ws.root)
    assert len(accepted_review) == 1
    assert publish_applications(llm=object(), root=ws.root, retry_errors=True)
    assert len(accepted_review) == 2 and not pending_finding_checks(ws.root)


@pytest.mark.parametrize('status', ['APPLICABLE', 'NOT_ESTABLISHED'])
def test_recover_double_encoded_review_without_repeating_independent_check(ws, monkeypatch, status):
    from pace.agents.verification import kernel
    from test_finding_publication import ProofPublicationTransport

    ref = deliver(ws, 1, application=True)
    commit_research_memory(ws.root, [ref])
    app = next(iter(load_index(ws.root)['applications'].values()))
    schema = kernel._finding_receipt_schema

    def legacy_schema(request=None):
        value = schema(request)
        value['properties'].pop('application_checks')
        value['required'] = ['stage_status']
        return value

    class Review(ProofPublicationTransport):
        def complete_structured_result(self, prompt, *, workdir, call_name, ledger_context, **kwargs):
            request_ref = next(r for r in ledger_context['input_refs'] if r.endswith('.request.json'))
            request = json.loads((workdir / request_ref).read_text())
            if ledger_context.get('output_refs'):
                ws.write_text(request['review_ref'], 'The exact application has been independently checked.')
            payload = {'stage_status': 'PASS', 'reuse_authorized': True, 'publication': {'action': 'NONE'},
                       'evidence': 'Literal mathematical text [1, 2] remains unchanged.',
                       'application_checks': json.dumps([{'obligation_id': request['obligation_id'],
                           'obligation_version': request['obligation_version'], 'status': status,
                           'review_ref': request['review_ref']}])}
            return self._reply(json.dumps(payload), workdir=workdir, call_name=call_name,
                               ledger_context=ledger_context, structured=True)

    llm = Review('NONE')
    with monkeypatch.context() as scoped:
        scoped.setattr(kernel, '_finding_receipt_schema', legacy_schema)
        assert not publish_applications(llm=llm, root=ws.root)
    assert len(llm.calls) == 2
    snapshots = {p: p.read_bytes() for p in ws.root.rglob('response.txt')}
    assert publish_applications(llm=llm, root=ws.root, retry_errors=True)
    assert len(llm.calls) == 2
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    restored = next(iter(load_index(ws.root)['applications'].values()))
    assert restored['status'] == status
    assert (load_index(ws.root)['obligations']['target']['status'] == 'CLOSED') == (status == 'APPLICABLE')
    record = json.loads(ws.path(restored['review_ref']).read_text())
    assert record['verdict']['application_checks'][0]['status'] == status
    assert record['verdict']['evidence'] == 'Literal mathematical text [1, 2] remains unchanged.'
    assert record['receipt_revalidation_ref']


def test_local_receipt_decoding_cannot_supply_authorization():
    from pace.agents.verification.kernel import _finding_receipt_schema
    from pace.runtime.json_values import decode_structured_fields
    from pace.runtime.schema import validate_json_instance

    value = {'stage_status': 'PASS', 'reuse_authorized': 'true', 'publication': {'action': 'NONE'},
             'application_checks': '[{"obligation_id":"target","obligation_version":"v","status":"APPLICABLE","review_ref":"review.md"}]'}
    result, changes = decode_structured_fields(value, _finding_receipt_schema())
    assert changes == ['/application_checks'] and result['reuse_authorized'] == 'true'
    assert validate_json_instance(result, _finding_receipt_schema())
