"""Prover role: fixed instructions, current task assembly and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pace.agents.prompt_composer import (
    build_specialist_assembly,
    delivery_instruction,
    specialist_instructions,
)
from pace.contracts.research.memory import reporter_owned
from pace.runtime.prompts import render_prompt_template


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    return specialist_instructions(
        workdir=workdir,
        role="WHOLE_PROVER",
        resources=resources,
        role_ref="prover/direct.md" if mode == "proof" else "prover/research.md",
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
    extra_inputs, extra_task = (), ""
    from pace.agents.director.context import task_context

    frozen = task_context(workdir, output_ref)
    ref, extra_task = compose_dependency_task(
        workdir=workdir,
        output_ref=output_ref,
        target_contract_id=target_contract_id,
        selected_dependencies=frozen["selected_dependencies"] if frozen else None,
    )
    extra_inputs = (ref,)
    return build_specialist_assembly(
        workdir=workdir,
        decision=decision,
        target_contract_id=target_contract_id,
        output_ref=output_ref,
        manifest_ref=manifest_ref,
        role_ref="prover/research.md",
        extra_refs=(),
        extra_inputs=extra_inputs,
        extra_task=extra_task,
        delivery_task=build_delivery_instructions(workdir, output_ref),
        separate_reporter=reporter_owned(workdir),
    )


def run(*, llm: Any, workdir: Path, **kwargs):
    from pace.agents.director.dispatch import run_specialist

    return run_specialist(llm=llm, workdir=workdir, agent_id="prover", **kwargs)


def compose_dependency_task(**kwargs: Any) -> tuple[str, str]:
    from pace.artifacts.results import write_dependency_manifest

    ref, entries = write_dependency_manifest(**kwargs)
    rows = [
        f"| {row['finding_id']} | `{row['source_ref']}` lines 1–{row['source_lines'][1]} | `{row['verification_ref']}` |"
        for row in entries
    ]
    if not rows:
        rows = ["| None | No checked finding dependency is available. | — |"]
    return ref, render_prompt_template(
        "prover/dependencies.md",
        workdir=kwargs["workdir"],
        table="\n".join(rows),
        manifest_ref=ref,
    )


def build_delivery_instructions(workdir: Path, output_ref: str) -> str:
    common = delivery_instruction(workdir, output_ref, with_delta=True)
    return common


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
