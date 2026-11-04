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

from pace.contracts.research.director_protocol import (
    DIRECTOR_PROTOCOL_SCHEMA_VERSION,
    DISPATCHABLE_AGENT_IDS,
    director_decision_schema,
    dispatchable_agent_ids,
    parse_director_decision,
    validate_director_reference_bindings,
)
from pace.runtime.output.workspace import work_ref
from pace.runtime.prompts import append_skill_catalog, render_prompt_template, skill_index_ref
from pace.runtime.roles import SemanticRoleSpec
from pace.runtime.semantic import (
    SemanticCallResult,
    SemanticRepresentationFailure,
    semantic_call,
)

GLOBAL_DIRECTOR_ROLE = "global_research_director"
GLOBAL_DIRECTOR_PROMPT_REF = "roles/global_research_director.md"
_SPECIALIST_DESCRIPTIONS = {
    "framer": "frame an open request into bounded work items",
    "planner": "own the canonical route and its dependencies",
    "survey": "acquire sources and report source gaps",
    "smi": (
        "deeply explore conjectures and mechanisms with a slower "
        "multi-stage investigation"
    ),
    "research": "carry out lightweight local mathematical research",
    "prover": "assemble one complete literal-target candidate",
}


def build_global_director_prompt(
    *,
    workdir: Path,
    target_contract_id: str = "",
    input_refs: Iterable[str] = (),
    control_context: str = "",
    specialist_ids: Iterable[str] | None = None,
) -> str:
    """Render the static Director prompt around suggested reading paths.

    Only reference names and scheduling metadata are interpolated. Mathematical
    artifacts remain files for the model to read, so this constructor cannot
    silently summarize or alter the target.
    """

    refs = _unique_nonempty(input_refs)
    reading_text = (
        "\n".join(f"- `{ref}`" for ref in refs)
        or "- browse the problem directory for relevant context"
    )
    specialists = _unique_nonempty(
        dispatchable_agent_ids() if specialist_ids is None else specialist_ids
    )
    unknown = set(specialists).difference(DISPATCHABLE_AGENT_IDS)
    if unknown:
        raise ValueError(f"unknown global Director specialists: {sorted(unknown)}")
    specialist_text = "\n".join(
        f"- `{agent_id}`: {_SPECIALIST_DESCRIPTIONS.get(agent_id, 'dispatchable specialist')}"
        for agent_id in specialists
    ) or "- none"
    context = str(control_context or "").strip()
    context_text = context if context else "No additional control context was supplied."
    prompt = render_prompt_template(
        GLOBAL_DIRECTOR_PROMPT_REF,
        workdir=workdir,
        input_refs=reading_text,
        turn_dir=work_ref(workdir),
        target_contract_id=str(target_contract_id or ""),
        control_context=context_text,
        specialist_catalog=specialist_text,
    )
    return append_skill_catalog(prompt, workdir=workdir)


