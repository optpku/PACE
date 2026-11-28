"""Select and freeze task inputs; no model calls or mathematical judgments."""

from __future__ import annotations

import json
from pathlib import Path

from pace.artifacts.results import FINDINGS_INDEX_REF, relocate_links
from pace.artifacts.workspace import _write_json_atomic, work_ref
from pace.contracts.research.director_protocol import MAX_SURVEY_CALLS
from pace.contracts.research.memory import (
    application_is_approved,
    application_review_bindings,
    binding,
    check_binding,
    check_source_ownership,
    digest,
    load_application_review,
    public_file,
    read_json,
)
from pace.runtime.prompts import standard_workflow


def load_index(root):
    root = Path(root)
    path = root / "memory/research_index.json"
    index = (
        json.loads(path.read_text())
        if path.is_file()
        else {"obligations": {}, "applications": {}, "attempts": []}
    )
    for attempt in index["attempts"]:
        if "research_problem_ref" in attempt:
            continue
        folder = Path(attempt["result_ref"]).parent
        outcome = folder / "rci/attempt_001/outcome.json"
        if (root / outcome).is_file():
            previous = read_json(root, str(outcome))
            if previous["outcome"] == "HANDOFF":
                attempt["research_problem_ref"] = previous["candidate_ref"] or str(
                    folder / "assignment/task.json"
                )
    return index


def director_archive_refs(root: Path, *, index=None) -> tuple[str, ...]:
    """Authorize the sources named by Host navigation, without making them required reading.

    Only known Host fields grant access. Links in mathematical manuscripts do not
    expand permissions, and specialists and blind probes do not inherit this view.
    """
    refs = []
    dossier_path = root / "memory/active_problem_dossier.json"
    if dossier_path.is_file():
        dossier = json.loads(dossier_path.read_text())
        refs.extend(
            dossier.get(k, "")
            for k in (
                "problem_ref",
                "target_contract_ref",
                "orientation_ref",
                "problem_brief_ref",
            )
        )
    if index is None:
        index = load_index(root)
    for obligation in index["obligations"].values():
        refs.extend((obligation["statement_ref"], obligation["scope_ref"]))
    for route in index.get("routes", {}).values():
        refs.extend((route["ref"], route.get("relations_ref", "")))
    for attempt in index["attempts"]:
        refs.extend(
            (
                attempt["change_ref"],
                attempt["result_ref"],
                attempt.get("research_problem_ref", ""),
                attempt["attempt"].get("obstacle_ref", ""),
                attempt["attempt"].get("reopen_condition_ref", ""),
            )
        )
    for application in index["applications"].values():
        refs.extend(
            (
                application["argument_ref"],
                *application["premise_refs"],
                application.get("review_ref", ""),
                application.get("review_text_ref", ""),
            )
        )
    refs = tuple(dict.fromkeys(ref for ref in refs if ref))
    for ref in refs:
        public_file(root, ref)
    return refs


