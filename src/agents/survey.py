"""Survey role: fixed instructions, current task assembly and execution."""

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
        role="SURVEY",
        resources=resources,
        role_ref="survey/role.md",
        guides=mode != "proof",
        shared=mode != "proof",
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
        role_ref="survey/role.md",
        delivery_task=delivery_instruction(workdir, output_ref),
    )


def run(*, llm: Any, workdir: Path, **kwargs):
    from pace.agents.director.dispatch import run_specialist

    return run_specialist(llm=llm, workdir=workdir, agent_id="survey", **kwargs)


def retrieval_instruction(workdir: Path, *, enabled: bool) -> str:
    return render_prompt_template(
        "survey/retrieval.md",
        workdir=workdir,
        status="enabled" if enabled else "disabled",
    )


def build_proof_prompt(
    *, workdir: Path, brief: str, input_refs, output_ref: str
) -> str:
    return render_prompt_template(
        "shared/proof_task.md",
        workdir=workdir,
        brief=brief,
        prior_turns=render_prompt_template("shared/prior_turns.md", workdir=workdir),
        input_refs="\n".join(f"- `{ref}`" for ref in input_refs),
        output_ref=output_ref,
    )