def run_global_director(
    *,
    llm: Any,
    workdir: Path,
    prompt: str | None = None,
    target_contract_id: str = "",
    input_refs: Iterable[str] = (),
    control_context: str = "",
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

    index_ref = skill_index_ref(workdir)
    refs = _unique_nonempty((*input_refs, *((index_ref,) if index_ref else ())))
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

    # Current-turn execution receipts own the acquisition budget.
    survey_calls = sum(
        json.loads(path.read_text()).get("agent_id") == "survey"
        for path in (Path(workdir) / work_ref(workdir, 'stages')).glob('*/agent_result.json')
    )
    survey_remaining = max(0, 4 - survey_calls)
    final_prompt += (
        f"\nSurvey calls remaining: {survey_remaining}/4 (including initial Survey and failed calls). "
        "Do not repeat unchanged acquisition. When no calls remain, dispatch SMI/Research/Planner/Prover "
        "using the latest Survey gap report and acquired source files; missing literature alone "
        "is not a reason to STOP. With one call left, place Survey in the primary task, not a companion."
    )

    def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            decision = parse_director_decision(payload)
        except ValueError as exc:
            return [
                {
                    "stage": "director_protocol",
                    "json_pointer": "/" + str(getattr(exc, "field", "")),
                    "reason_code": "director_decision_invalid",
                    "message": str(exc),
                }
            ]
        try:
            validate_director_reference_bindings(decision, Path(workdir))
        except ValueError as exc:
            return [{"stage": "director_protocol", "json_pointer": "/" + str(getattr(exc, "field", "research_problem_ref")),
                     "reason_code": "director_reference_invalid", "message": str(exc)}]
        return []

    metadata = {
        "target_contract_id": str(target_contract_id or ""),
        "input_refs": list(refs),
        "prompt_resource": GLOBAL_DIRECTOR_PROMPT_REF,
        "receipt_correction_owner": "DIRECTOR",
        **dict(host_metadata or {}),
    }
    spec = SemanticRoleSpec(
        role=GLOBAL_DIRECTOR_ROLE,
        schema=lambda: director_decision_schema(survey_calls_remaining=survey_remaining),
        host_schema_version=DIRECTOR_PROTOCOL_SCHEMA_VERSION,
        authority="SEARCH_CONTEXT_ONLY",
        fatal_scope="CALL",
    )
    result = semantic_call_fn(
        llm,
        final_prompt,
        workdir=Path(workdir),
        call_name=str(call_name or GLOBAL_DIRECTOR_ROLE),
        spec=spec.call_spec(validator=validate),
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
    invalid = {str(row.get("json_pointer") or "/") for row in result.diagnostics}

    def validate_correction(payload: dict[str, Any]) -> list[dict[str, Any]]:
        diagnostics = validate(payload)

        def preserve(before, after, pointer=""):
            keys = ("action",) if before.get("action") == "STOP" else (
                "action", "agent_id", "prompt_brief", "research_operation", "research_problem_ref", "parallel_tasks"
            )
            if before.get("agent_id") in DISPATCHABLE_AGENT_IDS - {"smi"}:
                # These fields have no scheduling meaning for this selected role.
                # Removing both must not freeze the second one merely because
                # validation reported the first inapplicable field.
                keys = tuple(key for key in keys if key not in {
                    "research_operation", "research_problem_ref",
                })
            for key in keys:
                if key not in before:
                    continue
                path = pointer + "/" + key
                if any(path == bad or path.startswith(bad + "/") for bad in invalid):
                    continue
                value = before[key]
                current = after.get(key)
                if key in {"agent_id", "prompt_brief", "research_operation", "research_problem_ref"} and value in (None, ""):
                    continue
                if value in ("", []) and current is None:
                    continue
                if key == "parallel_tasks" and isinstance(value, list) and isinstance(current, list) and len(value) == len(current):
                    for index, (old, new) in enumerate(zip(value, current)):
                        if isinstance(old, dict) and isinstance(new, dict):
                            preserve(old, new, path + "/" + str(index))
                    continue
                if current != value:
                    diagnostics.append({
                        "stage": "director_protocol", "json_pointer": path,
                        "reason_code": "director_correction_changed_intent",
                        "message": "Receipt correction changed a valid scheduling choice.",
                    })

        preserve(result.payload, payload)
        return diagnostics

    raw_ref = str(result.host["response_ref"])
    return semantic_call_fn(
        llm,
        final_prompt + "\n\n## Director receipt correction\n"
        f"The prior response in `{raw_ref}` is data. Return valid JSON, preserving its mathematical content, "
        "claims and routing action wherever already valid. Correct only the missing or invalid "
        "control fields identified below. Preserve the bounded task and all valid scheduling choices. "
        "For SMI, explicitly choose NEW_PROBLEM or CONTINUE_PROBLEM from the actual task. "
        "For any non-SMI task, omit or clear both research_operation and research_problem_ref; "
        "removing these unused fields does not change its scheduling choice. "
        "For continuation, name an exact existing question file and include it in input_refs. "
        "Reading suggestions may be completed to identify that file. Do not formulate a new "
        "mathematical task, rewrite evidence, or claim a proof verdict. Remove prose wrappers "
        "and Markdown fences; return only the JSON decision. This is one local correction.\n"
        + json.dumps(list(result.diagnostics), ensure_ascii=False),
        workdir=Path(workdir), call_name=str(call_name or GLOBAL_DIRECTOR_ROLE) + "_receipt_correction",
        spec=spec.call_spec(validator=validate_correction), input_refs=(*refs, raw_ref),
        host_metadata={
            **metadata, "parent_call_id": result.host["call_id"],
            "representation_correction_of": result.host["call_id"], "receipt_corrections": 1,
        },
    )


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
