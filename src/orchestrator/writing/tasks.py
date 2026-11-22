"""Shared writing navigation and the explicit task selected for resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.artifacts.workspace import _write_json_atomic, work_ref


def read_state(root: Path, ref: str) -> dict[str, Any]:
    path = Path(root) / ref
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def writing_task_ref(root: Path) -> str:
    return work_ref(root, "state/writing_task.json")


def select_writing_task(root: Path, action: str, state_ref: str) -> None:
    _write_json_atomic(
        Path(root) / writing_task_ref(root), {"action": action, "state_ref": state_ref}
    )


def active_writing_request(root: Path) -> dict[str, Any]:
    task = read_state(root, writing_task_ref(root))
    return {**task, "request": read_state(root, task["state_ref"])} if task else {}


def writing_artifacts(root: Path, *, snapshots: bool = False) -> dict[str, str]:
    from .paper import paper_artifacts, paper_outline_artifacts
    from .report import outline_artifacts, report_artifacts

    return {
        **outline_artifacts(root, snapshots=snapshots),
        **report_artifacts(root, snapshots=snapshots),
        **paper_outline_artifacts(root, snapshots=snapshots),
        **paper_artifacts(root, snapshots=snapshots),
    }


def pending_writing_request(root: Path) -> dict[str, Any]:
    """Select only a writing request whose own delivery is unfinished."""
    task = active_writing_request(root)
    if not task:
        return {}
    key = {
        "outline": "research_outline",
        "report": "research_report",
        "paper-outline": "paper_outline",
        "paper": "paper_source",
    }[task["action"]]
    artifacts = writing_artifacts(root)
    if key not in artifacts or (
        task["action"] == "paper" and "paper_pdf" not in artifacts
    ):
        return task
    return {}


def complete_writing(ws, action, llm):
    from .paper import PAPER_OUTLINE_REF, PAPER_SOURCE_REF, TheoryPaperWriter
    from .report import (
        RESEARCH_OUTLINE_REF,
        RESEARCH_REPORT_REF,
        ResearchReportOutliner,
        ResearchReportWriter,
    )

    if action == "outline":
        completed, output = (
            ResearchReportOutliner(llm=llm, workdir=ws.root).update(),
            RESEARCH_OUTLINE_REF,
        )
    elif action == "report":
        completed, output = (
            ResearchReportWriter(llm=llm, workdir=ws.root).update(),
            RESEARCH_REPORT_REF,
        )
    elif action == "paper-outline":
        completed, output = (
            TheoryPaperWriter(llm=llm, workdir=ws.root).update_outline(),
            PAPER_OUTLINE_REF,
        )
    elif action == "paper":
        completed, output = (
            TheoryPaperWriter(llm=llm, workdir=ws.root).update(),
            PAPER_SOURCE_REF,
        )
    else:
        raise ValueError(f"Unknown writing action: {action}")
    if not completed:
        raise ValueError(f"{action} failed; resume its saved writing task")
    return ws.root / output


def request_writing(
    ws, problem, *, action, outline_ref, requirements, resource_variant, llm
):
    from .paper import (
        PAPER_OUTLINE_REF,
        TheoryPaperWriter,
        paper_outline_state_ref,
        paper_state_ref,
    )
    from .report import (
        RESEARCH_OUTLINE_REF,
        ResearchReportOutliner,
        ResearchReportWriter,
        outline_state_ref,
        report_state_ref,
    )

    if action == "outline":
        ResearchReportOutliner(llm=llm, workdir=ws.root).request(
            problem.text, requirements=requirements, resource_variant=resource_variant
        )
        state_ref = outline_state_ref(ws.root)
    elif action == "report":
        ResearchReportWriter(llm=llm, workdir=ws.root).request(
            outline=outline_ref or RESEARCH_OUTLINE_REF,
            message=problem.text,
            requirements=requirements,
            resource_variant=resource_variant,
        )
        state_ref = report_state_ref(ws.root)
    elif action == "paper-outline":
        TheoryPaperWriter(llm=llm, workdir=ws.root).request_outline(
            problem.text, requirements=requirements, resource_variant=resource_variant
        )
        state_ref = paper_outline_state_ref(ws.root)
    elif action == "paper":
        TheoryPaperWriter(llm=llm, workdir=ws.root).request(
            outline=outline_ref or PAPER_OUTLINE_REF,
            message=problem.text,
            requirements=requirements,
            resource_variant=resource_variant,
        )
        state_ref = paper_state_ref(ws.root)
    else:
        raise ValueError(f"Unknown writing action: {action}")
    select_writing_task(ws.root, action, state_ref)
    return complete_writing(ws, action, llm)
