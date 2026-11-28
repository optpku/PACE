"""Global Research Director semantic boundary.

The Host supplies the prompt and suggested input paths.  This role
only obtains and structurally validates one scheduling response; it does not
compose specialist prompts, execute specialists, or decide mathematical
correctness.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import render_role_task
from pace.agents.roles import SemanticRoleSpec
from pace.artifacts.workspace import work_ref
from pace.contracts.research.director_protocol import (
    DIRECTOR_PROTOCOL_SCHEMA_VERSION,
    MAX_SURVEY_CALLS,
    DirectorProtocolError,
    director_decision_schema,
    director_reference_errors,
    parse_director_decision,
    validate_director_stop,
)
from pace.contracts.research.memory import digest
from pace.runtime.prompts import (
    load_prompt_template,
    render_prompt_template,
    skill_index_ref,
)
from pace.runtime.semantic import (
    SemanticCallResult,
    SemanticRepresentationFailure,
    recover_rejected_receipt,
    semantic_call,
    validate_semantic_payload,
)

GLOBAL_DIRECTOR_ROLE = "global_research_director"
GLOBAL_DIRECTOR_PROMPT_REF = "director/role.md"


def build_global_director_prompt(
    *,
    workdir: Path,
    target_contract_id: str = "",
    input_refs: Iterable[str] = (),
    control_context: str = "",
) -> str:
    """Render current Director task fields; fixed instructions live in AGENTS.md.

    Only reference names and scheduling metadata are interpolated. Mathematical
    artifacts remain files for the model to read, so this constructor cannot
    silently summarize or alter the target.
    """

    refs = _unique_nonempty(input_refs)
    reading_text = (
        "\n".join(f"- `{ref}`" for ref in refs)
        or "- browse the problem directory for relevant context"
    )
    specialist_text = load_prompt_template("director/catalog.md", workdir=workdir)
    context = str(control_context or "").strip()
    context_text = context if context else "No additional control context was supplied."
    prompt = render_role_task(
        GLOBAL_DIRECTOR_PROMPT_REF,
        workdir=workdir,
        input_refs=reading_text,
        turn_dir=work_ref(workdir),
        target_contract_id=str(target_contract_id or ""),
        control_context=context_text,
        specialist_catalog=specialist_text,
    )
    return prompt


def run_global_director(
    *,
    llm: Any,
    workdir: Path,
    prompt: str | None = None,
    target_contract_id: str = "",
    input_refs: Iterable[str] = (),
    control_context: str = "",
    decision_inputs: Mapping[str, Any] | None = None,
    on_accept: Callable[[dict[str, Any]], None] | None = None,
    survey_retrieval_enabled: bool = False,
    proof_states_remaining: int | None = None,
    research_rounds_remaining: int | None = None,
    call_name: str = "global_research_director",
    semantic_call_fn: Callable[..., SemanticCallResult] = semantic_call,
    host_metadata: Mapping[str, Any] | None = None,
) -> SemanticCallResult:
    """Run one Global Director call and return the shared semantic result.

    Supplying ``prompt`` is useful when a Host has already assembled additional
    run-local context. If it is omitted, the packaged static prompt is rendered
    from suggested reading paths. No Prompt Composer
    call is made here.
    """

    if decision_inputs is None:
        from pace.agents.director.context import director_decision_inputs

        decision_inputs = director_decision_inputs(Path(workdir), input_refs)
    memory_enabled = decision_inputs["memory_enabled"]
    index_ref = skill_index_ref(workdir)
    refs = _unique_nonempty(
        (*decision_inputs["required_refs"], *((index_ref,) if index_ref else ()))
    )
    archives = tuple(ref for ref in decision_inputs["archive_refs"] if ref not in refs)
    final_prompt = (
        str(prompt)
        if prompt is not None
        else build_global_director_prompt(
            workdir=workdir,
            target_contract_id=target_contract_id,
            input_refs=refs,
            control_context=control_context,
        )
    )

    if memory_enabled:
        final_prompt += render_prompt_template(
            "director/memory_task.md", workdir=workdir, brief=decision_inputs["brief"]
        )

    final_prompt += (
        "\nHost retrieval policy for Survey: enabled. The offline restriction applies "
        "to this Director call. Ask Survey to acquire missing primary sources needed "
        "for the task, respecting any source limits in the user's request.\n"
        if survey_retrieval_enabled
        else "\nHost retrieval policy for Survey: disabled. Assign local source review.\n"
    )
    survey_remaining = decision_inputs["survey_remaining"]
    if decision_inputs["latest_survey"]:
        final_prompt += render_prompt_template(
            "director/latest_survey.md",
            workdir=workdir,
            latest_survey=decision_inputs["latest_survey"],
        )
    final_prompt += render_prompt_template(
        "director/survey_budget.md", workdir=workdir, remaining=survey_remaining
    )
    rounds = (
        "no separate cap"
        if research_rounds_remaining is None
        else str(research_rounds_remaining)
    )
    proof_states = (
        "not supplied"
        if proof_states_remaining is None
        else str(proof_states_remaining)
    )
    final_prompt += render_prompt_template(
        "director/budget.md", workdir=workdir, proof_states=proof_states, rounds=rounds
    )

    schema = director_decision_schema(
        survey_calls_remaining=survey_remaining, memory_enabled=memory_enabled
    )

    def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
        issues = []
        try:
            decision = parse_director_decision(payload, errors=issues)
        except DirectorProtocolError as exc:
            issues.append(exc)
        else:
            try:
                validate_director_stop(
                    decision,
                    proof_states_remaining=proof_states_remaining,
                    research_rounds_remaining=research_rounds_remaining,
                )
            except DirectorProtocolError as exc:
                issues.append(exc)
            surveys = [
                (task, "" if index == 0 else f"parallel_tasks/{index - 1}/")
                for index, task in enumerate((decision, *decision.parallel_tasks))
                if task.action.value == "DISPATCH" and task.agent_id == "survey"
            ]
            if len(surveys) > 1:
                issues.append(
                    DirectorProtocolError(
                        "Combine the source needs into one Survey and remove the second Survey.",
                        field="parallel_tasks",
                        related_fields=("prompt_brief", "input_refs", "rationale"),
                    )
                )
            if survey_remaining < MAX_SURVEY_CALLS:
                for task, prefix in surveys:
                    repeated = _repeated_survey_assignment(Path(workdir), task)
                    if repeated or not task.rationale:
                        repeat_note = (
                            f"The same Survey work order already ran in {repeated}. "
                            if repeated
                            else ""
                        )
                        issues.append(
                            DirectorProtocolError(
                                repeat_note
                                + "Identify the missing decisive primary-source passage and the "
                                "research decision it blocks. If the originals are already "
                                "available, use them for the next mathematical task.",
                                field=prefix + "rationale",
                                related_fields=(
                                    prefix + "agent_id",
                                    prefix + "prompt_brief",
                                    prefix + "input_refs",
                                    prefix + "change_refs",
                                ),
                            )
                        )
            if memory_enabled:
                for index, task in enumerate((decision, *decision.parallel_tasks)):
                    if task.action.value != "DISPATCH":
                        continue
                    prefix = "" if index == 0 else f"parallel_tasks/{index - 1}/"
                    if not task.next_step_kind.strip():
                        missing = ["next_step_kind"]
                        if not task.obligation_ids:
                            missing.append("obligation_ids")
                        if not task.branch_id.strip():
                            missing.append("branch_id")
                        for field in missing:
                            issues.append(
                                DirectorProtocolError(
                                    "new memory protocol requires next_step_kind, obligation_ids and branch_id",
                                    field=prefix + field,
                                )
                            )
                    if any(
                        i not in decision_inputs["obligation_versions"]
                        for i in task.obligation_ids
                    ):
                        issues.append(
                            DirectorProtocolError(
                                "use the closest registered obligation or target within the assigned branch",
                                field=prefix + "obligation_ids",
                            )
                        )
                    if (
                        task.agent_id == "planner"
                        and task.next_step_kind != "REPLAN"
                        and decision_inputs["route"]
                    ):
                        issues.append(
                            DirectorProtocolError(
                                "a committed route already exists; use REPLAN with concrete change_refs",
                                field=prefix + "next_step_kind",
                                related_fields=(prefix + "change_refs",),
                            )
                        )
                    if task.agent_id == "rci" and (
                        repeated := _repeated_rci_assignment(Path(workdir), task, decision_inputs)
                    ):
                        issues.append(
                            DirectorProtocolError(
                                f"This RCI work order already ran in {repeated}. "
                                "Use its recorded question and obstacle. Give a different next "
                                "mathematical step toward the target or cite new evidence that "
                                "changes this test.",
                                field=prefix + "prompt_brief",
                                related_fields=(
                                    prefix + "agent_id",
                                    prefix + "research_operation",
                                    prefix + "research_problem_ref",
                                    prefix + "input_refs",
                                    prefix + "change_refs",
                                ),
                            )
                        )
            issues.extend(
                director_reference_errors(
                    decision, Path(workdir), require_bindings=memory_enabled
                )
            )
        diagnostics = []
        for issue in issues:
            for field in (issue.field, *issue.related_fields):
                diagnostics.append(
                    {
                        "stage": "director_protocol",
                        "json_pointer": "/" + field,
                        "reason_code": "director_decision_invalid",
                        "message": str(issue),
                    }
                )
        return diagnostics

    metadata = {
        "target_contract_id": str(target_contract_id or ""),
        "input_refs": list(refs),
        "prompt_resource": GLOBAL_DIRECTOR_PROMPT_REF,
        "receipt_correction_owner": "DIRECTOR",
        **dict(host_metadata or {}),
    }
    metadata["archive_refs"] = list(
        _unique_nonempty((*archives, *metadata.get("archive_refs", ())))
    )
    spec = SemanticRoleSpec(
        role=GLOBAL_DIRECTOR_ROLE,
        on_accept=on_accept,
        schema=lambda: schema,
        host_schema_version="research_director_decision/v3"
        if memory_enabled
        else DIRECTOR_PROTOCOL_SCHEMA_VERSION,
        authority="SEARCH_CONTEXT_ONLY",
        fatal_scope="CALL",
        retain_schema_rejected_payload=True,
    )
    normalizer = _normalize_parallel_purpose if memory_enabled else None
    call_name = str(call_name or GLOBAL_DIRECTOR_ROLE)
    result = None
    if on_accept is not None and metadata.get("task_id"):
        # The round freezes the route, obligation versions, budgets and reads.
        # A rejected receipt may be reused only for this exact scheduling request.
        metadata["director_request_digest"] = digest(
            {
                "prompt": final_prompt,
                "schema": schema,
                "decision_inputs": decision_inputs,
                "required_refs": refs,
                "archive_refs": metadata["archive_refs"],
                "target_contract_id": metadata["target_contract_id"],
            }
        )
        result = recover_rejected_receipt(
            Path(workdir),
            call_name=call_name,
            role=GLOBAL_DIRECTOR_ROLE,
            metadata=metadata,
            binding_fields=("task_id", "director_request_digest"),
        )
    if result is None:
        result = semantic_call_fn(
            llm,
            final_prompt,
            workdir=Path(workdir),
            call_name=call_name,
            spec=spec.call_spec(normalizer=normalizer, validator=validate),
            input_refs=refs,
            host_metadata=metadata,
        )
    if not isinstance(result, SemanticRepresentationFailure):
        return result
    from pace.runtime.sessions import budget_exhausted

    if budget_exhausted(Path(workdir)) or any(
        row.get("stage") == "acceptance" for row in result.diagnostics
    ):
        return result
    diagnostics = list(result.diagnostics)
    if result.payload:
        # Transport/schema rejection can precede cross-field validation.
        # Diagnose both before choosing which original fields to protect.
        for row in (
            *validate_semantic_payload(result.payload, schema),
            *validate(result.payload),
        ):
            if row not in diagnostics:
                diagnostics.append(row)
    invalid = {row["json_pointer"] for row in diagnostics if row.get("json_pointer")}
    rejected_stop = result.payload.get("action") == "STOP" and "/action" in invalid
    false_budget_stop = (
        rejected_stop
        and result.payload.get("stop_reason") == "BUDGET_EXHAUSTED"
        and proof_states_remaining != 0
        and research_rounds_remaining != 0
    )

    def validate_correction(payload: dict[str, Any]) -> list[dict[str, Any]]:
        diagnostics = validate(payload)
        if false_budget_stop and payload.get("action") != "DISPATCH":
            diagnostics.append(
                {
                    "stage": "director_protocol",
                    "json_pointer": "/action",
                    "reason_code": "director_false_budget_stop",
                    "message": "An incorrect budget stop needs a bounded dispatch, not a relabeled STOP.",
                }
            )

        def preserve(before, after, pointer=""):
            keys = (
                ("action",)
                if before.get("action") == "STOP"
                else (
                    "action",
                    "agent_id",
                    "prompt_brief",
                    "research_operation",
                    "research_problem_ref",
                    "parallel_tasks",
                    "obligation_ids",
                    "branch_id",
                    "next_step_kind",
                    "change_refs",
                    "stop_reason",
                    "input_refs",
                    "archive_refs",
                )
            )
            for key in keys:
                if key not in before:
                    continue
                path = pointer + "/" + key
                if any(path == bad or path.startswith(bad + "/") for bad in invalid):
                    continue
                value = before[key]
                current = after.get(key)
                if key in {
                    "agent_id",
                    "prompt_brief",
                    "research_operation",
                    "research_problem_ref",
                } and value in (None, ""):
                    continue
                if value in ("", []) and current is None:
                    continue
                if (
                    key == "parallel_tasks"
                    and isinstance(value, list)
                    and isinstance(current, list)
                    and len(value) == len(current)
                ):
                    for index, (old, new) in enumerate(zip(value, current)):
                        if isinstance(old, dict) and isinstance(new, dict):
                            preserve(old, new, path + "/" + str(index))
                    continue
                if key in {"input_refs", "archive_refs", "change_refs"} and any(
                    bad.startswith(path + "/") for bad in invalid
                ):
                    continue
                if current != value:
                    diagnostics.append(
                        {
                            "stage": "director_protocol",
                            "json_pointer": path,
                            "reason_code": "director_correction_changed_intent",
                            "message": "Receipt correction changed a valid scheduling choice.",
                        }
                    )

        preserve(result.payload, payload)
        return diagnostics

    raw_ref = str(result.host["response_ref"])
    correction = render_prompt_template(
        "director/receipt_correction.md", workdir=workdir, receipt_ref=raw_ref
    )
    if rejected_stop:
        correction = render_prompt_template(
            "director/stop_correction.md",
            workdir=workdir,
            task=final_prompt,
            receipt_ref=raw_ref,
            required_action="Return a bounded DISPATCH. Do not relabel the incorrect budget stop. "
            if false_budget_stop
            else "Return a bounded DISPATCH or a STOP with a valid reason and rationale. ",
        )
    return semantic_call_fn(
        llm,
        correction + json.dumps(diagnostics, ensure_ascii=False),
        workdir=Path(workdir),
        call_name=call_name + "_receipt_correction",
        spec=spec.call_spec(normalizer=normalizer, validator=validate_correction),
        input_refs=(*refs, raw_ref),
        host_metadata={
            **metadata,
            "parent_call_id": result.host["call_id"],
            "representation_correction_of": result.host["call_id"],
            "receipt_corrections": 1,
            "output_refs": [],
            "output_dirs": [],
        },
    )


def _repeated_survey_assignment(root: Path, task) -> str:
    """An unchanged source request in the same turn provides no new lead."""
    for result_path in (root / work_ref(root)).glob("round*/agent_result.json"):
        if json.loads(result_path.read_text(encoding="utf-8")).get("agent_id") != "survey":
            continue
        assignment = result_path.with_name("director_decision.json")
        previous = json.loads(assignment.read_text(encoding="utf-8"))
        if (
            task.prompt_brief.split() == previous["prompt_brief"].split()
            and set(task.input_refs) == set(previous["input_refs"])
            and set(task.change_refs) == set(previous.get("change_refs", ()))
        ):
            return assignment.relative_to(root).as_posix()
    return ""


def _repeated_rci_assignment(root: Path, task, decision_inputs: Mapping) -> str:
    """Reject exact replays of a completed work order at the same scope."""
    versions = {
        ident: decision_inputs["obligation_versions"].get(ident)
        for ident in task.obligation_ids
    }
    for attempt in reversed(decision_inputs.get("rci_attempts", ())):
        if (
            attempt["branch_id"] != task.branch_id
            or attempt["obligation_versions"] != versions
            or (
                task.research_problem_ref
                and task.research_problem_ref != attempt["research_problem_ref"]
            )
        ):
            continue
        assignment_ref = Path(attempt["result_ref"]).parent / "assignment/task.json"
        previous = json.loads((root / assignment_ref).read_text(encoding="utf-8"))
        evidence = set((*task.input_refs, *task.change_refs)) - {
            task.research_problem_ref, attempt["research_problem_ref"]
        }
        if (
            task.prompt_brief.split() == previous["prompt_brief"].split()
            and evidence.issubset(previous["input_refs"])
        ):
            return attempt["result_ref"]
    return ""


def _normalize_parallel_purpose(payload: dict[str, Any]) -> dict[str, Any]:
    """A companion with no stated purpose is ordinary work unless it is Planner."""
    if not payload.get("parallel_tasks"):
        return payload
    (companion,) = payload["parallel_tasks"]  # The receipt schema allows one companion.
    if (
        companion["agent_id"] == "planner"
        or companion.get("next_step_kind", "").strip()
    ):
        return payload
    # The task, its references and scope still go through the usual validation.
    return {**payload, "parallel_tasks": [{**companion, "next_step_kind": "CONTINUE"}]}


def _unique_nonempty(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


__all__ = [
    "GLOBAL_DIRECTOR_PROMPT_REF",
    "GLOBAL_DIRECTOR_ROLE",
    "build_global_director_prompt",
    "run_global_director",
]


def build_instructions(*, workdir: Path, resources: Path) -> str:
    from pace.agents.prompt_composer import append_skill_catalog, compose_instructions

    body = compose_instructions(
        workdir=workdir,
        role="GLOBAL_RESEARCH_DIRECTOR",
        resources=resources,
        refs=("director/role.md",),
        project_skills=True,
    )
    return append_skill_catalog(body, workdir=workdir)
