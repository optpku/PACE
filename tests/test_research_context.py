"""Frozen dependencies, concurrent planning and limited model input views."""

from pace.orchestrator.memory import commit_round_memory
from pace.orchestrator.results import publish_stage_findings
import json
from dataclasses import replace
from pathlib import Path

import pytest

from test_research_memory import ws, task
from pace.agents.director.context import freeze_context, validate_context
from pace.artifacts.workspace import work_ref
from pace.agents.director.loop import DirectorLedEngine
from pace.agents.prompt_composer import PromptComposerResult
from pace.artifacts.results import write_dependency_manifest
from pace.agents.rci.context import select_inputs


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


def test_parallel_inputs_frozen_before_planner_finishes(ws, monkeypatch):
    from threading import Event
    from pace.artifacts.context import empty_search_progress
    from pace.artifacts.memory import note_ref
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
        target_contract_id='TARGET',max_rounds=2, commit_memory=commit_round_memory, commit_results=publish_stage_findings)
    def interrupt_after_parent_commit(state):
        assert state.phase == 'DIRECTOR' and state.last_batch_result_ref
        raise KeyboardInterrupt
    with monkeypatch.context() as scoped:
        scoped.setattr(engine, '_publish_round_progress', interrupt_after_parent_commit)
        with pytest.raises(KeyboardInterrupt):
            engine.run()
    refs = [ws.work_ref(f'round{i}/agent_result.json') for i in (1, 2)]
    manifest = json.loads(ws.path(ws.work_ref('round1/batch.json')).read_text())
    common = ws.work_ref('round1/progress_snapshot.json')
    for i, row in enumerate(manifest['contexts'], 1):
        assignment = ws.work_ref(f'round{i}/assignment')
        ctx = json.loads(ws.path(f'{assignment}/context.json').read_text())
        ticket = json.loads(ws.path(f'{assignment}/task.json').read_text())
        expected = True
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
        ws.write_text(ref.replace(".verification.json", ".md"),
                      "The original manuscript with a rejected application.")
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
    monkeypatch.setattr('pace.artifacts.results.finding_entries',lambda _:sources)
    ref,_=write_dependency_manifest(workdir=ws.root,output_ref=ws.work_ref('round1/proof.md'),target_contract_id='TARGET',selected_dependencies=[sources[1]['source_ref']])
    entries=json.loads(ws.path(ref).read_text())['entries']
    assert len(entries)==1 and entries[0]['source_ref']==sources[1]['source_ref']
    assert 'source_text' not in entries[0] and 'verification_receipt' not in entries[0]


def test_pipeline_retrieval_is_survey_only(ws,monkeypatch):
    from pace.pipeline import ProofPipeline
    from pace.pipeline_types import PipelineConfig
    from types import SimpleNamespace
    settings=[]
    def build(**kw): settings.append(kw); return SimpleNamespace()
    monkeypatch.setattr('pace.agents.director.harness.build_llm',build)
    for switch in ('on','off'):
        pipeline=ProofPipeline(PipelineConfig(output_root=ws.root.parent,web_search_mode=switch)).director
        pipeline._llm();pipeline._director_llm();pipeline._survey_llm()
        assert [r['web_search'] for r in settings[-3:]]==[False,False,switch=='on']


def _local_obligation(ws):
    from pace.contracts.research.memory import binding, digest
    from pace.agents.director.context import load_index
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
    from pace.orchestrator.memory import initialize_research_memory
    original = ws.path('memory/research_index.json').read_bytes()
    ws.write_json(ws.work_ref('input/target_contract.json'), {'contract_id':'OTHER'})
    with pytest.raises(ValueError, match='target changed'):
        initialize_research_memory(ws.root)
    assert ws.path('memory/research_index.json').read_bytes() == original