def director_decision_inputs(
    root: Path, input_refs, completed_refs=(), *, assembling=False
) -> dict:
    """Select the reading list and scheduling facts for one Director call."""
    from pace.contracts.research.memory import enabled
    from pace.orchestrator.memory import RESEARCH_INDEX_REF, research_brief

    required = list(input_refs)
    focused = standard_workflow(root)
    index = load_index(root)
    archives = list(director_archive_refs(root, index=index))
    expand = (
        assembling
        or focused
        and any(
            row.get("status") == "INVALIDATED"
            and row.get("obligation_version")
            == index["obligations"][row["obligation_id"]]["version"]
            for row in index["applications"].values()
        )
    )
    survey_reports = []
    current_surveys = []
    for ref in completed_refs:
        if Path(ref).name != "agent_result.json":
            continue
        result = read_json(root, ref)
        if (
            focused
            and ref in required
            and result.get("agent_id") == "planner"
            and result.get("status") == "SUCCEEDED"
        ):
            expand = True
            route_ref = index.get("current_route", {}).get("ref")
            if route_ref:
                public_file(root, route_ref)
                required.append(route_ref)
        if result.get("agent_id") != "survey" or result.get("status") != "SUCCEEDED":
            continue
        report = result["deliverable_ref"]
        if report not in completed_refs:
            raise ValueError("Survey report is missing from the committed handoff")
        public_file(root, report)
        survey_reports.append(report)
        if ref in input_refs:
            current_surveys.append(report)
        folder = Path(ref).parent
        for source in result.get("published_refs", ()):
            if Path(source).is_relative_to(folder / "sources") or source in {
                str(folder / "source_manifest.json"),
                str(folder / "source_processing.json"),
            }:
                public_file(root, source)
                archives.append(source)
    if focused:
        if (root / RESEARCH_INDEX_REF).is_file():
            public_file(root, RESEARCH_INDEX_REF)
            archives.append(RESEARCH_INDEX_REF)
        navigation = {
            "memory/README.md",
            "memory/active_problem_dossier.json",
            work_ref(root, "state/director_state.json"),
        }
        if not expand and len(index["attempts"]) > 0:
            navigation.add(FINDINGS_INDEX_REF)
        archives.extend(ref for ref in required if ref in navigation)
        required = [ref for ref in required if ref not in navigation]
    latest = survey_reports[-1:] if not focused else current_surveys
    required.extend(latest)
    archives.extend(ref for ref in survey_reports if ref not in latest)
    # A completed or failed Survey costs one acquisition. Transport attempts
    # within the same round share the one host receipt.
    surveys = sum(
        json.loads(p.read_text()).get("agent_id") == "survey"
        for p in (root / work_ref(root)).glob("*/agent_result.json")
    )
    return {
        "required_refs": list(dict.fromkeys(required)),
        "archive_refs": list(
            dict.fromkeys(ref for ref in archives if ref not in required)
        ),
        "memory_enabled": enabled(root),
        "brief": research_brief(
            root, focused=focused, expand=expand, recent_refs=input_refs, index=index
        ),
        "obligation_versions": {
            key: row["version"] for key, row in index["obligations"].items()
        },
        "route": index.get("current_route", {}),
        "rci_attempts": [
            {
                key: attempt[key]
                for key in (
                    "research_problem_ref", "branch_id", "obligation_versions", "result_ref"
                )
            }
            for attempt in index["attempts"]
            if attempt.get("research_problem_ref")
        ],
        "survey_remaining": max(0, MAX_SURVEY_CALLS - surveys),
        "latest_survey": latest[-1] if latest else "",
    }


def validate_context(root: Path, context_ref: str) -> dict:
    context = read_json(root, context_ref)
    if context.get("schema_version") != "research_context/v1":
        raise ValueError("unsupported research context")
    for row in context["bindings"]:
        check_binding(root, row)
    check_binding(root, context["route_context"])
    frozen = read_json(root, context["route_context"]["ref"])
    check_binding(root, frozen["target"])
    for obligation in frozen["obligations"]:
        for row in obligation["bindings"]:
            check_binding(root, row)
    return context


