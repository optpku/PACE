"""Report author roles own fixed rules, task prompts and model calls."""

from __future__ import annotations

from pathlib import Path

from pace.agents.prompt_composer import compose_instructions
from pace.runtime.prompts import render_prompt_template
from pace.runtime.semantic import begin_artifact_call


def build_instructions(*, workdir: Path, resources: Path, outline: bool) -> str:
    role = "RESEARCH_REPORT_OUTLINE" if outline else "RESEARCH_REPORT_WRITER"
    ref = "report_writer" + ("/outline.md" if outline else "/writer.md")
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
    values = dict(
        run_directory=workdir.resolve(),
        request_ref=state["request_ref"],
        skill_ref=guides[1],
        output_ref=output_ref,
    )
    if not outline:
        values["outline_ref"] = state["outline_ref"]
    return render_prompt_template(
        "report_writer/outline_task.md" if outline else "report_writer/writer_task.md",
        workdir=workdir,
        resource_root=workdir / resources / "prompts",
        **values,
    )


def run(llm, prompt, *, workdir, outline: bool, input_refs, metadata):
    name = "research_report_outline" if outline else "research_report_writer"
    stage = (
        "HOST_RESEARCH_REPORT_OUTLINE" if outline else "HOST_RESEARCH_REPORT_WRITING"
    )
    return begin_artifact_call(
        llm,
        prompt,
        workdir=workdir,
        call_name=name,
        role=name.upper(),
        stage=stage,
        input_refs=input_refs,
        metadata=metadata,
    )
