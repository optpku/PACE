"""Frozen dependencies, concurrent planning and limited model input views."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from test_research_memory import ws, task
from pace.runtime.research_context import freeze_context, validate_context
from pace.runtime.output.workspace import work_ref
from pace.orchestrator.director_led import DirectorLedEngine
from pace.runtime.prompt_composer import PromptComposerResult, write_dependency_manifest
from pace.agents.research.rci.context import select_inputs


def test_frozen_view_keeps_mutable_navigation_and_detects_changed_source(ws):
    ws.write_text('memory/note.md','old navigation')
    ws.write_text(ws.work_ref('round0/theorem.md'),'fixed theorem')
    decision=replace(task(),input_refs=('memory/note.md',ws.work_ref('round0/theorem.md')))
    assignment=ws.work_ref('round1/assignment/task.json')
    context=freeze_context(ws.root,assignment,decision,decision.input_refs)
    ws.write_text('memory/note.md','new navigation')
    again=freeze_context(ws.root,assignment,decision,decision.input_refs)
    assert context==again
    assert ws.path(context['required_refs'][0]).read_text()=='old navigation'
    ws.write_text(ws.work_ref('round0/theorem.md'),'changed theorem')
    with pytest.raises(ValueError,match='version changed'):validate_context(ws.root,str(Path(assignment).with_name('context.json')))


@pytest.mark.parametrize("legacy_reservation", [False, True])
def test_parallel_inputs_frozen_before_planner_finishes(ws, monkeypatch, legacy_reservation):
    from threading import Event
    from pace.runtime.context_state import empty_search_progress
    from pace.runtime.output.memory import note_ref
    progress = empty_search_progress(target_contract_id="TARGET", target_contract_ref=ws.work_ref("input/target_contract.json"))
    ws.write_json("memory/verified_progress.json", progress)
    ready=Event();seen=[]
    ws.write_text(ws.work_ref('round0/route.md'),'old route')
    ws.write_json(ws.work_ref('state/current_route.json'),{'ref':ws.work_ref('round0/route.md')})
    planner=task('planner');research=task()
    decision=replace(planner,parallel_tasks=(research,))
    def author(**kw):
        folder=Path(kw['output_ref']).parent
        context=json.loads(ws.path(str(folder/'assignment/route_context.json')).read_text())
        seen.append((kw['agent_id'],ws.path(context['route']['ref']).read_text()))
        ws.write_text(kw['output_ref'],'new route' if kw['agent_id']=='planner' else 'bounded original derivation')
        ws.write_text(str(folder/'memory_note.md'), 'Preserve this branch and its open question.')
        if kw['agent_id']=='planner':
            ws.write_json(str(folder/'route_relations.json'),{'schema_version':'route_relations/v1','change_ref':kw['output_ref'],'obligations':[]})
            ready.set()
        else:
            assert ready.wait(10)
            from test_research_round_limit import write_delta
            write_delta(ws.root,ws.path(kw['output_ref']))
        return {'agent_id':kw['agent_id'],'status':'SUCCEEDED','deliverable_ref':kw['output_ref']}
    engine=DirectorLedEngine(workdir=ws.root,director=lambda **_:decision,
        composer=lambda **_:PromptComposerResult('Task',''),specialists={'planner':author,'research':author},
        target_contract_id='TARGET',max_rounds=2)
    if legacy_reservation:
        # Persist a batch from the earlier layout: only the first worker received the snapshot.
        from pace.orchestrator import director_parallel
        assign = engine._assignment_input_refs
        def interrupt_workers(*args, **kwargs):
            raise KeyboardInterrupt
        with monkeypatch.context() as scoped:
            scoped.setattr(engine, '_assignment_input_refs',
                           lambda decision, index, **_: assign(decision, index))
            scoped.setattr(director_parallel, 'run_batch', interrupt_workers)
            with pytest.raises(KeyboardInterrupt):
                engine.run()
    def interrupt_after_parent_commit(state):
        assert state.phase == 'DIRECTOR' and state.last_batch_result_ref
        raise KeyboardInterrupt
    with monkeypatch.context() as scoped:
        scoped.setattr(engine, '_publish_round_progress', interrupt_after_parent_commit)
        with pytest.raises(KeyboardInterrupt):
            engine.run(resume=legacy_reservation)
    refs = [ws.work_ref(f'round{i}/agent_result.json') for i in (1, 2)]
    manifest = json.loads(ws.path(ws.work_ref('round1/batch.json')).read_text())
    common = ws.work_ref('round1/progress_snapshot.json')
    for i, row in enumerate(manifest['contexts'], 1):
        assignment = ws.work_ref(f'round{i}/assignment')
        ctx = json.loads(ws.path(f'{assignment}/context.json').read_text())
        ticket = json.loads(ws.path(f'{assignment}/task.json').read_text())
        expected = i == 1 or not legacy_reservation
        assert (common in ctx['required_refs']) == expected
        assert row['input_refs'] == ticket['input_refs']
        assert (common in ticket['input_refs']) == expected
        assert json.loads(ws.path(common).read_text()) == progress
    assert not any(ws.path(note_ref(ref)).exists() for ref in refs)
    assert engine.resume().phase=='FINALIZE'
    assert all(ws.path(note_ref(ref)).is_file() for ref in refs)
    assert sorted(seen)==[('planner','old route'),('research','old route')]
    assert engine.resume().phase=='FINALIZE'
    assert len(seen)==2
    # Both batch members, including a rejection with no published finding,
    # must be visible at the next scheduling boundary.
    reviews = [ws.work_ref(f"round{i}/findings/local.verification.json") for i in (1, 2, 3)]
    for ref in reviews:
        ws.write_json(ref, {"verdict": {"stage_status": "MATH_FAIL"}})
    inputs = engine._director_input_refs(engine._load_state())
    assert all(ref in inputs for ref in [*refs, *reviews[:2]])
    assert reviews[2] not in inputs


def test_blind_input_selection_never_receives_context_or_mechanism(ws):
    inputs={'PROBE_TASK':['probe.md'],'RESEARCH_CONTEXT':['secret-memory.md'],'MECHANISM':['secret-mechanism.md']}
    for phase in ('BLIND_PROVER_PROBE','REFUTE_PROVER_PROBE','FRONTIER_REFUTE_PROBE'):
        assert select_inputs(phase,inputs,ws.root)=={'PROBE_TASK':('probe.md',)}


def test_dependency_manifest_does_not_expand_unselected_findings(ws,monkeypatch):
    sources=[]
    for n in range(3):
        source=ws.work_ref(f'round0/findings/{n}.md');receipt=source+'.verification.json'
        ws.write_text(source,f'# Theorem {n}\nLong proof {n}.');ws.write_json(receipt,{'PASS':True})
        sources.append({'finding_id':f'R{n:04d}','source_ref':source,'verification_ref':receipt,'ref':f'results/R{n}.md'})
    monkeypatch.setattr('pace.runtime.output.results.finding_entries',lambda _:sources)
    ref,_=write_dependency_manifest(workdir=ws.root,output_ref=ws.work_ref('round1/proof.md'),target_contract_id='TARGET',selected_dependencies=[sources[1]['source_ref']])
    entries=json.loads(ws.path(ref).read_text())['entries']
    assert len(entries)==1 and entries[0]['source_ref']==sources[1]['source_ref']
    assert 'source_text' not in entries[0] and 'verification_receipt' not in entries[0]


def test_old_policy_is_not_upgraded_during_setup_resume(ws):
    from pace.orchestrator.pipeline import ProofPipeline
    from pace.pipeline_types import PipelineConfig
    pipeline=ProofPipeline(PipelineConfig(output_root=ws.root.parent))
    pipeline._commit_execution_policies(ws,{'mode':'research','requested_mode':'research',
        'resource_variant':'standard','control_context':'fixed old policy'})
    assert 'research_memory_version' not in json.loads(ws.path(ws.work_ref('state/research_execution_policy.json')).read_text())


def test_pipeline_retrieval_is_survey_only(ws,monkeypatch):
    from pace.orchestrator.pipeline import ProofPipeline
    from pace.pipeline_types import PipelineConfig
    from types import SimpleNamespace
    settings=[]
    def build(**kw): settings.append(kw); return SimpleNamespace()
    monkeypatch.setattr('pace.orchestrator.pipeline.build_llm',build)
    for switch in ('on','off'):
        pipeline=ProofPipeline(PipelineConfig(output_root=ws.root.parent,web_search_mode=switch))
        pipeline._llm();pipeline._director_llm();pipeline._survey_llm()
        assert [r['web_search'] for r in settings[-3:]]==[False,False,switch=='on']


def _local_obligation(ws):
    from pace.contracts.research.memory import binding, digest
    from pace.runtime.research_context import load_index
    ref = ws.work_ref('round0/local-lemma.md')
    ws.write_text(ref, 'For the fixed hypotheses, establish the local lemma.')
    source = binding(ws.root, ref)
    index = load_index(ws.root)
    index['obligations']['lemma'] = {
        'id': 'lemma', 'kind': 'ROUTE_SUFFICIENT', 'statement_ref': ref,
        'scope_ref': ref, 'parent_ids': [], 'premise_groups': [], 'branch_id': 'b-lemma',
        'version': digest(source), 'bindings': [source], 'status': 'OPEN'}
    ws.write_json('memory/research_index.json', index)
    return ref


def test_new_assignment_rejects_changed_registered_obligation(ws):
    ref = _local_obligation(ws)
    ws.write_text(ref, 'The hypotheses were silently weakened.')
    with pytest.raises(ValueError, match='version changed'):
        freeze_context(ws.root, ws.work_ref('round1/assignment/task.json'),
                       task(obligation_ids=['lemma']), [])


def test_frozen_context_checks_nested_target_binding(ws):
    _local_obligation(ws)
    context = freeze_context(ws.root, ws.work_ref('round1/assignment/task.json'),
                             task(obligation_ids=['lemma']), [])
    target = ws.work_ref('input/target_contract.json')
    assert target not in context['required_refs']
    ws.write_json(target, {'contract_id':'TARGET', 'literal_target':'Different goal'})
    with pytest.raises(ValueError, match='version changed'):
        validate_context(ws.root, ws.work_ref('round1/assignment/context.json'))


def test_same_turn_target_mutation_does_not_reset_memory(ws):
    from pace.runtime.output.memory import initialize_research_memory
    original = ws.path('memory/research_index.json').read_bytes()
    ws.write_json(ws.work_ref('input/target_contract.json'), {'contract_id':'OTHER'})
    with pytest.raises(ValueError, match='target changed'):
        initialize_research_memory(ws.root)
    assert ws.path('memory/research_index.json').read_bytes() == original

@pytest.mark.parametrize("legacy", [False, True])
def test_saved_director_decision_survives_interrupted_parallel_reservation(ws, monkeypatch, legacy):
    from pace.orchestrator import director_parallel
    pair = replace(task("survey"), parallel_tasks=(task("research"),))
    def survey(**kw):
        ws.write_text(kw["output_ref"], "Source gap.")
        return {"agent_id": "survey", "status": "SUCCEEDED", "deliverable_ref": kw["output_ref"]}
    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: pair, composer=lambda **_: PromptComposerResult("Evaluate the sources.", "composer"),
                               specialists={"survey": survey}, target_contract_id="TARGET", max_rounds=1)
    with monkeypatch.context() as scoped:
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt
        scoped.setattr(director_parallel, "reserve_batch", interrupt)
        with pytest.raises(KeyboardInterrupt):
            engine.run()
    path = ws.path(ws.work_ref("round1/director_context.json"))
    saved = json.loads(path.read_text())
    if legacy:
        saved.pop("decision_inputs")
        path.write_text(json.dumps(saved))
    engine.director = lambda **_: pytest.fail("a saved decision must never be regenerated")
    assert engine.resume().phase == "FINALIZE"
    assert json.loads(path.read_text()) == saved


def test_director_reuses_frozen_brief_before_first_decision(ws):
    views = []
    def decide(**kw):
        views.append(kw["decision_inputs"])
        if len(views) == 1:
            raise KeyboardInterrupt
        return task("survey")
    def survey(**kw):
        ws.write_text(kw["output_ref"], "Source gap.")
        return {"agent_id": "survey", "status": "SUCCEEDED", "deliverable_ref": kw["output_ref"]}
    engine = DirectorLedEngine(workdir=ws.root, director=decide, composer=lambda **_: PromptComposerResult("Evaluate the sources.", "composer"),
                               specialists={"survey": survey}, max_rounds=1)
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    index = json.loads(ws.path("memory/research_index.json").read_text())
    index["costs"] = {"branches": {"b-target": {"logical_calls": 5}}}
    ws.write_json("memory/research_index.json", index)
    assert engine.resume().phase == "FINALIZE"
    assert views[0] == views[1]

@pytest.mark.parametrize("hypothesis_checked", [False, True])
def test_survey_informs_the_next_work_order_without_closing_obligation(ws, hypothesis_checked):
    from test_research_round_limit import write_delta

    survey_ref = ws.work_ref("round1/survey_deliverable.md")
    gap = "Prove B." if hypothesis_checked else "Check H before applying A to B."
    note = "Literature establishes A under H. " + ("H matches." if hypothesis_checked else "H is unverified.")
    def director(**kw):
        view = kw["decision_inputs"]
        if kw["round_index"] == 1:
            return task("survey")
        assert view["latest_survey"] == survey_ref and survey_ref in view["required_refs"]
        assert ws.path(survey_ref).read_text() == note and view["survey_remaining"] == 3
        return task("research", prompt_brief=gap, input_refs=[survey_ref])
    def author(**kw):
        if kw["agent_id"] == "survey":
            ws.write_text(kw["output_ref"], note)
        else:
            ws.write_text(kw["output_ref"], "The remaining implication has not been established.")
            write_delta(ws.root, ws.path(kw["output_ref"]))
        return {"agent_id": kw["agent_id"], "status": "SUCCEEDED", "deliverable_ref": kw["output_ref"]}
    engine = DirectorLedEngine(workdir=ws.root, director=director,
                               composer=lambda **_: PromptComposerResult("Inspect the stated gap.", "composer"),
                               specialists={"survey": author, "research": author}, max_rounds=2)
    assert engine.run().phase == "FINALIZE"
    assignment = json.loads(ws.path(ws.work_ref("round2/assignment/task.json")).read_text())
    assert assignment["prompt_brief"] == gap and survey_ref in assignment["input_refs"]
    index = json.loads(ws.path("memory/research_index.json").read_text())
    assert index["obligations"]["target"]["status"] == "OPEN" and not index["applications"]
