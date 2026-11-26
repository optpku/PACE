"""Research authority, committed replay and independent application checks."""

from pace.orchestrator.memory import commit_round_memory
from pace.orchestrator.results import publish_stage_findings
import json
from pathlib import Path

import pytest

from pace.contracts.research.director_protocol import parse_director_decision
from pace.contracts.research.memory import binding, digest, prepare_delta, prepare_route, public_file, repair_route_references
from pace.agents.director.loop import DirectorLedEngine
from pace.artifacts.events import read_events
from pace.orchestrator.memory import (initialize_research_memory, commit_research_memory,
    rebuild_research_index, research_brief, refresh_application_memory)
from pace.artifacts.workspace import RunWorkspace, work_ref
from pace.agents.prompt_composer import PromptComposerResult
from pace.agents.director.context import director_archive_refs, freeze_context, load_index


@pytest.fixture
def ws(tmp_path, request):
    ws = RunWorkspace.create(tmp_path, 'memory')
    ws.write_text(ws.work_ref('input/problem.md'), 'Prove the requested uniform bound.')
    ws.write_text(ws.work_ref('input/request.md'), 'Keep all hypotheses.')
    ws.write_json(ws.work_ref('input/target_contract.json'), {'contract_id':'TARGET','literal_target':'Uniform bound'})
    ws.write_json(ws.work_ref('state/research_execution_policy.json'), {'research_memory_version':1, 'research_delta_owner':'reporter', 'mode':'research'})
    ws.write_json(ws.work_ref('state/proof_loop_budget.json'), {'max_total_proof_states':3})
    from pace.runtime.prompts import materialize_run_resources
    materialize_run_resources(ws.root, resource_variant=getattr(request, "param", "standard"))
    from pace.agents.director.harness import prepare_agent_environments
    prepare_agent_environments(ws.root, mode="research")
    initialize_research_memory(ws.root)
    return ws


def task(agent='research', **kw):
    return parse_director_decision({'action':'DISPATCH','agent_id':agent,'prompt_brief':'Investigate the fixed gap.',
        'input_refs':[], 'obligation_ids':['target'],'branch_id':'b-target','archive_refs':[],
        'next_step_kind':'INITIAL_PLAN' if agent=='planner' else 'CONTINUE','change_refs':[],
        **({'research_operation':'NEW_PROBLEM'} if agent=='rci' else {}), **kw})


def record_report(ws, result_ref, findings=()):
    """Supply a committed Reporter handoff to tests starting at the memory boundary."""
    folder = Path(result_ref).parent
    original = json.loads(ws.path(result_ref).read_text())["deliverable_ref"]
    receipt = {
        "schema_version": "research_reporter/v1", "authority": "SEARCH_CONTEXT_ONLY",
        "result_ref": result_ref, "bindings": [binding(ws.root, original)],
        "payload": {"delta": json.loads(ws.path(str(folder / "research_delta.json")).read_text()),
                    "summary": "Record the stated result and its remaining gap.", "finding_refs": list(findings)},
    }
    ws.write_json(str(folder / "reporter_result.json"), {**receipt, "receipt_id": digest(receipt)})


def deliver(ws, number, *, application=False, agent='research', reported=True, **task_fields):
    folder=ws.work_ref(f'round{number}')
    output=f'{folder}/research.md'; result_ref=f'{folder}/agent_result.json'
    assignment=f'{folder}/assignment/task.json'
    freeze_context(ws.root, assignment, task(agent, **task_fields), [ws.work_ref('input/problem.md')])
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
    if reported and agent in {'research','prover','rci'}:
        record_report(ws, result_ref)
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


