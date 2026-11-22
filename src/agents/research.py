"""Research role: fixed instructions, current task assembly and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import (
    build_specialist_assembly,
    delivery_instruction,
    specialist_instructions,
)
from pace.contracts.research.memory import reporter_owned


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    return specialist_instructions(
        workdir=workdir,
        role="RESEARCH",
        resources=resources,
        role_ref="research/role.md",
        extra_refs=(),
    )


def build_prompt(
    *,
    workdir: Path,
    decision: Any,
    target_contract_id: str,
    output_ref: str,
    manifest_ref: str,
):
    extra_inputs, extra_task = (), ""
    return build_specialist_assembly(
        workdir=workdir,
        decision=decision,
        target_contract_id=target_contract_id,
        output_ref=output_ref,
        manifest_ref=manifest_ref,
        role_ref="research/role.md",
        extra_refs=(),
        extra_inputs=extra_inputs,
        extra_task=extra_task,
        delivery_task=build_delivery_instructions(workdir, output_ref),
        separate_reporter=reporter_owned(workdir),
    )


def run(*, llm: Any, workdir: Path, **kwargs):
    from pace.agents.director.dispatch import run_specialist

    return run_specialist(llm=llm, workdir=workdir, agent_id="research", **kwargs)


def build_delivery_instructions(workdir: Path, output_ref: str) -> str:
    common = delivery_instruction(workdir, output_ref, with_delta=True)
    return common
