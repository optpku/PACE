"""Planner role: fixed instructions, current task assembly and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import (
    build_specialist_assembly,
    delivery_instruction,
    specialist_instructions,
)
from pace.runtime.prompts import render_prompt_template


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    return specialist_instructions(
        workdir=workdir,
        role="PLANNER",
        resources=resources,
        role_ref="planner/role.md",
    )


def build_prompt(
    *,
    workdir: Path,
    decision: Any,
    target_contract_id: str,
    output_ref: str,
    manifest_ref: str,
):
    extra_inputs, extra_task, revision_ref = (), "", ""
    from pace.agents.director.context import route_base

    base = route_base(workdir, output_ref)
    if decision.prompt_brief.startswith("REVISE_ROUTE") and base:
        extra_inputs = (base,)
        revision_ref = str(Path(output_ref).parent / "revision.json")
        extra_task = render_prompt_template(
            "planner/revision.md",
            workdir=workdir,
            base_ref=base,
            patch_ref=revision_ref,
        )
    return build_specialist_assembly(
        workdir=workdir,
        decision=decision,
        target_contract_id=target_contract_id,
        output_ref=output_ref,
        manifest_ref=manifest_ref,
        role_ref="planner/role.md",
        extra_inputs=extra_inputs,
        extra_task=extra_task,
        delivery_task=build_delivery_instructions(
            workdir, output_ref, revision=bool(revision_ref)
        ),
        revision_ref=revision_ref,
    )


def run(*, llm: Any, workdir: Path, **kwargs):
    from pace.agents.director.dispatch import run_specialist

    return run_specialist(llm=llm, workdir=workdir, agent_id="planner", **kwargs)


def build_delivery_instructions(
    workdir: Path, output_ref: str, *, revision: bool = False
) -> str:
    common = delivery_instruction(workdir, output_ref, with_reporter=False)
    import json

    from pace.agents.director.context import task_context

    if not task_context(workdir, output_ref):
        return common
    if revision:
        common += (
            "\nIf the dependencies and obligation scopes stay unchanged, set "
            "relations_unchanged=true in revision.json. The Host then writes "
            "the empty relation update, and you can skip the following file instruction.\n"
        )
    route = {
        "schema_version": "route_relations/v1",
        "change_ref": output_ref,
        "obligations": [],
    }
    return render_prompt_template(
        "planner/delivery.md",
        workdir=workdir,
        common=common,
        folder=Path(output_ref).parent,
        envelope=json.dumps(route),
    )
