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
from test_research_memory import ws, deliver, record_report, task


def manuscript_handoff(ws, *, count=6, finding=True, dependencies=()):
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
        ws.root, f"{folder}/assignment/task.json", task(obligation_ids=ids, next_step_kind="REASSESS"), []
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
        {
            "agent_id": "research", "status": "SUCCEEDED", "deliverable_ref": manuscript,
            "manuscript_dependencies": {manuscript: list(dependencies)},
        },
    )
    ws.write_json(
        ws.work_ref("state/director_state.json"),
        {"completed_result_refs": [planner, result]},
    )
    record_report(ws, result, [manuscript] if finding else [])
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
                "reuse_authorized": item["obligation_id"] not in rejected,
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
    assert (
        json.loads(ws.path(context["route_context"]["ref"]).read_text())[
            "review_obligation_ids"
        ]
        == []
    )
    reassess = freeze_context(
        ws.root,
        ws.work_ref("round3/assignment/task.json"),
        task(next_step_kind="REASSESS"),
        [],
    )
    assert json.loads(ws.path(reassess["route_context"]["ref"]).read_text())[
        "review_obligation_ids"
    ] == ["target"]


def test_new_premises_require_a_new_scoped_review_and_changed_sources_invalidate(
    ws, monkeypatch
):
    manuscript, completed = manuscript_handoff(ws, count=1, finding=True)
    calls = []
    summary = "The original bound holds under the listed assumptions."
    prior_receipt = str(Path(manuscript).with_suffix(".verification.json"))

    def verify(**kwargs):
        request = kwargs["application_request"]
        calls.append(request)
        assert prior_receipt not in kwargs["input_refs"]
        ws.write_text(
            request["review_ref"],
            "Independent check of the exact premises and their application.",
        )
        return StructuredSemanticResult(
            {**review_payload(request), "summary": summary}, {}
        )

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert check_stage_findings(
        llm=object(), workdir=ws.root, completed_result_refs=completed
    )
    memory_owner.refresh_application_memory(ws.root)
    second = deliver(ws, 2, application=True, next_step_kind="REASSESS")
    premise = ws.work_ref("input/new-premise.md")
    ws.write_text(premise, "A genuinely additional mathematical premise.")
    delta_ref = str(Path(second).with_name("research_delta.json"))
    delta = json.loads(ws.path(delta_ref).read_text())
    delta["applications"][0].update(argument_ref=manuscript, premise_refs=[premise])
    ws.write_json(delta_ref, delta)
    record_report(ws, second)
    commit_research_memory(ws.root, [*completed, second])
    assert check_applications(llm=object(), root=ws.root)
    memory_owner.refresh_application_memory(ws.root)
    assert len(calls) == 2 and len(calls[-1]["applications"]) == 1
    assert not calls[-1]["publication_requested"]
    assert calls[-1]["checked_statement"] == summary
    assert calls[-1]["prior_review"]["ref"] == prior_receipt
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
    from test_research_reporter import NativeFixture

    result_ref = deliver(ws, 1, reported=False)
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
    record_report(ws, second_result)
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
        assert request["compact_review"]
        model_view = "\n".join(message.get("content") or "" for message in messages)
        assert not any(
            item["application_key"] in model_view for item in request["applications"]
        )
        assert not any(
            row["sha256"] in model_view
            for item in request["applications"]
            for row in item["bindings"]
        )
        payload = {
            "stage_status": "PASS",
            "reuse_authorized": True,
            "summary": "The bound follows under the exact hypotheses in the original.",
            "publication": {"action": "CREATE"},
            "application_checks": [
                {
                    "application": i,
                    "status": "NOT_ESTABLISHED" if i == 2 else "APPLICABLE",
                    "reason": "Check the required conditions for this scope.",
                }
                for i in range(1, 7)
            ],
        }
        return NativeFixture.tool("submit_result", payload)

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
    assert len(calls) == 1
    assert check_stage_findings(
        llm=llm, workdir=ws.root, completed_result_refs=completed
    )
    assert len(calls) == 1
    apps = rebuild_research_index(ws.root)["applications"].values()
    assert sum(app["status"] == "APPLICABLE" for app in apps) == 5
    review = next(ws.root.glob("work/turn001/round1/verification/finding-*.md"))
    assert "NOT_ESTABLISHED" in review.read_text()


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
        del payload["application_checks"][0]["reuse_authorized"]
    assert validate_finding_application(payload, request, ws.root)