def freeze_context(root: Path, assignment_ref: str, decision, input_refs) -> dict:
    folder = Path(assignment_ref).parent
    context_ref = str(folder / "context.json")
    if (root / context_ref).exists():
        context = validate_context(root, context_ref)
        if context["decision_hash"] != digest(decision.to_json()):
            raise ValueError("frozen decision differs from assignment")
        return context
    index = load_index(root)
    check_binding(root, index["target"])
    ids = list(decision.obligation_ids) or ["target"]
    if any(i not in index["obligations"] for i in ids):
        raise ValueError("unknown task obligation")
    selected = list(input_refs)
    selected += list(decision.change_refs)
    related = set(ids)
    pending = list(ids)
    while pending:
        for parent in index["obligations"][pending.pop()].get("parent_ids", []):
            if parent not in related:
                related.add(parent)
                pending.append(parent)
    archives = list(decision.archive_refs)
    premises = set()
    for ident in ids:
        obligation = index["obligations"][ident]
        for row in obligation["bindings"]:
            check_binding(root, row)
        selected += [obligation["statement_ref"], obligation["scope_ref"]]
        for parent in obligation.get("parent_ids", []):
            for row in index["obligations"][parent]["bindings"]:
                check_binding(root, row)
            selected += [index["obligations"][parent]["statement_ref"]]
        for group in obligation.get("premise_groups", []):
            for premise in group:
                premises.add(premise)
                for row in index["obligations"][premise]["bindings"]:
                    check_binding(root, row)
                selected += [
                    index["obligations"][premise]["statement_ref"],
                    index["obligations"][premise]["scope_ref"],
                ]
    for app in index.get("applications", {}).values():
        ident = app["obligation_id"]
        direct = ident in ids
        if not direct and (ident not in premises or app["status"] != "APPLICABLE"):
            continue
        sources = [app["argument_ref"], *app["premise_refs"]]
        sources.extend(
            app[key] for key in ("review_ref", "review_text_ref") if app.get(key)
        )
        current = app["obligation_version"] == index["obligations"][ident]["version"]
        if current and app["status"] != "INVALIDATED":
            if app["status"] == "APPLICABLE":
                expected = application_review_bindings(app)
                review = load_application_review(root, app)
                if (
                    review is None
                    or not application_is_approved(app, review)
                    or (
                        app.get("review_text_ref")
                        and app["review_text_ref"] != review["review_binding"]["ref"]
                    )
                ):
                    raise ValueError(
                        f"application review differs from the selected evidence: {app['review_ref']}"
                    )
                for row in (
                    *expected,
                    review["request_binding"],
                    review["review_binding"],
                ):
                    check_binding(root, row)
            selected.extend(sources)
        elif direct:
            archives.extend(sources)
    attempts = [
        attempt
        for attempt in index.get("attempts", [])
        if related & set(attempt["obligation_ids"])
        or (
            decision.research_problem_ref
            and attempt.get("research_problem_ref") == decision.research_problem_ref
        )
    ]
    # A direct obstacle can outlive a revision. Other branches remain accessible
    # without turning every attempt at a common ancestor into required reading.
    focused = standard_workflow(root)
    latest_change = ""
    latest_obstacle = ()
    latest_failed_obstacle = ()
    for attempt in attempts:
        obligation_ids = set(attempt["obligation_ids"])
        same_task = (
            bool(obligation_ids.intersection(ids))
            or attempt["branch_id"] == decision.branch_id
        )
        obstacle = attempt["attempt"]["obstacle_ref"]
        if focused and decision.agent_id in {"rci", "planner"}:
            reopen = attempt["attempt"].get("reopen_condition_ref", "")
            notes = tuple(ref for ref in (obstacle, reopen) if ref)
            archives.extend((*notes, attempt["change_ref"]))
            if decision.agent_id == "planner":
                continue  # The dispatch's change_refs already select the reason to replan.
            if decision.research_problem_ref:
                same_task = (
                    attempt.get("research_problem_ref") == decision.research_problem_ref
                )
            else:
                same_task = attempt["branch_id"] == decision.branch_id
            if same_task:
                latest_obstacle = notes
                if notes and attempt["attempt"]["outcome"] in {
                    "METHOD_OBSTRUCTION", "NOT_ESTABLISHED", "CONTEXT_GAP"
                }:
                    latest_failed_obstacle = notes
        elif obstacle:
            (selected if same_task else archives).append(obstacle)
        versions = attempt.get("obligation_versions", {})
        if same_task and any(
            versions.get(i) == index["obligations"][i]["version"]
            for i in related & obligation_ids
        ):
            latest_change = attempt["change_ref"]
    selected.extend((*latest_failed_obstacle, *latest_obstacle))
    if latest_change:
        selected.append(latest_change)
    route_path = root / work_ref(root, "state/current_route.json")
    route = json.loads(route_path.read_text()) if route_path.is_file() else {}
    if route:
        if route.get("sha256"):
            check_binding(root, route)
        if route.get("relations_sha256"):
            check_binding(
                root,
                {"ref": route["relations_ref"], "sha256": route["relations_sha256"]},
            )
        route_inputs = selected if decision.agent_id == "planner" else archives
        route_inputs.append(route["ref"])
        if route.get("relations_ref"):
            route_inputs.append(route["relations_ref"])
    # Follow only the selected RCI artifact's own Critic sources. These notes
    # remain optional and carry no independent mathematical verdict.
    critic_notes = {}
    owner_bindings = {}
    if decision.agent_id in {"rci", "research", "prover"}:
        owners = {}
        for ref in dict.fromkeys(selected):
            if decision.agent_id == "rci" and ref != decision.research_problem_ref:
                continue
            row = binding(root, ref)
            owner_bindings[ref] = row
            owner = row.get("source_result_ref", "")
            if not owner or not (root / owner).is_file():
                continue
            if owner not in owners:
                check_source_ownership(root, row)
                owners[owner] = read_json(root, owner)
            result = owners[owner]
            if result.get("agent_id") != "rci" or result.get("status") != "SUCCEEDED":
                continue
            sources = result.get("critic_derivations", {})
            notes = sources.get(ref, ())
            if ref in {owner, result["deliverable_ref"]}:
                notes = list(
                    dict.fromkeys(note for group in sources.values() for note in group)
                )
            if notes:
                critic_notes[ref] = list(notes)
                destination = selected if decision.agent_id == "rci" else archives
                destination.extend(notes)
    if decision.agent_id == "planner":
        from pace.orchestrator.memory import research_progress

        _, target_progress = research_progress(index)
        if target_progress["last_change_ref"]:
            archives.append(target_progress["last_change_ref"])
    # Navigation is a frozen entry, not a reason to expand the complete archive.
    archives = list(dict.fromkeys(archives))
    required, bindings, aliases = [], [], {}
    for original in dict.fromkeys((*selected, *archives)):
        source = public_file(root, original)
        original_binding = owner_bindings.get(original) or binding(root, original)
        check_source_ownership(root, original_binding)
        mutable = original.startswith(("memory/", "results/")) or "/state/" in original
        ref = original
        if mutable:
            destination = root / folder / "snapshot" / original
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".md":
                destination.write_text(
                    relocate_links(source.read_text(), source, destination)
                )
            else:
                destination.write_bytes(source.read_bytes())
            ref = destination.relative_to(root).as_posix()
        row = binding(root, ref) if mutable else original_binding.copy()
        row.update(source_ref=original, source_sha256=original_binding["sha256"])
        bindings.append(row)
        aliases[original] = ref
        if original in selected:
            required.append(ref)
    route_ref = str(folder / "route_context.json")
    frozen_route = {**route, "ref": aliases.get(route.get("ref"), route.get("ref", ""))}
    if route.get("relations_ref"):
        frozen_route["relations_ref"] = aliases[route["relations_ref"]]
    route_context = {
        "schema_version": "route_context/v1",
        "route": frozen_route,
        "work_order": decision.prompt_brief,
        "critic_derivations": critic_notes,
        "target": binding(root, work_ref(root, "input/target_contract.json")),
        "branch_id": decision.branch_id or "b-target",
        "obligations": [index["obligations"][i] for i in ids],
        "related_versions": {
            i: index["obligations"][i]["version"] for i in sorted(related)
        },
        "related_attempt_refs": [a["change_ref"] for a in attempts],
    }
    if focused and decision.agent_id in {"research", "rci", "prover"}:
        route_context["review_obligation_ids"] = [
            row["id"]
            for row in route_context["obligations"]
            if (row["kind"] == "TARGET_REQUIRED" and row["status"] == "OPEN")
            or decision.next_step_kind == "REASSESS"
        ]
    if decision.agent_id == "planner":
        route_context["target_progress"] = target_progress
        if decision.next_step_kind == "REPLAN":
            route_context["change_refs"] = [
                aliases[ref] for ref in decision.change_refs
            ]
    _write_json_atomic(root / route_ref, route_context)
    context = {
        "schema_version": "research_context/v1",
        "assignment_ref": assignment_ref,
        "decision_hash": digest(decision.to_json()),
        "required_refs": list(dict.fromkeys(required)),
        "archive_refs": [aliases[r] for r in archives if aliases[r] not in required],
        "bindings": bindings,
        "route_context": binding(root, route_ref),
        "branch_id": route_context["branch_id"],
        "obligation_ids": ids,
        "selected_dependencies": list(dict.fromkeys(selected)),
        "required_bytes": sum(
            (root / r).stat().st_size for r in dict.fromkeys(required)
        ),
    }
    _write_json_atomic(root / context_ref, context)
    return context


