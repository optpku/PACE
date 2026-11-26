"""Paper author roles own fixed rules, task prompts and model calls."""

from __future__ import annotations

from pathlib import Path

from pace.agents.prompt_composer import compose_instructions
from pace.runtime.prompts import render_prompt_template
from pace.runtime.semantic import begin_artifact_call


def build_instructions(*, workdir: Path, resources: Path, outline: bool) -> str:
    role = "THEORY_PAPER_OUTLINE" if outline else "THEORY_PAPER_WRITER"
    ref = "paper_writer" + ("/outline.md" if outline else "/writer.md")
    return compose_instructions(
        workdir=workdir,
        role=role,
        resources=resources,
        refs=(ref,),
        project_skills=True,
    )


def build_prompt(
    *,
    workdir: Path,
    resources: str,
    state: dict,
    guides: tuple,
    output_ref: str,
    outline: bool,
) -> str:
    task = render_prompt_template(
        "paper_writer/task.md",
        workdir=workdir,
        resource_root=workdir / resources / "prompts",
        run_directory=workdir.resolve(),
        request_ref=state["request_ref"],
        skill_ref=guides[1],
        output_ref=output_ref,
    )
    if outline:
        return task + render_prompt_template(
            "paper_writer/outline_delivery.md",
            workdir=workdir,
            resource_root=workdir / resources / "prompts",
        )
    return task + render_prompt_template(
        "paper_writer/writer_delivery.md",
        workdir=workdir,
        resource_root=workdir / resources / "prompts",
        outline_ref=state["outline_ref"],
        output_directory=Path(output_ref).parent,
        template_ref=resources + "/templates/neurips2026/neurips_2026.sty",
    )


def run(
    llm, prompt, *, workdir, outline: bool, input_refs, metadata, on_transport_success=None
):
    name = "theory_paper_outline" if outline else "theory_paper_writer"
    stage = "HOST_" + name.upper()
    return begin_artifact_call(
        llm,
        prompt,
        workdir=workdir,
        call_name=name,
        role=name.upper(),
        stage=stage,
        input_refs=input_refs,
        metadata=metadata,
        on_transport_success=on_transport_success,
    )