@pytest.mark.parametrize("damage", ["edited", "rehashed", "swapped"])
def test_replay_rejects_changed_or_misplaced_memory_commit(ws, damage):
    first = deliver(ws, 1)
    commit_research_memory(ws.root, [first])
    second = deliver(ws, 2)
    commit_research_memory(ws.root, [second])
    path = ws.path(first).with_name("memory_commit.json")
    if damage in {"edited", "rehashed"}:
        record = json.loads(path.read_text())
        record["payload"]["delta"]["attempt"]["outcome"] = "LOCAL_RESULT"
        if damage == "rehashed":
            record["commit_id"] = digest({k: v for k, v in record.items() if k != "commit_id"})
        path.write_text(json.dumps(record))
    else:
        path.write_bytes(ws.path(second).with_name("memory_commit.json").read_bytes())
    before = ws.path("memory/research_index.json").read_bytes()
    for replay in (commit_research_memory, rebuild_research_index):
        with pytest.raises(ValueError, match="memory commit differs"):
            replay(ws.root)
        assert ws.path("memory/research_index.json").read_bytes() == before


def test_missing_index_rebuilds_and_missing_event_is_repaired(ws):
    ref=deliver(ws,1); commit_research_memory(ws.root,[ref])
    expected=load_index(ws.root)['attempts']
    ws.path('memory/research_index.json').unlink()
    assert rebuild_research_index(ws.root)['attempts']==expected
    event=ws.path(ws.work_ref('events.jsonl')); event.write_text('')
    commit_research_memory(ws.root,[ref]); commit_research_memory(ws.root,[ref])
    assert len(read_events(ws.root))==1



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
        target_contract_id='TARGET',max_rounds=1, commit_memory=commit_round_memory, commit_results=publish_stage_findings)
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


@pytest.mark.parametrize('combined', [False, True])
@pytest.mark.parametrize('records,locator,valid', [
    ([{'clause_id': 'TC001'}, {'clause_id': 'TC002'}], '#TC001', True),
    ([{'id': 'lemma'}], '#lemma', True),
    ([{'clause_id': 'other', 'text': 'TC001 appears only in prose.'}], '#TC001', False),
    ([{'clause_id': 'TC001'}, {'clause_id': 'TC001'}], '#TC001', False),
])
def test_json_route_locator_resolves_a_unique_record_without_editing_source(ws, combined, records, locator, valid):
    contract = ws.work_ref('input/clauses.json')
    source = ws.work_ref('round1/route.md')
    route = ws.work_ref('round1/route_relations.json')
    ws.write_json(contract, {'records': records})
    ws.write_text(source, 'A route with an explicitly bound target clause.')
    row = {'id': 'lemma', 'kind': 'ROUTE_SUFFICIENT', 'statement_ref': contract, 'scope_ref': contract,
           'parent_ids': [], 'branch_id': 'b-target', 'premise_groups': []}
    for field in ('statement', 'scope'):
        if combined:
            row[field + '_ref'] += locator
        else:
            row[field + '_locator'] = locator
    ws.write_json(route, {'schema_version': 'route_relations/v1', 'change_ref': source, 'obligations': [row]})
    original = ws.path(contract).read_bytes()
    before = ws.path(route).read_bytes()
    if valid:
        assert len(repair_route_references(ws.root, route)) == (2 if combined else 0)
        prepared = prepare_route(ws.root, route, {})['obligations'][0]
        assert all(binding['locator'] == locator for binding in prepared['bindings'])
        assert repair_route_references(ws.root, route) == []
    else:
        with pytest.raises(ValueError, match='one JSON record'):
            repair_route_references(ws.root, route)
            prepare_route(ws.root, route, {})
        assert ws.path(route).read_bytes() == before
    assert ws.path(contract).read_bytes() == original


