"""Presentation-only Web role using committed evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from pace.runtime.prompts import render_prompt_template


def build_prompt(
    *,
    workdir: Path,
    event: dict[str, Any],
    input_refs: Iterable[str],
    output_refs: dict[str, str],
    brief: bool,
) -> str:
    return render_prompt_template(
        "web_reporter/task.md",
        workdir=workdir,
        task_facts=json.dumps(event, ensure_ascii=False),
        input_refs="\n".join(f"- `{ref}`" for ref in input_refs),
        output_refs="\n".join(
            f"- {name}: `{ref}`" for name, ref in output_refs.items()
        ),
        brief_task=(
            render_prompt_template("web_reporter/brief_task.md", workdir=workdir)
            if brief
            else ""
        ),
    )


def run(llm, prompt, *, workdir, input_refs, agent_home):
    from pace.runtime.semantic import begin_artifact_call

    return begin_artifact_call(
        llm,
        prompt,
        workdir=workdir,
        call_name="web_explanation",
        role="web_reporter",
        stage="WEB_PRESENTATION",
        input_refs=input_refs,
        metadata={"agent_home": agent_home},
    )


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    from pace.agents.prompt_composer import compose_instructions

    return compose_instructions(
        workdir=workdir,
        role="WEB_REPORTER",
        resources=resources,
        refs=("web_reporter/role.md",),
    )