def test_old_unbound_scope_receipt_cannot_authorize_a_use(ws):
    from pace.contracts.research.memory import application_is_approved, load_application_review

    manuscript_handoff(ws, count=1)
    app, = load_index(ws.root)["applications"].values()
    review_ref = ws.work_ref("round1/verification/review.md")
    check = {k: app[k] for k in ("application_key", "obligation_id", "obligation_version")}
    record = {"verdict": {"stage_status": "PASS", "reuse_authorized": True,
                          "application_checks": [{**check, "status": "APPLICABLE", "review_ref": review_ref}]},
              "review_binding": {"ref": review_ref}}
    assert not application_is_approved(app, record)
    link = Path(app["result_ref"]).parent / "verification" / f"application-{app['application_key']}.json"
    ws.write_json(str(link), record)
    with pytest.raises(ValueError, match="no bound manuscript verification"):
        load_application_review(ws.root, app)


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
        str(owner / "reporter_result.json"), {"result_ref": later, "payload": {"finding_refs": [manuscript]}}
    )
    evidence = str(owner / "proof-details.md")
    ws.write_text(
        evidence, "Additional evidence required to review the complete manuscript."
    )
    irrelevant = str(owner / "old_route.md")
    ws.write_text(irrelevant, "An old planning file assigned to the author.")
    # An explicitly declared dependency survives; unrelated dispatch input does not.
    result = json.loads(ws.path(later).read_text())
    result["manuscript_dependencies"] = {manuscript: [evidence]}
    ws.write_json(later, result)
    ws.write_json(
        str(owner / "assignment/task.json"),
        {"round_index": 2, "input_refs": [irrelevant]},
    )
    calls = []

    def verify(**kwargs):
        calls.append(kwargs)
        request = kwargs["application_request"]
        assert request["publication_requested"] and evidence in kwargs["input_refs"]
        assert irrelevant not in kwargs["input_refs"]
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
    premise_review = str(Path(premise).with_suffix(".verification.json"))
    ws.write_json(premise_review, {"conclusion": "The stronger assumption holds."})
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
    record_report(ws, later)
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
    ws.write_json(premise_review, {"conclusion": "The stronger assumption is unproved."})
    memory_owner.refresh_application_memory(ws.root)
    apps = load_index(ws.root)["applications"].values()
    assert sorted((app["premise_refs"], app["status"]) for app in apps) == [
        ([], "NOT_ESTABLISHED"), ([premise], "INVALIDATED"),
    ]


def test_unfinished_manuscript_retries_without_reviewing_established_scope(
    ws, monkeypatch
):
    from pace.agents.finding_verifier import validate_finding_application

    evidence = ws.work_ref("input/lemma.md")
    ws.write_text(evidence, "A necessary lemma with narrower hypotheses.")
    manuscript, completed = manuscript_handoff(ws, count=1, dependencies=[evidence])
    jobs = []

    def verify(**kw):
        request = kw.get("application_request")
        jobs.append(request is not None)
        assert evidence in kw["input_refs"]
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
    record_report(ws, extra)
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


def test_joint_review_excludes_critic_sources_from_dispatch_and_handoff(
    ws, monkeypatch
):
    manuscript, completed = manuscript_handoff(ws, count=2)
    result_path = ws.path(completed[-1])
    folder = Path(completed[-1]).parent
    assignment = result_path.parent / "assignment/task.json"
    note = str(folder / "rci/attempt_001/002_candidate_critic/review.md")
    dependency = str(folder / "lemma.md")
    ws.write_text(note, "Private exploratory derivation.")
    ws.write_text(dependency, "An independently checkable premise.")
    result = json.loads(result_path.read_text())
    result.update(
        agent_id="rci",
        manuscript_refs=[manuscript],
        published_refs=[note, manuscript],
        manuscript_dependencies={manuscript: [dependency]},
        critic_derivations={manuscript: [note]},
    )
    ws.write_json(completed[-1], result)
    ticket = json.loads(assignment.read_text())
    assignment.write_text(json.dumps({**ticket, "input_refs": [note]}))
    calls = []

    def verify(**kwargs):
        request = kwargs["application_request"]
        assert note not in json.dumps(request) and note not in kwargs["input_refs"]
        assert dependency in kwargs["input_refs"] and manuscript in kwargs["input_refs"]
        calls.append(request)
        ws.write_text(
            request["review_ref"], "Second scope needs a separate implication."
        )
        return StructuredSemanticResult(
            review_payload(request, rejected={"lemma-1"}), {}
        )

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    for _ in range(2):
        assert check_stage_findings(
            llm=object(), workdir=ws.root, completed_result_refs=completed
        )
    assert len(calls) == 1