def test_interrupted_handoff_repair_reuses_original_author(ws):
    from pace.agents.director.dispatch import run_specialist
    from test_finding_publication import ProofPublicationTransport
    output = ws.work_ref('round1/route.md')
    assignment = ws.work_ref('round1/assignment/task.json')
    context = freeze_context(ws.root, assignment, task("planner"), [ws.work_ref('input/problem.md')])
    ws.write_json(assignment, {'round_index': 1})
    class Writer(ProofPublicationTransport):
        def __init__(self): super().__init__('NONE'); self.authors=0; self.repairs=0
        def complete_unstructured_result(self,prompt,*,workdir,call_name,ledger_context):
            if call_name.endswith('_handoff_correction'):
                self.repairs+=1
                if self.repairs==1: raise KeyboardInterrupt()
                ws.write_json(ws.work_ref("round1/route_relations.json"), {"schema_version":"route_relations/v1","change_ref":output,"obligations":[]})
            else:
                self.authors+=1
                ws.write_text(output,'A complete original argument; the remaining bridge is explicit.')
            return self._reply('Delivered',workdir=workdir,call_name=call_name,ledger_context=ledger_context,structured=False)
    llm=Writer()
    kwargs=dict(llm=llm,workdir=ws.root,prompt='Deliver the original argument.',agent_id='planner',
        input_refs=tuple(context['required_refs']),output_ref=output,round_index=1,context={'target_contract_id':'TARGET'})
    with pytest.raises(KeyboardInterrupt):run_specialist(**kwargs)
    before=ws.path(output).read_bytes()
    assert ws.path(str(Path(output).with_name('author_delivery.json'))).is_file()
    result=run_specialist(**kwargs)
    assert result['status']=='SUCCEEDED' and llm.authors==1 and llm.repairs==2
    assert ws.path(output).read_bytes()==before


@pytest.mark.parametrize("changes_relations", [False, True])
@pytest.mark.parametrize("interruption", ["transport", "materialization"])
def test_planner_work_order_revision_resumes_without_another_call(
    ws, monkeypatch, changes_relations, interruption
):
    from pace.agents import planner
    from pace.agents.director import dispatch
    from pace.runtime.chat_api import WorkspaceTools
    from test_finding_publication import ProofPublicationTransport

    first = deliver(ws, 1, agent="planner")
    commit_research_memory(ws.root, [first])
    base = ws.path(first).with_name("research.md")
    old = base.read_text()
    output = ws.work_ref("round2/route.md")
    folder = ws.path(output).parent
    assignment = ws.work_ref("round2/assignment/task.json")
    decision = task(
        "planner", prompt_brief="REVISE_ROUTE Replace the next work order.",
        next_step_kind="REPLAN", change_refs=[base.relative_to(ws.root).as_posix()],
    )
    frozen = freeze_context(ws.root, assignment, decision, [])
    ws.write_json(assignment, {"round_index": 2})
    assembly = planner.build_prompt(
        workdir=ws.root, decision=decision, target_contract_id="TARGET",
        output_ref=output, manifest_ref=assignment,
    )
    patch_ref = ws.work_ref("round2/revision.json")
    assert f"Write the requested deliverable to `{patch_ref}`" in assembly.prompt_seed
    assert f"Write the requested deliverable to `{output}`" not in assembly.prompt_seed
    assert "relations_unchanged" in assembly.prompt_seed
    updated = "Investigate the next gap in the existing route."
    relations = [{
        "id": "remaining-bound",
        "kind": "ROUTE_SUFFICIENT",
        "statement_ref": base.relative_to(ws.root).as_posix(),
        "scope_ref": output,
        "parent_ids": ["target"],
        "branch_id": "b-target",
        "premise_groups": [],
    }] if changes_relations else []

    class PlannerTransport(ProofPublicationTransport):
        def complete_unstructured_result(self, prompt, *, workdir, call_name, ledger_context):
            if call_name.endswith("_handoff_correction"):
                assert changes_relations
                tools = WorkspaceTools(ws.root, ledger_context)
                tools.execute("read_file", {"path": relations[0]["statement_ref"]})
                tools.execute("read_file", {"path": frozen["route_context"]["ref"]})
                assert ledger_context["output_refs"] == [ws.work_ref("round2/route_relations.json")]
                ws.write_json(ws.work_ref("round2/route_relations.json"), {
                    "schema_version": "route_relations/v1", "obligations": relations,
                    "change_ref": output,
                })
            else:
                assert call_name == "director_led_planner_02"
                ws.write_json(patch_ref, {
                    "edits": [{"old": old, "new": updated}],
                    "relations_unchanged": not changes_relations,
                })
            return self._reply(
                "Delivered the revision.", workdir=workdir, call_name=call_name,
                ledger_context=ledger_context, structured=False,
            )

    llm = PlannerTransport("NONE")
    kwargs = dict(
        llm=llm, workdir=ws.root, prompt=assembly.prompt_seed, decision=decision,
        input_refs=frozen["required_refs"], output_ref=output, round_index=2,
    )
    write_text = dispatch._write_text_atomic

    def interrupt_after_materializing(path, text):
        write_text(path, text)
        if path == ws.path(output):
            raise KeyboardInterrupt()

    begin = dispatch.begin_artifact_call

    def interrupt_after_transport(*args, **kwargs):
        result = begin(*args, **kwargs)
        assert result.transport_status == "SUCCESS"
        raise KeyboardInterrupt()

    with monkeypatch.context() as scoped:
        if interruption == "transport":
            scoped.setattr(dispatch, "begin_artifact_call", interrupt_after_transport)
        else:
            scoped.setattr(dispatch, "_write_text_atomic", interrupt_after_materializing)
        with pytest.raises(KeyboardInterrupt):
            planner.run(**kwargs)
    assert (folder / "author_delivery.json").is_file()
    result = planner.run(**kwargs)
    assert result["status"] == "SUCCEEDED"
    assert llm.calls.count("director_led_planner_02") == 1
    assert len(llm.calls) == 1 + changes_relations
    assert ws.path(output).read_text() == updated
    assert base.read_text() == old
    assert json.loads((folder / "route_relations.json").read_text()) == {
        "schema_version": "route_relations/v1", "obligations": relations, "change_ref": output,
    }


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

