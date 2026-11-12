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


def select_writing_task(root: Path, action: str, state_ref: str) -> None:
    _write_json_atomic(
        Path(root) / writing_task_ref(root), {"action": action, "state_ref": state_ref}
    )


def active_writing_request(root: Path) -> dict[str, Any]:
    task = read_state(root, writing_task_ref(root))
    return {**task, "request": read_state(root, task["state_ref"])} if task else {}


def writing_artifacts(root: Path, *, snapshots: bool = False) -> dict[str, str]:
    from .research_report import outline_artifacts, report_artifacts
    from .theory_paper import paper_artifacts, paper_outline_artifacts

    return {
        **outline_artifacts(root, snapshots=snapshots),
        **report_artifacts(root, snapshots=snapshots),
        **paper_outline_artifacts(root, snapshots=snapshots),
        **paper_artifacts(root, snapshots=snapshots),
    }


def writing_complete(root: Path) -> bool:
    task = active_writing_request(root)
    if not task:
        return True
    key = {
        "outline": "research_outline",
        "report": "research_report",
        "paper-outline": "paper_outline",
        "paper": "paper_source",
    }[task["action"]]
    if key not in writing_artifacts(root):
        return False
    if task["action"] == "paper":
        from .theory_paper import paper_build_status

        return paper_build_status(root) == "COMPLETE"
    return True
