"""Application recovery selects the run; its Director owns the saved turn."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pace.artifacts.workspace import RunWorkspace
from pace.pipeline_types import PipelineResult


class RecoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RecoveryRequest:
    """One exact committed continuation; recovery never chooses a new action."""

    workspace: RunWorkspace
    checkpoint: dict[str, Any]
    continuation_action: str


def resume_existing_run(
    run_dir: Path,
    *,
    continue_pipeline: Callable[[RecoveryRequest], PipelineResult | Path] | None = None,
) -> PipelineResult | Path:
    try:
        workspace = RunWorkspace.open_existing(run_dir)
    except ValueError as exc:
        raise RecoveryError("invalid_run_directory", str(exc)) from exc
    # Restore derived records before terminal shortcuts or publication checks.
    # Only parent-committed handoffs are replayed; this never calls a model.
    from pace.artifacts.setup import load_setup
    from pace.contracts.research.memory import enabled

    setup = load_setup(workspace.root)
    if (setup is None or setup["phase"] == "READY") and enabled(workspace.root):
        from pace.orchestrator.memory import (
            commit_research_memory,
            refresh_memory_index,
        )

        try:
            commit_research_memory(workspace.root)
            refresh_memory_index(workspace.root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryError("invalid_research_memory", str(exc)) from exc

    from pace.orchestrator.writing.tasks import pending_writing_request

    writing = pending_writing_request(workspace.root)
    if writing:
        if continue_pipeline is None:
            raise RecoveryError(
                "continuation_hook_unavailable",
                "writing recovery requires a pipeline continuation",
            )
        return continue_pipeline(RecoveryRequest(workspace, writing, "WRITING"))
    from pace.agents.director.recovery import resume_turn

    return resume_turn(workspace, continue_pipeline=continue_pipeline)