def test_declared_and_linked_public_finding_freeze_the_same_sources(ws):
    from pace.agents.director.findings import _manuscript_sources
    import os

    current = "results/findings/R0001.md"
    original = ws.work_ref("round0/research.md")
    review = ws.work_ref("round0/research.verification.json")
    manuscript = ws.work_ref("round1/research.md")
    for ref in (current, original, review):
        ws.write_text(ref, "Bound evidence")
    ws.write_json(
        "work/state/findings.json",
        {
            "entries": [
                {"ref": current, "source_ref": original, "verification_ref": review}
            ]
        },
    )
    ws.write_text(manuscript, "A theorem using a declared finding.")
    assert _manuscript_sources(ws.root, manuscript, [current]) == [original, review]
    link = os.path.relpath(ws.path(current), ws.path(manuscript).parent)
    ws.write_text(manuscript, f"Use the [checked lemma]({link}).")
    assert _manuscript_sources(ws.root, manuscript, [current]) == [original, review]


@pytest.mark.parametrize("change", ["shared_lemma", "review"])
def test_joint_review_keeps_same_evidence_for_memory_and_publication(ws, monkeypatch, change):
    dependency = ws.work_ref("input/support.md")
    ws.write_text(dependency, "The common lemma under its original assumptions.")
    _, completed = manuscript_handoff(ws, count=2, dependencies=[dependency])
    calls = []

    def verify(**kw):
        request = kw["application_request"]
        calls.append(request)
        ws.write_text(request["review_ref"], "Independent derivations for the two scopes.")
        return StructuredSemanticResult(review_payload(request), {})

    monkeypatch.setattr("pace.agents.director.findings.verify_finding", verify)
    assert check_stage_findings(llm=object(), workdir=ws.root, completed_result_refs=completed)
    memory_owner.refresh_application_memory(ws.root)
    assert {a["status"] for a in load_index(ws.root)["applications"].values()} == {"APPLICABLE"}
    changed = dependency if change == "shared_lemma" else calls[0]["review_ref"]
    ws.write_text(changed, "Replacement with different hypotheses.")
    memory_owner.refresh_application_memory(ws.root)
    assert {a["status"] for a in load_index(ws.root)["applications"].values()} == {"INVALIDATED"}
    with pytest.raises(ValueError, match="version changed"):
        publish_stage_findings(workdir=ws.root, completed_result_refs=completed)
    assert not ws.path("results/findings/R0001.md").exists() and len(calls) == 1


def test_checked_premise_summary_replaces_required_rereading(ws, monkeypatch):
    from pace.agents import finding_verifier
    from pace.agents.director.findings import _checked_premises
    from pace.contracts.research.memory import binding

    premise = ws.work_ref("round1/lemma.md")
    receipt = str(Path(premise).with_suffix(".verification.json"))
    stable = "results/findings/R0001.md"
    draft = ws.work_ref("round2/generalization.md")
    statement = "Under compactness, a minimizer exists."
    for ref in (premise, stable, draft):
        ws.write_text(ref, "Mathematical manuscript")
    record = {
        "draft_ref": premise,
        "target_contract_id": "TARGET",
        "source_binding": binding(ws.root, premise),
        "dependency_bindings": [],
        "verdict": {
            "stage_status": "PASS",
            "reuse_authorized": True,
            "summary": statement,
        },
    }
    ws.write_json(receipt, record)
    refs = [draft, premise, receipt]
    assert not _checked_premises(ws.root, refs, draft)
    ws.write_json(
        "work/state/findings.json",
        {
            "entries": [
                {"ref": stable, "source_ref": premise, "verification_ref": receipt}
            ],
            "publications": [{"ref": stable, "source_ref": premise, "verification_ref": receipt}],
        },
    )

    def model(llm, prompt, **kwargs):
        assert statement in prompt
        assert set(kwargs["input_refs"]) == {draft}
        assert set(kwargs["host_metadata"]["archive_refs"]) == {premise, receipt}

    monkeypatch.setattr(finding_verifier, "run_semantic_role", model)
    finding_verifier.verify_finding(
        llm=None,
        workdir=ws.root,
        draft_ref=draft,
        input_refs=refs,
        round_index=2,
        target_contract_id="TARGET",
        compact=True,
        checked_premises=_checked_premises(ws.root, refs, draft),
    )
    ws.write_text(premise, "Changed theorem.")
    with pytest.raises(ValueError, match="version changed"):
        _checked_premises(ws.root, refs, draft)


