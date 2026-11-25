"""Manuscript-scoped reviews retain separately bound application decisions."""

import json
from pathlib import Path

import pytest

from pace.agents.director.context import freeze_context, load_index
from pace.agents.director.findings import (
    check_applications,
    check_stage_findings,
    pending_finding_checks,
)
from pace.artifacts.calls import begin_call
from pace.artifacts.events import append_event
from pace.contracts.research.memory import prepare_route
from pace.orchestrator import memory as memory_owner
from pace.orchestrator.memory import (
    commit_research_memory,
    rebuild_research_index,
    research_brief,
    update_research_costs,
)
from pace.orchestrator.results import publish_stage_findings
from pace.runtime.semantic import SemanticInterfaceFailure, StructuredSemanticResult
from test_research_memory import ws, deliver, task


def manuscript_handoff(ws, *, count=6, finding=True):
    planner = deliver(ws, 0, agent="planner")
    plan = str(Path(planner).with_name("research.md"))
    ids = ["target", *(f"lemma-{i}" for i in range(1, count))]
    ws.write_json(
        str(Path(planner).with_name("route_relations.json")),
        {
            "schema_version": "route_relations/v1",
            "change_ref": plan,
            "obligations": [
                {
                    "id": ident,
                    "kind": "ROUTE_SUFFICIENT",
                    "statement_ref": plan,
                    "scope_ref": plan,
                    "parent_ids": ["target"],
                    "branch_id": "b-target",
                    "premise_groups": [],
                }
                for ident in ids[1:]
            ],
        },
    )
    commit_research_memory(ws.root, [planner])
    folder = ws.work_ref("round1")
    manuscript = f"{folder}/findings/result.md" if finding else f"{folder}/research.md"
    result = f"{folder}/agent_result.json"
    freeze_context(
        ws.root, f"{folder}/assignment/task.json", task(obligation_ids=ids), []
    )
    ws.write_json(f"{folder}/assignment/task.json", {"round_index": 1})
    ws.write_text(
        manuscript,
        "# A complete theorem\n\nAn explicit derivation for each requested scope.",
    )
    ws.write_json(
        f"{folder}/research_delta.json",
        {
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
    )
    ws.write_json(
        result,
        {"agent_id": "research", "status": "SUCCEEDED", "deliverable_ref": manuscript},
    )
    ws.write_json(
        ws.work_ref("state/director_state.json"),
        {"completed_result_refs": [planner, result]},
    )
    commit_research_memory(ws.root, [planner, result])
    return manuscript, [planner, result]


def review_payload(request, rejected=()):
    return {
        "stage_status": "PASS",
        "reuse_authorized": True,
        "publication": {
            "action": "CREATE" if request["publication_requested"] else "NONE"
        },
        "application_checks": [
            {
                "application_key": item["application_key"],
                "obligation_id": item["obligation_id"],
                "obligation_version": item["obligation_version"],
                "status": "NOT_ESTABLISHED"
                if item["obligation_id"] in rejected
                else "APPLICABLE",
                "review_ref": request["review_ref"],
                **(
                    {"reuse_authorized": item["obligation_id"] not in rejected}
                    if request.get("separate_scope_decisions")
                    else {}
                ),
            }
            for item in request["applications"]
        ],
    }


def test_six_scopes_and_publication_share_one_review_with_independent_decisions(
    ws, monkeypatch
):
    manuscript, completed = manuscript_handoff(ws)
    calls = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        calls.append(request)
        ws.write_text(
            request["review_ref"],
            "The theorem is valid; lemma-2's additional application is not established.",
        )
        return StructuredSemanticResult(
            review_payload(request, rejected={"lemma-2"}), {}
        )

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    original = ws.path(manuscript).read_bytes()
    assert check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=completed
    )
    publish_stage_findings(workdir=ws.root, completed_result_refs=completed)
    assert len(calls) == 1 and len(calls[0]["applications"]) == 6
    index = rebuild_research_index(ws.root)
    assert index["obligations"]["lemma-2"]["status"] == "OPEN"
    assert all(
        app["status"]
        == ("NOT_ESTABLISHED" if app["obligation_id"] == "lemma-2" else "APPLICABLE")
        for app in index["applications"].values()
    )
    assert len({app["review_ref"] for app in index["applications"].values()}) == 1
    publication = json.loads(
        ws.path(manuscript).with_suffix(".verification.json").read_text()
    )
    assert (
        next(iter(index["applications"].values()))["review_ref"]
        == publication["joint_receipt_ref"]
    )
    assert (
        len(json.loads(ws.path("work/state/findings.json").read_text())["publications"])
        == 1
    )
    assert check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=completed
    )
    assert len(calls) == 1 and ws.path(manuscript).read_bytes() == original
    context = freeze_context(
        ws.root, ws.work_ref("round2/assignment/task.json"), task(), []
    )
    assert calls[0]["review_ref"] in context["required_refs"]


