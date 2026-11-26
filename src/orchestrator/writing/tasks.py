"""Shared writing navigation and the explicit task selected for resume."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pace.artifacts.results import relocate_links
from pace.artifacts.workspace import _write_json_atomic, _write_text_atomic, work_ref
from pace.runtime.math_format import require_math_format
from pace.runtime.semantic import (
    ArtifactCallHandle,
    commit_artifact_call_failure,
    commit_artifact_call_success,
)


def read_state(root: Path, ref: str) -> dict[str, Any]:
    path = Path(root) / ref
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def read_selected_outline(root, outline, default_ref, state_ref, message):
    """Inherit instructions only from the request that produced the selected outline."""
    source = (root / outline).resolve()
    parent = read_state(root, state_ref)
    current = (root / default_ref).resolve()
    saved = parent.get("outline_ref")
    if source == current and parent and not saved:
        raise ValueError("The current outline request has not delivered an outline")
    body = source.read_text(encoding="utf-8")
    if not body.strip():
        raise ValueError("Selected outline is empty")
    if saved and source in (current, (root / saved).resolve()):
        original = (root / parent["request_ref"]).read_text(encoding="utf-8")
    elif message.strip():
        original = ""
    else:
        raise ValueError("Supply --message with a standalone outline")
    return source, body, original


def run_writing_author(
    writer,
    llm,
    prompt,
    *,
    workdir,
    state_ref,
    state,
    output_ref,
    diagnostic_ref,
    outline,
    input_refs,
    metadata,
):
    """Accept a valid source at transport completion, before local publication."""

    def accept(delivery: ArtifactCallHandle) -> None:
        source = workdir / output_ref
        try:
            body = source.read_text(encoding="utf-8")
            if not body.strip():
                raise ValueError(f"Writing output is empty: {output_ref}")
            require_math_format(body, markdown=source.suffix != ".tex")
        except (OSError, ValueError):
            commit_artifact_call_failure(
                delivery,
                workdir=workdir,
                failure_class="REPRESENTATION",
                retry_class="MODEL_REPRESENTATION",
                diagnostics_ref=diagnostic_ref,
            )
            raise
        state["accepted_source_ref"] = output_ref
        state["accepted_call"] = asdict(delivery)
        _write_json_atomic(workdir / state_ref, state)

    handle = writer(
        llm,
        prompt,
        workdir=workdir,
        outline=outline,
        input_refs=input_refs,
        metadata=metadata,
        on_transport_success=accept,
    )
    if handle.transport_status != "SUCCESS":
        commit_artifact_call_failure(
            handle,
            workdir=workdir,
            failure_class="INTERFACE",
            retry_class=handle.retry_class,
            diagnostics_ref=diagnostic_ref,
        )
        raise ValueError(f"Writing transport failed: {handle.error_code}")


def settle_writing_call(root, state):
    if saved := state.get("accepted_call"):
        commit_artifact_call_success(
            ArtifactCallHandle(**saved),
            workdir=root,
            artifact_ref=state["accepted_source_ref"],
        )
        del state["accepted_call"]


def project_accepted_text(
    root: Path, state_ref: str, source_key: str, destination_ref: str
) -> bool:
    state = read_state(root, state_ref)
    source_ref = state.get("accepted_source_ref") or state.get(source_key)
    if not source_ref:
        return False
    destination = root / destination_ref
    if state.get("accepted_source_ref") or not destination.is_file():
        settle_writing_call(root, state)
        source = root / source_ref
        _write_text_atomic(
            destination,
            relocate_links(source.read_text(encoding="utf-8"), source, destination),
        )
        state[source_key] = source_ref
        state.pop("accepted_source_ref", None)
        _write_json_atomic(root / state_ref, state)
    return True


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
    if (
        task["request"].get("accepted_source_ref")
        or key not in artifacts
        or (task["action"] == "paper" and "paper_pdf" not in artifacts)
    ):
        return task
    return {}


def complete_writing(ws, action, llm):
    from .paper import PAPER_OUTLINE_REF, TheoryPaperWriter, paper_artifacts
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
        completed = TheoryPaperWriter(llm=llm, workdir=ws.root).update()
        if completed:
            return ws.root / paper_artifacts(ws.root)["paper_source"]
    else:
        raise ValueError(f"Unknown writing action: {action}")
    if not completed:
        raise ValueError(f"{action} failed; resume its saved writing task")
    return ws.root / output


def prepare_writing_request(
    ws, problem, *, action, outline_ref, requirements, resource_variant
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
        ResearchReportOutliner(llm=None, workdir=ws.root).request(
            problem.text, requirements=requirements, resource_variant=resource_variant
        )
        state_ref = outline_state_ref(ws.root)
    elif action == "report":
        ResearchReportWriter(llm=None, workdir=ws.root).request(
            outline=outline_ref or RESEARCH_OUTLINE_REF,
            message=problem.text,
            requirements=requirements,
            resource_variant=resource_variant,
        )
        state_ref = report_state_ref(ws.root)
    elif action == "paper-outline":
        TheoryPaperWriter(llm=None, workdir=ws.root).request_outline(
            problem.text, requirements=requirements, resource_variant=resource_variant
        )
        state_ref = paper_outline_state_ref(ws.root)
    elif action == "paper":
        TheoryPaperWriter(llm=None, workdir=ws.root).request(
            outline=outline_ref or PAPER_OUTLINE_REF,
            message=problem.text,
            requirements=requirements,
            resource_variant=resource_variant,
        )
        state_ref = paper_state_ref(ws.root)
    else:
        raise ValueError(f"Unknown writing action: {action}")
    select_writing_task(ws.root, action, state_ref)
    return read_state(ws.root, state_ref)["request_ref"]