def test_saved_director_decision_survives_interrupted_parallel_reservation(ws, monkeypatch):
    from pace.agents.director import parallel as director_parallel
    pair = replace(task("survey"), parallel_tasks=(task("research"),))
    def survey(**kw):
        ws.write_text(kw["output_ref"], "Source gap.")
        return {"agent_id": "survey", "status": "SUCCEEDED", "deliverable_ref": kw["output_ref"]}
    engine = DirectorLedEngine(workdir=ws.root, director=lambda **_: pair, composer=lambda **_: PromptComposerResult("Evaluate the sources.", "composer"),
                               specialists={"survey": survey}, target_contract_id="TARGET", max_rounds=1, commit_memory=commit_round_memory, commit_results=publish_stage_findings)
    with monkeypatch.context() as scoped:
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt
        scoped.setattr(director_parallel, "reserve_batch", interrupt)
        with pytest.raises(KeyboardInterrupt):
            engine.run()
    path = ws.path(ws.work_ref("round1/director_context.json"))
    saved = json.loads(path.read_text())
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
                               specialists={"survey": survey}, max_rounds=1, commit_memory=commit_round_memory, commit_results=publish_stage_findings)
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
                               specialists={"survey": author, "research": author}, max_rounds=2, commit_memory=commit_round_memory, commit_results=publish_stage_findings)
    assert engine.run().phase == "FINALIZE"
    assignment = json.loads(ws.path(ws.work_ref("round2/assignment/task.json")).read_text())
    assert assignment["prompt_brief"] == gap and survey_ref in assignment["input_refs"]
    index = json.loads(ws.path("memory/research_index.json").read_text())
    assert index["obligations"]["target"]["status"] == "OPEN" and not index["applications"]


def test_critic_derivations_follow_only_selected_work_and_never_enter_review(ws):
    from pace.agents.reporter import reporter_schema
    from pace.artifacts.workspace import is_rci_critic_material
    from pace.contracts.research.memory import binding, prepare_delta_payload
    from pace.runtime.chat_api import WorkspaceTools
    from test_research_memory import deliver

    owner = deliver(ws, 1, agent="rci")
    folder = Path(owner).parent
    source = str(folder / "rci/attempt_001/candidate_01/frontier_explorer/closure_brief.md")
    note = str(folder / "rci/attempt_001/candidate_01/raw_seed_critic_math/review.md")
    sibling = note.replace("candidate_01", "candidate_02")
    for ref in (source, note, sibling):
        ws.write_text(ref, ref)
    result = json.loads(ws.path(owner).read_text())
    result.update(manuscript_refs=[source], critic_derivations={source: [note], "other": [sibling]})
    ws.write_json(owner, result)
    route = str(folder / "route.md")
    ws.write_text(route, "Previous canonical route.")
    ws.write_json(ws.work_ref("state/current_route.json"), {"ref": route})
    assignment = ws.work_ref("round2/assignment/task.json")
    decision = task("prover", input_refs=[source])
    ctx = freeze_context(ws.root, assignment, decision, [source])
    assert {route, note} <= set(ctx["archive_refs"])
    assert source in ctx["required_refs"] and route not in ctx["required_refs"]
    assert sibling not in ctx["archive_refs"]
    frozen = json.loads(ws.path(ctx["route_context"]["ref"]).read_text())
    assert frozen["critic_derivations"] == {source: [note]}
    assert frozen["work_order"] == decision.prompt_brief
    ws.write_json(ws.work_ref("state/current_route.json"), {})
    assert freeze_context(ws.root, assignment, decision, []) == ctx

    schema = reporter_schema({"original_refs": [source, note], "manuscript_refs": [source],
                              "bindings": [binding(ws.root, ref) for ref in (note, source)]})
    app = schema["properties"]["delta"]["properties"]["applications"]["items"]["properties"]
    assert app["argument_ref"]["enum"] == app["premise_refs"]["items"]["enum"] == [source]
    assert schema["properties"]["finding_refs"]["items"]["enum"] == [source]
    verifier = WorkspaceTools(ws.root, {"role": "FINDING_VERIFIER", "input_refs": [note, source]})
    assert verifier.readable(source) and not verifier.readable(note)
    assert is_rci_critic_material(f"{folder}/rci/attempt_001/source_snapshot/{note}")
    assert is_rci_critic_material(f"{folder}/rci/attempt_001/scope_reviews.json")
    delta = json.loads(ws.path(str(folder / "research_delta.json")).read_text())
    for argument, premises in [(source, [note]), (note, [])]:
        delta["applications"] = [{"id": "a", "obligation_id": "target",
                                  "argument_ref": argument, "premise_refs": premises}]
        with pytest.raises(ValueError, match="Critic material"):
            prepare_delta_payload(ws.root, delta, {"target": frozen["obligations"][0]}, local_result_ref=owner)