def test_changed_obligation_starts_a_fresh_reassessment_count(ws):
    for i in range(1, 4):
        commit_research_memory(ws.root, [deliver(ws, i, application=True)])
    index = load_index(ws.root)
    assert '"reassessment_due": ["target"]' in research_brief(ws.root)
    index["obligations"]["target"]["version"] = "revised-scope"
    ws.write_json("memory/research_index.json", index)
    assert '"reassessment_due": []' in research_brief(ws.root)
    context = freeze_context(ws.root, ws.work_ref("round4/assignment/task.json"), task(), [])
    for i in range(1, 4):
        original = ws.work_ref(f"round{i}/research.md")
        assert original in context["archive_refs"] and original not in context["required_refs"]
    assert len(load_index(ws.root)["attempts"]) == 3


def test_local_progress_under_root_does_not_clear_target_reassessment(ws):
    for n in range(1, 4):
        result = deliver(ws, n)
        path = ws.path(str(Path(result).with_name("research_delta.json")))
        delta = json.loads(path.read_text())
        delta["attempt"]["outcome"] = "LOCAL_RESULT" if n == 2 else "NOT_ESTABLISHED"
        ws.write_json(str(path.relative_to(ws.root)), delta)
        record_report(ws, result)
        commit_research_memory(ws.root, [result])

    assert '"reassessment_due": ["target"]' in research_brief(ws.root)
    assert load_index(ws.root)["obligations"]["target"]["status"] == "OPEN"

