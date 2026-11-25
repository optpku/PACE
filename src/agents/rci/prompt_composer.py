"""Compose fixed RCI instructions and task-local inputs."""

import json

from pace.agents.prompt_composer import compose_instructions
from pace.runtime.prompts import prompt_root, render_prompt_template

from .roles import RCI_SKILL_CORE_REF, resolve_skill_refs, role_binding


def instruction_refs(phase: str) -> tuple[str, ...]:
    binding = role_binding(phase)
    return (
        "rci/shared/instructions.md",
        binding.prompt_ref.removeprefix("resources/prompts/"),
        "rci/shared/phase_delivery.md",
    )


def skill_refs(phase: str, *, workdir) -> tuple[str, ...]:
    return resolve_skill_refs(
        workdir, (*role_binding(phase).skill_refs, RCI_SKILL_CORE_REF)
    )


def build_instructions(*, phase: str, workdir) -> str:
    body = compose_instructions(
        workdir=workdir,
        role=role_binding(phase).role,
        resources=prompt_root(workdir),
        refs=instruction_refs(phase),
    )
    return (
        body
        + "\n\n"
        + render_prompt_template(
            "rci/shared/skills.md",
            workdir=workdir,
            skill_refs="\n".join(
                f"- `{ref}`" for ref in skill_refs(phase, workdir=workdir)
            ),
        )
    )


def build_prompt(
    *, workdir, task, actions, routing, input_refs, output_directory, findings_directory
) -> str:
    return render_prompt_template(
        "rci/shared/task.md",
        workdir=workdir,
        task=task,
        routing_contract=render_prompt_template(
            "rci/shared/routing_task.md", workdir=workdir, actions=repr(list(actions))
        )
        if actions
        else "After writing the assigned Markdown, return an empty JSON object.",
        routing_identities=json.dumps(routing) if routing else "",
        input_refs="\n".join(
            f"- {kind}: `{ref}`" for kind, refs in input_refs.items() for ref in refs
        ),
        output_directory=output_directory,
        finding_instruction=(
            f"For a distinct result with a complete proof or counterexample, write the original finding in `{findings_directory}`."
            if findings_directory
            else "Write your mathematical derivations and objections in review.md."
        ),
    )