def task_context(root: Path, output_ref: str) -> dict:
    ref = str(Path(output_ref).parent / "assignment/context.json")
    return validate_context(root, ref) if (root / ref).is_file() else {}


def route_base(root: Path, output_ref: str) -> str:
    context = task_context(root, output_ref)
    if context:
        return read_json(root, context["route_context"]["ref"])["route"].get("ref", "")
    return ""


def validate_research_delivery(
    root: Path, result: dict, assignment_ref: str
) -> dict:
    from pace.contracts.research.memory import (
        enabled,
        prepare_delta,
        prepare_route,
    )

    if not enabled(root):
        return {}
    folder = Path(assignment_ref).parent.parent
    context_ref = str(folder / "assignment/context.json")
    if not (root / context_ref).is_file():
        return {}  # proof-mode authors have a different fixed chain
    context = validate_context(root, context_ref)
    index = load_index(root)
    obligations = dict(index["obligations"])
    # Validate against the dispatched versions, even if a parallel task changes the current route.
    from pace.contracts.research.memory import read_json

    frozen = read_json(root, context["route_context"]["ref"])
    obligations.update({row["id"]: row for row in frozen["obligations"]})
    payload = {}
    if result["agent_id"] == "planner":
        ref = str(folder / "route_relations.json")
        payload["route"] = prepare_route(root, ref, obligations)
        payload["route_ref"] = ref
        if frozen.get("change_refs"):
            payload["route_change_refs"] = frozen["change_refs"]
    if result["agent_id"] not in {"research", "prover", "rci"}:
        return payload
    from pace.agents.reporter import restore_reporter_delivery

    result_ref = str(folder / "agent_result.json")
    restore_reporter_delivery(root, result_ref)
    payload["reporter_ref"] = str(folder / "reporter_result.json")
    if "review_obligation_ids" in frozen:
        payload["review_obligation_ids"] = frozen["review_obligation_ids"]
    delta_ref = str(folder / "research_delta.json")
    delta = prepare_delta(root, delta_ref, obligations)
    if (
        delta["branch_id"] != context["branch_id"]
        or delta["obligation_ids"] != context["obligation_ids"]
    ):
        raise ValueError("research delta must retain the assigned branch and obligation identities")
    payload.update(delta=delta, delta_ref=delta_ref)
    if result["agent_id"] == "rci" and result.get("research_problem_ref"):
        question = result["research_problem_ref"]
        check_source_ownership(root, binding(root, question), local_result_ref=result_ref)
        payload["research_problem_ref"] = question
    if "related_versions" in frozen:
        payload["related_versions"] = frozen["related_versions"]
    return payload
