"""RCI-owned fixed instructions and task prompts for independent phase calls."""

import json
from pathlib import Path

from pace.agents.prompt_composer import compose_instructions
from pace.runtime.prompts import is_prompt_resource, prompt_root, render_prompt_template

from .roles import (
    RCI_SKILL_CORE_REF,
    RCI_SKILL_ENTRY_REF,
    resolve_skill_refs,
    role_binding,
)


def instruction_refs(phase: str, *, workdir: Path) -> tuple[str, ...]:
    binding = role_binding(phase)
    phase = binding.phase
    return (
        "rci/shared/instructions.md",
        *(
            ("rci/shared/raw_seed_critic.md",)
            if phase.startswith("RAW_SEED_CRITIC_")
            else ()
        ),
        binding.prompt_ref.removeprefix("resources/prompts/"),
        "shared/transport.md",
        "shared/output/findings.md",
        "shared/output/structured.md",
        "shared/contracts/research.md",
        "shared/artifact_writing.md",
        *(
            "shared/guides/" + p.name
            for p in sorted((prompt_root(workdir) / "shared/guides").glob("*.md"))
        ),
        "rci/seed_scout/delivery.md"
        if phase == "SEED_SCOUT"
        else "rci/shared/phase_delivery.md",
    )


def skill_refs(phase: str, *, workdir: Path, extra_refs=()) -> tuple[str, ...]:
    binding = role_binding(phase)
    return resolve_skill_refs(
        workdir,
        (
            *binding.skill_refs,
            *extra_refs,
            RCI_SKILL_CORE_REF,
            *(
                (RCI_SKILL_ENTRY_REF,)
                if binding.required_inputs != ("PROBE_TASK",)
                else ()
            ),
        ),
    )


def build_instructions(*, phase: str, workdir: Path) -> str:
    body = compose_instructions(
        workdir=workdir,
        role=role_binding(phase).role,
        resources=prompt_root(workdir),
        refs=instruction_refs(phase, workdir=workdir),
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
    *,
    workdir: Path,
    task: str,
    actions,
    routing,
    input_refs,
    archive_ref: str,
    output_directory: str,
    findings_directory: str,
) -> str:
    inputs = tuple(
        ref
        for ref in input_refs
        if ref != archive_ref and not is_prompt_resource(workdir / ref, workdir=workdir)
    )
    return render_prompt_template(
        "rci/shared/task.md",
        workdir=workdir,
        task=task,
        routing_contract=render_prompt_template(
            "rci/shared/routing_task.md", workdir=workdir, actions=repr(list(actions))
        )
        if actions
        else "",
        routing_identities=json.dumps(routing) if routing else "",
        input_refs="\n".join(f"- `{ref}`" for ref in inputs),
        archive_instruction=render_prompt_template(
            "rci/shared/archive_task.md", workdir=workdir, archive_ref=archive_ref
        )
        if archive_ref
        else "",
        output_directory=output_directory,
        findings_directory=findings_directory,
    )


def build_explorer_task(
    *, workdir: Path, actions, question_ref: str, mechanisms
) -> str:
    return render_prompt_template(
        "rci/frontier_explorer/task.md",
        workdir=workdir,
        value_1=", ".join(actions),
        continued_question=render_prompt_template(
            "rci/frontier_explorer/continued_question.md",
            workdir=workdir,
            question_ref=question_ref,
        )
        if question_ref
        else "",
        mechanism_identities=json.dumps(mechanisms),
    )
