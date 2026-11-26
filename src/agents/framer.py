"""Framer role: fixed instructions, current task assembly and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import (
    build_specialist_assembly,
    delivery_instruction,
    specialist_instructions,
)


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    return specialist_instructions(
        workdir=workdir,
        role="DIRECTOR_LED_FRAMER",
        resources=resources,
        role_ref="framer/role.md",
        project_skills=False,
        mode_ref="framer/instructions.md",
        extra_refs=(
            "shared/literal_target.md",
            "shared/contracts/research.md",
            "shared/artifact_writing.md",
        ),
    )


def build_prompt(
    *,
    workdir: Path,
    decision: Any,
    target_contract_id: str,
    output_ref: str,
    manifest_ref: str,
):
    return build_specialist_assembly(
        workdir=workdir,
        decision=decision,
        target_contract_id=target_contract_id,
        output_ref=output_ref,
        manifest_ref=manifest_ref,
        role_ref="framer/role.md",
        mode="RESEARCH_FRAMING",
        mode_ref="framer/instructions.md",
        extra_refs=(
            "shared/literal_target.md",
            "shared/contracts/research.md",
            "shared/artifact_writing.md",
        ),
        delivery_task=delivery_instruction(workdir, output_ref),
    )


def run(*, llm: Any, workdir: Path, **kwargs):
    from pace.agents.director.dispatch import run_specialist

    return run_specialist(llm=llm, workdir=workdir, agent_id="framer", **kwargs)