def test_declared_frozen_finding_keeps_original_after_update(ws):
    from pace.agents.director.findings import _manuscript_sources
    from pace.artifacts.results import publish_finding

    def publish(n, action, current=""):
        source = ws.work_ref(f"round{n}/original.md")
        checked = ws.work_ref(f"round{n}/original.verification.json")
        ws.write_text(source, "Original theorem version " + str(n))
        ws.write_text(checked, "Checked under its stated hypotheses.")
        stable = publish_finding(
            ws.root,
            source_ref=source,
            verification_ref=checked,
            round_index=n,
            publication={"action": action, "finding_ref": current},
        )
        return stable, [source, checked]

    stable, old = publish(0, "CREATE")
    folder = ws.work_ref("round2")
    assignment = f"{folder}/assignment/task.json"
    frozen = freeze_context(ws.root, assignment, task(), [stable])
    ws.write_json(assignment, {"round_index": 2})
    snapshot = next(
        row["ref"] for row in frozen["bindings"] if row["source_ref"] == stable
    )
    publish(3, "UPDATE", stable)
    draft = f"{folder}/research.md"
    ws.write_text(draft, "Use the dispatched lemma.")
    assert _manuscript_sources(ws.root, draft, [snapshot, stable]) == old


@pytest.mark.parametrize("ws", ["weak", "standard"], indirect=True)
def test_same_theorem_and_missing_condition_through_both_verifier_interfaces(
    ws, monkeypatch
):
    from test_research_reporter import NativeFixture

    source = ws.work_ref("input/lemma.md")
    verification = str(Path(source).with_suffix(".verification.json"))
    ws.write_text(source, "For a positive real x, its reciprocal is positive.")
    ws.write_json(verification, {"summary": "The result requires strict positivity."})
    manuscript, completed = manuscript_handoff(ws, count=2, dependencies=[source])
    transport = NativeFixture("research")
    turn = ws.path(ws.work_ref("round1"))
    variant = json.loads(ws.path(ws.work_ref("resources/manifest.json")).read_text())[
        "resource_variant"
    ]
    read = []

    def model(messages, tools):
        request = json.loads(
            next((turn / "verification").glob("finding-*.request.json")).read_text()
        )
        if messages[-1]["role"] == "tool":
            assert "error" not in json.loads(messages[-1]["content"])
        available = {t["function"]["name"] for t in tools}
        paths = [manuscript, source, verification]
        if len(read) < len(paths):
            path = paths[len(read)]
            read.append(path)
            return transport.tool("read_file", {"path": path})
        applicable = "Strict positivity supplies the premise."
        missing = "Nonzero alone permits x=-1."
        if variant == "weak":
            assert "compact_review" not in request
            if not ws.path(request["review_ref"]).exists():
                return transport.tool(
                    "write_file",
                    {
                        "path": request["review_ref"],
                        "content": applicable + "\n" + missing,
                    },
                )
            payload = review_payload(request, rejected={"lemma-1"})
        else:
            assert request["compact_review"] and "write_file" not in available
            payload = {
                "stage_status": "PASS",
                "summary": "The claim requires strict positivity.",
                "reuse_authorized": True,
                "publication": {"action": "CREATE"},
                "application_checks": [
                    {
                        "application": n,
                        "status": "APPLICABLE"
                        if item["obligation_id"] == "target"
                        else "NOT_ESTABLISHED",
                        "reason": applicable
                        if item["obligation_id"] == "target"
                        else missing,
                    }
                    for n, item in enumerate(request["applications"], 1)
                ],
            }
        return transport.tool("submit_result", payload)

    monkeypatch.setattr(transport, "_request", model)
    for _ in range(2):
        assert check_stage_findings(
            llm=transport, workdir=ws.root, completed_result_refs=completed
        )
    assert len(transport.calls) == 1 and read == [manuscript, source, verification]
    index = rebuild_research_index(ws.root)
    assert {
        a["obligation_id"]: a["status"] for a in index["applications"].values()
    } == {"target": "APPLICABLE", "lemma-1": "NOT_ESTABLISHED"}


def test_application_only_review_omits_publication_without_sharing_verdicts(ws, monkeypatch):
    from test_research_reporter import NativeFixture

    manuscript_handoff(ws, count=2, finding=False)
    transport = NativeFixture("research")

    def model(messages, tools):
        schema = next(t["function"]["parameters"] for t in tools if t["function"]["name"] == "submit_result")
        assert "publication" not in schema["required"]
        assert schema["properties"]["publication"]["properties"]["action"]["enum"] == ["NONE"]
        return transport.tool("submit_result", {
            "stage_status": "PASS", "reuse_authorized": True,
            "summary": "The bound holds under its positivity condition.",
            "application_checks": [
                {"application": 1, "status": "APPLICABLE", "reason": "The premise holds."},
                {"application": 2, "status": "NOT_ESTABLISHED", "reason": "The sign is unknown."},
            ],
        })

    monkeypatch.setattr(transport, "_request", model)
    for _ in range(2):
        assert check_applications(llm=transport, root=ws.root)
    assert len(transport.calls) == 1
    apps = rebuild_research_index(ws.root)["applications"].values()
    assert sorted(a["status"] for a in apps) == ["APPLICABLE", "NOT_ESTABLISHED"]
    assert not ws.path("results/findings/R0001.md").exists()
