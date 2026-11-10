"""Shared writing navigation and the explicit task selected for resume."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from pace.runtime.output.workspace import _write_json_atomic, work_ref


def read_state(root: Path, ref: str) -> dict[str, Any]:
    path = Path(root) / ref
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def writing_task_ref(root: Path) -> str:
    return work_ref(root, "state/writing_task.json")


def select_writing_task(root: Path, action: str, state_ref: str, *, scope: str = "") -> None:
    _write_json_atomic(
        Path(root) / writing_task_ref(root), {"action": action, "state_ref": state_ref, "scope": scope}
    )


def active_writing_request(root: Path) -> dict[str, Any]:
    task = read_state(root, writing_task_ref(root))
    return {**task, "request": read_state(root, task["state_ref"])} if task else {}


def writing_artifacts(root: Path, *, snapshots: bool = False, scope: str | None = None) -> dict[str, str]:
    from .research_report import outline_artifacts, report_artifacts
    from .theory_paper import paper_artifacts, paper_outline_artifacts

    if scope is None:
        scope = active_writing_request(root).get("scope", "")
    return {
        **outline_artifacts(root, scope=scope),
        **report_artifacts(root, snapshots=snapshots, scope=scope),
        **paper_outline_artifacts(root, scope=scope),
        **paper_artifacts(root, snapshots=snapshots, scope=scope),
    }


def terminal_delivery_pending(root: Path) -> bool:
    from .terminal_delivery import delivery_complete
    terminal = read_state(root, work_ref(root, "final_report.json"))
    return (terminal.get("run_status") == "completed"
            and not delivery_complete(root))


def turn_continuation_pending(root: Path) -> bool:
    """A saved mathematical workflow or its automatic delivery still owns the turn.

    Only detect ownership here. The recovery dispatcher validates the saved
    checkpoint and chooses its exact action; writing never reconstructs it.
    A standalone writing workspace has no mathematical checkpoint to resume.
    """
    if terminal_delivery_pending(root):
        return True
    terminal = read_state(root, work_ref(root, "final_report.json"))
    if terminal.get("run_status") == "completed":
        return False
    return any((Path(root) / work_ref(root, f"state/{name}.json")).is_file()
               for name in ("setup_state", "proof_state", "director_state"))


def selected_writing_complete(root: Path) -> bool:
    task = active_writing_request(root)
    if not task:
        return True
    key = {
        "outline": "research_outline",
        "report": "research_report",
        "paper-outline": "paper_outline",
        "paper": "paper_source",
    }[task["action"]]
    if key not in writing_artifacts(root, snapshots=True):
        return False
    state = task["request"]
    if task["action"] in {"outline", "report"} and state.get("draft_ref"):
        # The accepted manuscript can precede its local publication receipt.
        # Finish that Host-only step before relinquishing the selected task.
        if not state.get("publication_complete"):
            return False
    if task["action"] == "paper":
        from .theory_paper import paper_build_status

        return (paper_build_status(root, task.get("scope", "")) == "COMPLETE"
                and (not state.get("draft_ref") or state.get("build_publication")
                     == [state.get("build_ref"), "COMPLETE"]))
    return True


def release_completed_writing_task(root: Path) -> None:
    """Release navigation only; keep accepted writing and review checkpoints."""
    if turn_continuation_pending(root) and selected_writing_complete(root):
        (Path(root) / writing_task_ref(root)).unlink(missing_ok=True)


def writing_complete(root: Path) -> bool:
    return not terminal_delivery_pending(root) and selected_writing_complete(root)


def with_writing_outputs(result):
    """Expose delivered files without changing the bound mathematical report."""
    from dataclasses import replace
    artifacts = writing_artifacts(result.run_dir, snapshots=True, scope="")
    fields = {"outline_file": "research_outline", "research_report_file": "research_report",
              "paper_outline_file": "paper_outline", "paper_source_file": "paper_source",
              "paper_pdf_file": "paper_pdf"}
    return replace(result, **{field: result.run_dir / artifacts[key] if key in artifacts else None
                              for field, key in fields.items()})