def test_new_premises_require_a_new_scoped_review_and_changed_sources_invalidate(
    ws, monkeypatch
):
    manuscript, completed = manuscript_handoff(ws, count=1, finding=False)
    calls = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        calls.append(request)
        ws.write_text(
            request["review_ref"],
            "Independent check of the exact premises and their application.",
        )
        return StructuredSemanticResult(review_payload(request), {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert check_applications(llm=object(), root=ws.root)
    memory_owner.refresh_application_memory(ws.root)
    second = deliver(ws, 2, application=True)
    premise = ws.work_ref("input/new-premise.md")
    ws.write_text(premise, "A genuinely additional mathematical premise.")
    delta_ref = str(Path(second).with_name("research_delta.json"))
    delta = json.loads(ws.path(delta_ref).read_text())
    delta["applications"][0].update(argument_ref=manuscript, premise_refs=[premise])
    ws.write_json(delta_ref, delta)
    commit_research_memory(ws.root, [*completed, second])
    assert check_applications(llm=object(), root=ws.root)
    memory_owner.refresh_application_memory(ws.root)
    assert len(calls) == 2 and len(calls[-1]["applications"]) == 1
    assert not calls[-1]["publication_requested"]
    ws.write_text(manuscript, "A changed proof cannot inherit the earlier receipt.")
    memory_owner.refresh_application_memory(ws.root)
    assert all(
        app["status"] == "INVALIDATED"
        for app in load_index(ws.root)["applications"].values()
    )
    assert load_index(ws.root)["obligations"]["target"]["status"] == "OPEN"


def test_reporter_nominates_the_original_for_both_application_and_publication(
    ws, monkeypatch
):
    from pace.agents.reporter import run_research_reporter
    from test_research_reporter import NativeFixture, use_reporter

    use_reporter(ws)
    result_ref = deliver(ws, 1)
    manuscript = str(Path(result_ref).with_name("research.md"))
    llm = NativeFixture("research")
    original_request = llm._request

    def nominate(messages, tools):
        response = original_request(messages, tools)
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        payload = json.loads(function["arguments"])
        payload["finding_refs"] = [manuscript]
        payload["delta"]["applications"] = [
            {
                "id": "original-result",
                "obligation_id": "target",
                "argument_ref": manuscript,
                "premise_refs": [],
            }
        ]
        function["arguments"] = json.dumps(payload)
        return response

    monkeypatch.setattr(llm, "_request", nominate)
    run_research_reporter(llm=llm, workdir=ws.root, result_ref=result_ref)
    commit_research_memory(ws.root, [result_ref])
    reviews = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        reviews.append(request)
        ws.write_text(
            request["review_ref"],
            "One independent review of the original theorem and its application.",
        )
        return StructuredSemanticResult(review_payload(request), {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=[result_ref]
    )
    publish_stage_findings(workdir=ws.root, completed_result_refs=[result_ref])
    assert len(reviews) == 1 and reviews[0]["publication_requested"]
    publication = json.loads(ws.path("work/state/findings.json").read_text())[
        "publications"
    ]
    assert len(publication) == 1 and publication[0]["source_ref"] == manuscript
    assert not ws.path(str(Path(result_ref).parent / "findings")).exists()


def test_different_manuscripts_keep_independent_reviews_of_the_same_obligation(
    ws, monkeypatch
):
    first, completed = manuscript_handoff(ws, count=1, finding=False)
    second_result = deliver(ws, 2, application=True)
    second = str(Path(second_result).with_name("research.md"))
    ws.write_text(
        second, "An alternative argument with different mathematical hypotheses."
    )
    commit_research_memory(ws.root, [*completed, second_result])
    reviewed = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        reviewed.append(request["argument_ref"])
        ws.write_text(
            request["review_ref"],
            "Independent review of this manuscript's own assumptions.",
        )
        return StructuredSemanticResult(
            review_payload(
                request,
                rejected={"target"} if request["argument_ref"] == second else (),
            ),
            {},
        )

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert check_applications(llm=object(), root=ws.root)
    index = rebuild_research_index(ws.root)
    assert sorted(reviewed) == sorted([first, second])
    assert index["obligations"]["target"]["status"] == "CLOSED"
    assert {app["status"] for app in index["applications"].values()} == {
        "APPLICABLE",
        "NOT_ESTABLISHED",
    }


def test_batch_acceptance_survives_semantic_completion_interruption(ws, monkeypatch):
    from pace.runtime.chat_api import ChatCompletionsLLM
    from test_research_reporter import NativeFixture

    _, completed = manuscript_handoff(ws)
    llm = ChatCompletionsLLM(model="fixture")
    calls = []

    def reply(messages, tools):
        calls.append(messages)
        request = json.loads(
            next(
                ws.root.glob("work/turn001/round1/verification/*.request.json")
            ).read_text()
        )
        if messages[-1]["role"] == "user":
            return NativeFixture.tool(
                "write_file",
                {
                    "path": request["review_ref"],
                    "content": "Six explicit, independently checked applications.",
                },
            )
        return NativeFixture.tool("submit_result", review_payload(request))

    monkeypatch.setattr(llm, "_request", reply)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "pace.runtime.semantic.finish_call",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            check_stage_findings(
                llm=llm, workdir=ws.root, completed_result_refs=completed
            )
    assert len(calls) == 2
    assert check_stage_findings(
        llm=llm, workdir=ws.root, completed_result_refs=completed
    )
    assert len(calls) == 2
    assert all(
        app["status"] == "APPLICABLE"
        for app in rebuild_research_index(ws.root)["applications"].values()
    )


def test_batch_verifier_error_stays_pending_and_resume_retries_once(ws, monkeypatch):
    _, completed = manuscript_handoff(ws)
    calls = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        calls.append(request)
        if len(calls) == 1:
            return SemanticInterfaceFailure(
                host={}, diagnostics=({"reason_code": "inference_interrupted"},)
            )
        ws.write_text(
            request["review_ref"], "The resumed batch has been independently checked."
        )
        return StructuredSemanticResult(review_payload(request), {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert not check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=completed
    )
    assert pending_finding_checks(ws.root)
    assert not check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=completed
    )
    assert len(calls) == 1
    assert check_stage_findings(
        llm=object(),
        workdir=ws.root,
        completed_result_refs=completed,
        retry_errors=True,
    )
    assert len(calls) == 2
    assert all(
        app["status"] == "APPLICABLE"
        for app in rebuild_research_index(ws.root)["applications"].values()
    )


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "wrong_version", "unauthorized"]
)
def test_batch_receipt_cannot_authorize_an_unchecked_scope(ws, damage):
    from pace.agents.finding_verifier import validate_finding_application

    manuscript_handoff(ws, count=2)
    apps = list(load_index(ws.root)["applications"].values())
    request = {
        "review_ref": ws.work_ref("round1/verification/review.md"),
        "publication_requested": False,
        "applications": apps,
    }
    ws.write_text(request["review_ref"], "Exact scope evidence.")
    payload = review_payload(request)
    if damage == "missing":
        payload["application_checks"].pop()
    elif damage == "duplicate":
        payload["application_checks"][1] = payload["application_checks"][0]
    elif damage == "wrong_version":
        payload["application_checks"][0]["obligation_version"] = "another-scope"
    else:
        payload["reuse_authorized"] = False
    assert validate_finding_application(payload, request, ws.root)


def test_replay_commit_before_index_without_repeating_author(ws, monkeypatch):
    ref = deliver(ws, 1)
    import pace.artifacts.workspace as persistence

    write = persistence._write_json_atomic

    def interrupt(path, value):
        if path.name == "research_index.json" and value["commits"]:
            raise KeyboardInterrupt()
        write(path, value)

    with monkeypatch.context() as scoped:
        scoped.setattr(persistence, "_write_json_atomic", interrupt)
        with pytest.raises(KeyboardInterrupt):
            commit_research_memory(ws.root, [ref])
    assert ws.path(str(Path(ref).with_name("memory_commit.json"))).is_file()
    assert not load_index(ws.root)["commits"]
    commit_research_memory(ws.root, [ref])
    assert len(load_index(ws.root)["commits"]) == 1


def test_root_target_cannot_be_redefined_by_planner(ws):
    ref = ws.work_ref("round1/route_relations.json")
    target = ws.work_ref("input/target_contract.json")
    ws.write_json(
        ref,
        {
            "schema_version": "route_relations/v1",
            "change_ref": target,
            "obligations": [
                {
                    "id": "target",
                    "kind": "TARGET_REQUIRED",
                    "statement_ref": target,
                    "scope_ref": target,
                    "branch_id": "b-target",
                    "parent_ids": [],
                    "premise_groups": [],
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="root target"):
        prepare_route(ws.root, ref, load_index(ws.root)["obligations"])


def test_costs_use_transport_intervals_once_and_report_missing_time(ws):
    deliver(ws, 1)
    for name in ("finished", "unobserved"):
        ref = begin_call(
            ws.root,
            call_id=name,
            role="RESEARCH",
            stage="test",
            prompt="Work",
            metadata={"round_index": 1},
        )
        append_event(ws.root, "SEMANTIC_CALL_STARTED", call_id=name, call_ref=ref)
    append_event(ws.root, "TRANSPORT_TIMING", call_id="finished", elapsed_seconds=3.5)
    append_event(ws.root, "PROFILE_SPAN", call_id="finished", elapsed_seconds=9)
    ws.write_json(
        ws.work_ref("state/sessions.json"),
        [
            {
                "ended_at": "2026-11-17",
                "elapsed_seconds": 5,
                "gap_since_previous_session_seconds": 200,
            },
            {"status": "end_unobserved", "elapsed_seconds": 0},
        ],
    )
    update_research_costs(ws.root)
    update_research_costs(ws.root)
    costs = load_index(ws.root)["costs"]
    branch = costs["branches"]["b-target"]
    assert branch["cumulative_call_seconds"] == 3.5
    assert branch["missing_timing_calls"] == 1 and branch["logical_calls"] == 2
    assert (
        costs["completed_session_seconds"] == 5
        and not costs["session_measurement_complete"]
    )


def test_retired_obligation_stays_retired_after_review_refresh(ws):
    from pace.orchestrator.memory import refresh_application_memory

    ref = deliver(ws, 1, agent="planner")
    original = ws.work_ref("round1/research.md")
    ws.write_json(
        ws.work_ref("round1/route_relations.json"),
        {
            "schema_version": "route_relations/v1",
            "change_ref": original,
            "obligations": [
                {
                    "id": "old-route",
                    "kind": "ROUTE_SUFFICIENT",
                    "statement_ref": original,
                    "scope_ref": original,
                    "branch_id": "b-route",
                    "parent_ids": ["target"],
                    "premise_groups": [],
                    "disposition": "SUPERSEDED",
                }
            ],
        },
    )
    commit_research_memory(ws.root, [ref])
    refresh_application_memory(ws.root)
    assert load_index(ws.root)["obligations"]["old-route"]["status"] == "SUPERSEDED"
    assert load_index(ws.root)["obligations"]["target"]["status"] == "OPEN"


def test_cross_task_source_requires_committed_owner_in_either_mode(ws):
    from pace.contracts.research.memory import binding, check_source_ownership

    original = ws.work_ref("round2/proof.md")
    owner = ws.work_ref("round2/agent_result.json")
    ws.write_text(original, "Complete original proof.")
    ws.write_json(ws.work_ref("round2/assignment/task.json"), {"round_index": 2})
    ws.write_json(owner, {"status": "SUCCEEDED", "deliverable_ref": original})
    row = binding(ws.root, original)
    with pytest.raises(ValueError, match="uncommitted"):
        check_source_ownership(ws.root, row)
    ws.write_json(ws.work_ref("state/proof_state.json"), {"completed_refs": [original]})
    check_source_ownership(ws.root, row)


def test_other_handoff_nominates_same_manuscript_for_joint_review(ws, monkeypatch):
    manuscript, completed = manuscript_handoff(ws, count=1, finding=False)
    later = deliver(ws, 2)
    owner = Path(later).parent
    ws.write_json(
        str(owner / "reporter_result.json"), {"payload": {"finding_refs": [manuscript]}}
    )
    evidence = str(owner / "proof-details.md")
    ws.write_text(
        evidence, "Additional evidence required to review the complete manuscript."
    )
    ws.write_json(
        str(owner / "assignment/task.json"),
        {"round_index": 2, "input_refs": [evidence]},
    )
    calls = []

    def verify(**kwargs):
        calls.append(kwargs)
        request = kwargs["application_request"]
        assert request["publication_requested"] and evidence in kwargs["input_refs"]
        ws.write_text(
            request["review_ref"], "Independent manuscript and scope decisions."
        )
        return StructuredSemanticResult(review_payload(request), {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    for _ in range(2):
        assert check_stage_findings(
            llm=object(), workdir=ws.root, completed_result_refs=[*completed, later]
        )
    assert len(calls) == 1


def test_partial_review_repairs_only_unfinished_scope_after_interruption(
    ws, monkeypatch
):
    from pace.agents import finding_verifier
    from pace.agents.director import findings

    manuscript, completed = manuscript_handoff(ws, count=3)
    calls = []

    def verify(**kw):
        request = kw["application_request"]
        calls.append(request)
        ws.write_text(
            request["review_ref"],
            "Reviewed the assigned statements under their own premises.",
        )
        payload = review_payload(request)
        if len(calls) == 1:
            for check in payload["application_checks"]:
                if check["obligation_id"] == "lemma-2":
                    check.update(status="VERIFIER_ERROR", reuse_authorized=False)
        else:
            assert not request["publication_requested"]
            assert [a["obligation_id"] for a in request["applications"]] == ["lemma-2"]
            assert manuscript.replace(".md", ".verification.json") in kw["input_refs"]
        assert not finding_verifier.validate_finding_application(
            payload, request, ws.root
        )
        kw["on_accept"](payload)
        return StructuredSemanticResult(payload, {})

    monkeypatch.setattr(findings, "verify_finding", verify)

    def run(retry=False):
        return check_stage_findings(
            llm=object(),
            workdir=ws.root,
            completed_result_refs=completed,
            retry_errors=retry,
        )

    assert not run()
    receipt = ws.path(manuscript).with_suffix(".verification.json")
    original = receipt.read_bytes()
    index = rebuild_research_index(ws.root)
    assert index["obligations"]["lemma-2"]["status"] == "OPEN"
    assert index["obligations"]["target"]["status"] == "CLOSED"
    assert not run() and len(calls) == 1
    commit = findings._commit_application_reviews
    with monkeypatch.context() as scoped:

        def interrupt(root, request, ref):
            if request.get("retry_of"):
                raise KeyboardInterrupt()
            return commit(root, request, ref)

        scoped.setattr(findings, "_commit_application_reviews", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(True)
    assert run(True) and run()
    assert len(calls) == 2 and receipt.read_bytes() == original
    assert (
        rebuild_research_index(ws.root)["obligations"]["lemma-2"]["status"] == "CLOSED"
    )


def test_same_obligation_with_different_premises_keeps_two_decisions(ws, monkeypatch):
    from pace.contracts.research.memory import prepare_delta_payload
    from pace.agents.finding_verifier import validate_finding_application

    manuscript, completed = manuscript_handoff(ws, count=1)
    later = deliver(ws, 2)
    premise = ws.work_ref("input/stronger.md")
    ws.write_text(premise, "The additional regularity assumption.")
    delta_ref = str(Path(later).with_name("research_delta.json"))
    delta = json.loads(ws.path(delta_ref).read_text())
    delta["applications"] = [
        {
            "id": name,
            "obligation_id": "target",
            "argument_ref": manuscript,
            "premise_refs": refs,
        }
        for name, refs in (
            ("weak", []),
            ("strong", [premise]),
            ("strong-again", [premise]),
        )
    ]
    ws.write_json(delta_ref, delta)
    prepared = prepare_delta_payload(
        ws.root, delta, load_index(ws.root)["obligations"], local_result_ref=later
    )
    assert len(prepared["applications"]) == 2
    commit_research_memory(ws.root, [*completed, later])

    def verify(**kw):
        request = kw["application_request"]
        ws.write_text(
            request["review_ref"],
            "Only the strengthened premises justify this application.",
        )
        payload = review_payload(request)
        payload.update(
            stage_status="MATH_FAIL",
            reuse_authorized=False,
            publication={"action": "NONE"},
        )
        for row, item in zip(
            payload["application_checks"], request["applications"], strict=True
        ):
            if not item["premise_refs"]:
                row.update(status="NOT_ESTABLISHED", reuse_authorized=False)
        assert not validate_finding_application(payload, request, ws.root)
        return StructuredSemanticResult(payload, {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=[*completed, later]
    )
    apps = rebuild_research_index(ws.root)["applications"].values()
    assert sorted((app["premise_refs"], app["status"]) for app in apps) == [
        ([], "NOT_ESTABLISHED"),
        ([premise], "APPLICABLE"),
    ]
    publish_stage_findings(workdir=ws.root, completed_result_refs=completed)
    assert not ws.path("results/findings/R0001.md").exists()


def test_unfinished_manuscript_retries_without_reviewing_established_scope(
    ws, monkeypatch
):
    from pace.agents.finding_verifier import validate_finding_application

    manuscript, completed = manuscript_handoff(ws, count=1)
    jobs = []

    def verify(**kw):
        request = kw.get("application_request")
        jobs.append(request is not None)
        if request:
            ws.write_text(
                request["review_ref"],
                "The local implication holds; the whole proof needs review.",
            )
            payload = review_payload(request)
            payload.update(
                stage_status="VERIFIER_ERROR",
                reuse_authorized=False,
                publication={"action": "NONE"},
            )
            assert not validate_finding_application(payload, request, ws.root)
        else:
            payload = {
                "stage_status": "PASS",
                "reuse_authorized": True,
                "publication": {"action": "CREATE"},
            }
        return StructuredSemanticResult(payload, {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)

    def run(retry=False):
        return check_stage_findings(
            llm=object(),
            workdir=ws.root,
            completed_result_refs=completed,
            retry_errors=retry,
        )

    assert not run()
    index = rebuild_research_index(ws.root)
    app = next(iter(index["applications"].values()))
    assert app["status"] == "APPLICABLE"
    scoped_receipt = ws.path(app["review_ref"]).read_bytes()
    assert run(True) and run()
    assert jobs == [True, False]
    assert ws.path(app["review_ref"]).read_bytes() == scoped_receipt
    assert (
        json.loads(ws.path(manuscript).with_suffix(".verification.json").read_text())[
            "verdict"
        ]["stage_status"]
        == "PASS"
    )


def test_changed_group_replays_accepted_receipt_before_scheduling_new_scope(
    ws, monkeypatch
):
    manuscript, completed = manuscript_handoff(ws, count=2)
    batches = []

    def verify(**kw):
        req = kw["application_request"]
        batches.append({item["application_key"] for item in req["applications"]})
        ws.write_text(req["review_ref"], "Each use checked under its own premises.")
        payload = review_payload(req)
        kw["on_accept"](payload)
        if len(batches) == 1:
            raise KeyboardInterrupt()
        return StructuredSemanticResult(payload, {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    with pytest.raises(KeyboardInterrupt):
        check_stage_findings(
            llm=object(), workdir=ws.root, completed_result_refs=completed
        )
    extra = deliver(ws, 2, application=True)
    delta_ref = str(Path(extra).with_name("research_delta.json"))
    delta = json.loads(ws.path(delta_ref).read_text())
    premise = ws.work_ref("input/additional.md")
    ws.write_text(premise, "An additional premise.")
    delta["applications"][0].update(argument_ref=manuscript, premise_refs=[premise])
    ws.write_json(delta_ref, delta)
    commit_research_memory(ws.root, [*completed, extra])
    for _ in range(2):
        assert check_stage_findings(
            llm=object(), workdir=ws.root, completed_result_refs=[*completed, extra]
        )
    assert list(map(len, batches)) == [2, 1]
    assert batches[0].isdisjoint(batches[1])
    assert all(
        app["status"] == "APPLICABLE"
        for app in rebuild_research_index(ws.root)["applications"].values()
    )


def test_joint_review_excludes_critic_sources_from_dispatch_and_handoff(ws, monkeypatch):
    manuscript, completed = manuscript_handoff(ws, count=2)
    result_path = ws.path(completed[-1])
    folder = Path(completed[-1]).parent
    assignment = result_path.parent / "assignment/task.json"
    note = str(folder / "rci/attempt_001/candidate_01/raw_seed_critic_math/review.md")
    dependency = str(folder / "lemma.md")
    ws.write_text(note, "Private exploratory derivation.")
    ws.write_text(dependency, "An independently checkable premise.")
    result = json.loads(result_path.read_text())
    result.update(agent_id="rci", manuscript_refs=[manuscript], published_refs=[note, manuscript],
                  manuscript_dependencies={manuscript: [dependency]}, critic_derivations={manuscript: [note]})
    ws.write_json(completed[-1], result)
    ticket = json.loads(assignment.read_text())
    assignment.write_text(json.dumps({**ticket, "input_refs": [note]}))
    calls = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        assert note not in json.dumps(request) and note not in kwargs["input_refs"]
        assert dependency in kwargs["input_refs"] and manuscript in kwargs["input_refs"]
        calls.append(request)
        ws.write_text(request["review_ref"], "Second scope needs a separate implication.")
        return StructuredSemanticResult(review_payload(request, rejected={"lemma-1"}), {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    for _ in range(2):
        assert check_stage_findings(llm=object(), workdir=ws.root, completed_result_refs=completed)
    assert len(calls) == 1