def test_scoped_progress_and_planner_snapshot(ws):
    from pace.orchestrator.memory import research_progress

    index = load_index(ws.root)
    root = index["obligations"]["target"]
    for node, kind in (("aux", "ROUTE_SUFFICIENT"), ("necessary", "TARGET_REQUIRED")):
        index["obligations"][node] = {
            **root,
            "id": node,
            "kind": kind,
            "parent_ids": ["target"],
            "version": node,
        }

    def attempt(node, outcome, n):
        versions = {
            "target": root["version"],
            node: index["obligations"][node]["version"],
        }
        return {
            "obligation_ids": [node],
            "obligation_versions": {node: versions[node]},
            "related_versions": versions,
            "attempt": {"outcome": outcome, "obstacle_ref": ""},
            "branch_id": "b-target",
            "result_ref": f"handoff-{n}",
            "change_ref": root["statement_ref"],
        }

    index["attempts"] = [attempt("aux", "NOT_ESTABLISHED", n) for n in range(3)]
    index["attempts"].append(attempt("aux", "LOCAL_RESULT", 3))
    counts, signal = research_progress(index)
    assert counts == {"aux": 0, "target": 4}
    ws.write_json("memory/research_index.json", index)
    assignment = ws.work_ref("round5/assignment/task.json")
    decision = task("planner")
    context = freeze_context(ws.root, assignment, decision, [])
    frozen = ws.path(context["route_context"]["ref"])
    assert json.loads(frozen.read_text())["target_progress"] == signal
    for node, n in (("aux", 4), ("necessary", 5), ("necessary", 6)):
        index["attempts"].append(attempt(node, "APPLICATION_PROPOSED", n))
        index["applications"][str(n)] = {
            "result_ref": f"handoff-{n}",
            "obligation_id": node,
            "obligation_version": node,
            "argument_ref": root["statement_ref"],
            "status": "APPLICABLE",
        }
        counts, progress = research_progress(index)
        assert counts["target"] == {4: 5, 5: 0, 6: 1}[n]
    ws.write_json("memory/research_index.json", index)
    assert freeze_context(ws.root, assignment, decision, []) == context
    assert json.loads(frozen.read_text())["target_progress"] == signal


def test_only_substantive_committed_dependency_change_resets_target(ws):
    from pace.orchestrator.memory import research_progress

    original = deliver(ws, 0, agent="planner")
    route_ref = str(Path(original).with_name("route_relations.json"))
    route = json.loads(ws.path(route_ref).read_text())

    def node(ident, kind):
        return {
            "id": ident,
            "kind": kind,
            "statement_ref": route["change_ref"],
            "scope_ref": route["change_ref"],
            "parent_ids": ["target"],
            "branch_id": "b-target",
            "premise_groups": [],
        }

    route["obligations"] = [
        node("required", "TARGET_REQUIRED"),
        node("aux", "ROUTE_SUFFICIENT"),
    ]
    ws.write_json(route_ref, route)
    commit_research_memory(ws.root, [original])
    for n in range(1, 4):
        latest = deliver(ws, n)
        commit_research_memory(ws.root, [latest])
    source = str(Path(latest).with_name("research.md"))
    decision = task("planner", next_step_kind="REPLAN", change_refs=[source])
    for n, groups, expected in ((4, [], 3), (5, [["aux"]], 0)):
        folder = ws.work_ref(f"round{n}")
        output = f"{folder}/route.md"
        result = f"{folder}/agent_result.json"
        assignment = f"{folder}/assignment/task.json"
        frozen = freeze_context(ws.root, assignment, decision, [])
        assert (
            json.loads(ws.path(frozen["route_context"]["ref"]).read_text())[
                "target_progress"
            ]["attempts_without_target_progress"]
            == 3
        )
        ws.write_json(assignment, {"round_index": n})
        ws.write_text(output, "The first required inference uses the auxiliary lemma.")
        required = {**route["obligations"][0], "premise_groups": groups}
        ws.write_json(
            f"{folder}/route_relations.json",
            {**route, "change_ref": output, "obligations": [required]},
        )
        ws.write_json(
            result,
            {"agent_id": "planner", "status": "SUCCEEDED", "deliverable_ref": output},
        )
        state_ref = ws.work_ref("state/director_state.json")
        state = json.loads(ws.path(state_ref).read_text())
        state["completed_result_refs"].append(result)
        ws.write_json(state_ref, state)
        commit_research_memory(ws.root, [result])
        assert (
            research_progress(load_index(ws.root))[1][
                "attempts_without_target_progress"
            ]
            == expected
        )
    rebuilt = research_progress(rebuild_research_index(ws.root))[1]
    assert rebuilt == {
        "status": "OPEN",
        "attempts_without_target_progress": 0,
        "last_change_ref": source,
    }
