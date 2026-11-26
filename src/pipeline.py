from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pace.agents.director.harness import DirectorHarness
from pace.artifacts.workspace import CURRENT_TURN_REF, RunWorkspace
from pace.contracts.problem.loader import Problem
from pace.orchestrator.memory import (
    commit_round_memory,
    initialize_turn_memory,
    update_research_costs,
)
from pace.orchestrator.recovery import RecoveryError
from pace.orchestrator.results import publish_stage_findings, publish_terminal_report
from pace.orchestrator.writing.tasks import (
    active_writing_request,
    complete_writing,
    prepare_writing_request,
)
from pace.pipeline_types import PipelineConfig, PipelineResult
from pace.runtime.llm import build_llm
from pace.runtime.sessions import run_session


def _previous_turn_context(ws: RunWorkspace) -> dict[str, Any]:
    """Carry immutable prior work as reading context, never inherited authority."""
    refs = []
    for name in (
        "final_report.json",
        "state/proof_state.json",
        "state/director_state.json",
    ):
        ref = ws.work_ref(name)
        if not ws.path(ref).is_file():
            continue
        refs.append(ref)
        value = json.loads(ws.path(ref).read_text(encoding="utf-8"))
        refs.extend(value.get("completed_refs", value.get("completed_result_refs", ())))
        artifacts = value.get("artifact_refs", {})
        refs.extend(
            artifacts.get(key, "")
            for key in ("proof", "candidate", "verification", "auditor", "kernel")
        )
    for ref in tuple(refs):
        if ref and Path(ref).name == "agent_result.json" and ws.path(ref).is_file():
            refs.extend(
                json.loads(ws.path(ref).read_text(encoding="utf-8")).get(
                    "published_refs", ()
                )
            )
    valid = []
    prefix = f"work/{ws.turn_id}/"
    for ref in dict.fromkeys(refs):
        if (
            isinstance(ref, str)
            and ref.startswith(prefix)
            and "/resources/" not in ref
            and ws.path(ref).is_file()
            and ws.path(ref).resolve().is_relative_to(ws.root.resolve())
        ):
            valid.append(ref)
    return {
        "source_turn": ws.turn_id,
        "authority": "SEARCH_CONTEXT_ONLY",
        "input_refs": valid,
    }


class ProofPipeline:
    """Select and manage turns; the Director harness owns each turn's execution."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.output_root = Path(config.output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.director = DirectorHarness(
            config,
            initialize_memory=initialize_turn_memory,
            commit_memory=commit_round_memory,
            commit_results=publish_stage_findings,
            publish_terminal=publish_terminal_report,
        )
        self._writing_llm_instance = None

    def run(
        self,
        problem: Problem,
        *,
        run_dir: Path | None = None,
        on_started=None,
        research_context: dict[str, Any] | None = None,
        writing_action: str = "",
        outline_ref: str = "",
        additional_requirements: str = "",
    ) -> PipelineResult | Path:
        ws = (
            RunWorkspace.open_existing(run_dir)
            if run_dir is not None
            else RunWorkspace.create(self.output_root, problem.problem_id)
        )
        with ws.execution_lock():
            if writing_action:
                request_ref = prepare_writing_request(
                    ws,
                    problem,
                    action=writing_action,
                    outline_ref=outline_ref,
                    requirements=additional_requirements,
                    resource_variant=self.config.resource_variant,
                )
                with self._session(ws, writing_request_ref=request_ref):
                    return complete_writing(ws, writing_action, self._writing_llm())
            if run_dir is not None:
                from pace.runtime.input_protection import (
                    recover_open_input_snapshots,
                )

                recover_open_input_snapshots(ws.root)
            request = problem.text
            source_turn_id = ""
            if run_dir is not None:
                previous_context = _previous_turn_context(ws)
                previous = json.loads(ws.path(CURRENT_TURN_REF).read_text())
                if previous["status"] != "completed":
                    source_turn_id = ws.turn_id
                original = ws.path(ws.work_ref("input/problem.md")).read_text(
                    encoding="utf-8"
                )
                ws.start_turn()
                context_ref = ws.work_ref("input/continuation_context.json")
                ws.write_json(context_ref, previous_context)
                research_context = {
                    "input_refs": (
                        context_ref,
                        *previous_context["input_refs"],
                        *((research_context or {}).get("input_refs", ())),
                    )
                }
                problem = Problem(
                    problem_id=ws.problem_id,
                    text=original.rstrip()
                    + f"\n\n## User follow-up ({ws.turn_id})\n\n"
                    + request,
                    source=problem.source,
                )
            setup = self.director.prepare(
                ws,
                problem,
                request=request,
                research_context=research_context,
                source_turn_id=source_turn_id,
            )
            try:
                with self._session(ws):
                    if on_started is not None:
                        on_started(ws)
                    result = self.director.run(ws, problem, setup)
            except BaseException:
                ws.finish_turn("interrupted")
                raise
            ws.finish_turn("completed")
            return result

    def resume(
        self,
        workspace: RunWorkspace,
        checkpoint: dict[str, Any],
        continuation_action: str,
        *,
        finalize: bool = False,
    ) -> PipelineResult | Path:
        from pace.runtime.input_protection import recover_open_input_snapshots

        with workspace.execution_lock():
            if continuation_action == "WRITING":
                recover_open_input_snapshots(workspace.root)
                if finalize:
                    raise RecoveryError(
                        "finalization_requires_research_boundary",
                        "--finalize requires a research turn",
                    )
                if checkpoint != active_writing_request(workspace.root):
                    raise RecoveryError(
                        "invalid_writing_checkpoint",
                        "the writing request changed before resume",
                    )
                with self._session(
                    workspace, writing_request_ref=checkpoint["request"]["request_ref"]
                ):
                    return complete_writing(
                        workspace, checkpoint["action"], self._writing_llm()
                    )
            workspace.finish_turn("running")
            try:
                recover_open_input_snapshots(workspace.root)
                with self._session(workspace):
                    result = self.director.resume(
                        workspace, checkpoint, continuation_action, finalize=finalize
                    )
            except BaseException:
                workspace.finish_turn("interrupted")
                raise
            workspace.finish_turn("completed")
            return result

    def _writing_llm(self):
        if self._writing_llm_instance is None:
            self._writing_llm_instance = build_llm(
                provider=self.config.provider,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                web_search=False,
                timeout_seconds=self.config.timeout_seconds,
            )
        self._writing_llm_instance.profile = self.config.profile
        return self._writing_llm_instance

    @contextmanager
    def _session(self, workspace: RunWorkspace, *, writing_request_ref: str = ""):
        try:
            with run_session(
                workspace.root,
                config=self.config,
                writing_request_ref=writing_request_ref,
            ):
                yield
        finally:
            try:
                update_research_costs(workspace.root)
            except (OSError, ValueError, KeyError):
                logging.getLogger(__name__).warning(
                    "Research cost index refresh failed", exc_info=True
                )
