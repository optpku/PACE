from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.reporting.research_report import (
    RESEARCH_OUTLINE_REF,
    RESEARCH_REPORT_REF,
    ResearchReportOutliner,
    ResearchReportWriter,
    outline_state_ref,
    report_state_ref,
)
from pace.reporting.theory_paper import (
    PAPER_OUTLINE_REF,
    PAPER_SOURCE_REF,
    TheoryPaperWriter,
    paper_outline_state_ref,
    paper_state_ref,
)
from pace.reporting.writing import (
    active_writing_request,
    select_writing_task,
)
from pace.runtime.output.workspace import CURRENT_TURN_REF
from pace.agents.orientation import (
    proof_assessment_schema,
    run_problem_orientation,
)
from pace.agents.research.global_director import run_global_director
from pace.contracts.problem.loader import Problem
from pace.contracts.proof.binding import load_active_target
from pace.orchestrator.director_led import (
    DirectorLedEngine,
    DirectorLedEngineError,
    DirectorLedState,
)
from pace.orchestrator.director_led_adapters import (
    build_default_composer,
    build_default_specialist_handlers,
)
from pace.orchestrator.director_led_finalization import (
    finalize_director_led,
)
from pace.orchestrator.director_verifier import build_default_verifier
from pace.orchestrator.finding_publication import publish_stage_findings
from pace.pipeline_types import (
    MAX_PROOF_STATES,
    PipelineConfig,
    PipelineResult,
)
from pace.runtime.context_state import build_context_manifest
from pace.runtime.output.memory import (
    initialize_memory,
    refresh_memory_index,
)
from pace.runtime.output.profiling import ProfileRecorder
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.prompts import (
    materialize_run_resources,
    prompt_root,
    validate_resource_snapshot,
)
from pace.runtime.output.setup import load_setup, save_setup, setup_ref
from pace.runtime.output.delivery_pause import (
    DeliveryPaused,
    delivery_pause,
    pause_delivery,
    clear_delivery_pause,
)
from pace.runtime.semantic import StructuredSemanticResult
from pace.runtime.llm import build_llm
from pace.runtime.schema import validate_json_instance


__all__ = ["ProofPipeline"]


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


def _canonical_problem_text(value: Any) -> str:
    """Use the run-wide UTF-8/LF representation for the literal problem."""

    return (
        str(value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    )


def _director_control_context(
    *,
    allow_research_continuation: bool,
    research_context: dict[str, Any] | None,
    mode: str,
) -> str:
    """Expose only scheduling metadata to the Director prompt."""

    rows = [
        f"allow_research_continuation={bool(allow_research_continuation)}",
        f"mode={str(mode or '').strip()}",
    ]
    if isinstance(research_context, dict):
        refs = research_context.get("input_refs")
        if isinstance(refs, (list, tuple)):
            visible = [str(ref).strip() for ref in refs if str(ref).strip()]
            if visible:
                rows.append("inherited_input_refs=" + ",".join(visible))
    return "\n".join(rows)


def _proof_budget_from_assessment(assessment: dict[str, Any]) -> dict[str, Any]:
    errors = validate_json_instance(assessment, proof_assessment_schema())
    if errors:
        raise ValueError(
            "invalid proof assessment: " + json.dumps(errors, ensure_ascii=False)
        )
    difficulty = assessment["difficulty"]
    complexity = max(
        assessment["plan_node_level"], assessment["dependency_depth_level"]
    )
    if difficulty == "research":
        total = 10 + 3 * (complexity - 1)
    elif difficulty == "routine":
        total = 2
    else:
        total = 3 * complexity
    return {
        "max_total_proof_states": min(MAX_PROOF_STATES, total),
        "cap_origin": "ORIENTATION_SCHEMA",
    }


class ProofPipeline:
    """Persist a problem turn and run its selected proof or research workflow."""

    def __init__(
        self,
        config: PipelineConfig,
    ) -> None:
        self.config = config
        self.output_root = Path(config.output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._core_llm_instance: Any | None = None
        self._director_llm_instance: Any | None = None
        self._survey_llm_instance: Any | None = None

    def _llm(self) -> Any:
        if self._core_llm_instance is None:
            self._core_llm_instance = build_llm(
                provider=self.config.provider,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                web_search=self.config.web_search_mode != "off",
                timeout_seconds=self.config.timeout_seconds,
            )
        self._core_llm_instance.profile = self.config.profile
        return self._core_llm_instance

    def _survey_llm(self) -> Any:
        if self._survey_llm_instance is None:
            self._survey_llm_instance = build_llm(
                provider=self.config.provider,
                model=self.config.web_search_model,
                reasoning_effort=self.config.web_search_reasoning_effort,
                web_search=self.config.web_search_mode != "off",
                timeout_seconds=self.config.timeout_seconds,
            )
        self._survey_llm_instance.profile = self.config.profile
        return self._survey_llm_instance

    def _director_llm(self) -> Any:
        if self._director_llm_instance is None:
            self._director_llm_instance = build_llm(
                provider=self.config.provider,
                model=self.config.model,
                reasoning_effort="none"
                if self.config.reasoning_effort == "none"
                else "low",
                web_search=self.config.web_search_mode != "off",
                timeout_seconds=self.config.timeout_seconds,
            )
        self._director_llm_instance.profile = self.config.profile
        return self._director_llm_instance

    def run(
        self,
        problem: Problem,
        *,
        run_dir: Path | None = None,
        on_started=None,
        allow_research_continuation: bool = True,
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
            if writing_action == "outline":
                outliner = self._outliner(ws)
                outliner.request(
                    problem.text,
                    requirements=additional_requirements,
                    resource_variant=self.config.resource_variant,
                )
                selected_state = outline_state_ref(ws.root)
            elif writing_action == "report":
                writer = self._writer(ws)
                writer.request(
                    outline=outline_ref or RESEARCH_OUTLINE_REF,
                    message=problem.text,
                    requirements=additional_requirements,
                    resource_variant=self.config.resource_variant,
                )
                selected_state = report_state_ref(ws.root)
            elif writing_action == "paper-outline":
                self._paper_writer(ws).request_outline(
                    problem.text,
                    requirements=additional_requirements,
                    resource_variant=self.config.resource_variant,
                )
                selected_state = paper_outline_state_ref(ws.root)
            elif writing_action == "paper":
                self._paper_writer(ws).request(
                    outline=outline_ref or PAPER_OUTLINE_REF,
                    message=problem.text,
                    requirements=additional_requirements,
                    resource_variant=self.config.resource_variant,
                )
                selected_state = paper_state_ref(ws.root)
            elif writing_action:
                raise ValueError(f"Unknown writing action: {writing_action}")
            if writing_action:
                from pace.runtime.sessions import run_session

                select_writing_task(ws.root, writing_action, selected_state)
                with run_session(ws.root, profile=self.config.profile, config=self.config):
                    return self._complete_writing(ws, writing_action)
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
            ws.write_text(ws.work_ref("input/request.md"), request)
            ws.write_text(
                ws.work_ref("input/problem.md"), _canonical_problem_text(problem.text)
            )
            self._prepare_setup(
                ws, problem, allow_research_continuation=allow_research_continuation,
                research_context=research_context, source_turn_id=source_turn_id,
            )
            if on_started is not None:
                try:
                    on_started(ws)
                except BaseException:
                    ws.finish_turn("interrupted")
                    raise
            from pace.runtime.sessions import run_session

            try:
                with run_session(ws.root, profile=self.config.profile, config=self.config):
                    result = self._run_initialized(
                        ws, problem, allow_research_continuation=allow_research_continuation,
                        research_context=research_context,
                    )
            except BaseException:
                ws.finish_turn("interrupted")
                raise
            ws.finish_turn("completed")
            return result

    def _outliner(self, ws: RunWorkspace) -> ResearchReportOutliner:
        return ResearchReportOutliner(
            llm=self._llm(),
            composer_llm=self._director_llm(),
            workdir=ws.root,
        )

    def _writer(self, ws: RunWorkspace) -> ResearchReportWriter:
        return ResearchReportWriter(
            llm=self._llm(), composer_llm=self._director_llm(), workdir=ws.root
        )

    def _paper_writer(self, ws: RunWorkspace) -> TheoryPaperWriter:
        return TheoryPaperWriter(
            llm=self._llm(), composer_llm=self._director_llm(), workdir=ws.root
        )


    def _complete_writing(self, ws: RunWorkspace, action: str) -> Path:
        if action == "outline":
            completed, output = self._outliner(ws).update(), RESEARCH_OUTLINE_REF
        elif action == "report":
            completed, output = self._writer(ws).update(), RESEARCH_REPORT_REF
        elif action == "paper-outline":
            completed, output = self._paper_writer(ws).update_outline(), PAPER_OUTLINE_REF
        elif action == "paper":
            completed, output = self._paper_writer(ws).update(), PAPER_SOURCE_REF
        else:
            raise ValueError(f"Unknown writing action: {action}")
        if not completed:
            raise ValueError(f"{action} failed; resume its saved writing task")
        return ws.root / output

    def _prepare_turn_handoff(self, ws: RunWorkspace, source_turn_id: str) -> None:
        """Bind the prior committed records for the next mathematical task."""
        context = json.loads(ws.path(ws.work_ref("input/continuation_context.json")).read_text())
        refs = tuple(dict.fromkeys(context["input_refs"]))
        ws.write_text_atomic(ws.work_ref("input/handoff.md"),
            f"# Previous mathematical work: {source_turn_id}\n\n"
            + "These records are search context. The current literal target remains authoritative.\n\n"
            + "\n".join(f"- `{ref}`" for ref in refs) + "\n")

    def _commit_execution_policies(
        self, ws: RunWorkspace, policy: dict[str, Any]
    ) -> None:
        resources = ws.path(ws.work_ref("resources"))
        if resources.exists():
            validate_resource_snapshot(
                resources, resource_variant=policy["resource_variant"]
            )
        else:
            materialize_run_resources(
                ws.root, resource_variant=policy["resource_variant"]
            )
        if policy["mode"] is None:
            return
        ws.write_json(
            ws.work_ref("state/research_execution_policy.json"),
            {
                "schema_version": "research_execution_policy/v1",
                "resource_variant": policy["resource_variant"],
                "mode": policy["mode"],
                "requested_mode": policy["requested_mode"],
                "mode_source": policy.get("mode_source", "CLI"),
                "mode_reason": policy.get("mode_reason", ""),
                "control_context": policy["control_context"],
                "max_calls": policy.get("max_calls"),
                "max_seconds": policy.get("max_seconds"),
                "max_rounds": policy.get("max_rounds"),
            },
        )

    def _prepare_setup(
        self,
        ws: RunWorkspace,
        problem: Problem,
        *,
        allow_research_continuation: bool = True,
        research_context: dict[str, Any] | None = None,
        source_turn_id: str = "",
    ) -> dict[str, Any]:
        """Save initialization before callbacks or cancellable model work."""
        state = load_setup(ws.root)
        if state is None:
            state = {
                "schema_version": "setup_state/v2",
                "turn_id": ws.turn_id,
                "problem_text": _canonical_problem_text(problem.text),
                "phase": "RESOURCES",
                "policy": {
                    "resource_variant": self.config.resource_variant,
                    "max_proof_states": self.config.max_proof_states,
                    "max_calls": self.config.max_calls,
                    "max_seconds": self.config.max_seconds,
                    "max_rounds": self.config.max_rounds,
                    "mode": self.config.mode,
                    "requested_mode": self.config.mode,
                    "control_context": _director_control_context(
                        allow_research_continuation=allow_research_continuation,
                        research_context=research_context,
                        mode=self.config.mode,
                    ),
                },
                "failure_refs": [],
                "source_turn_id": source_turn_id,
            }
            save_setup(ws.root, state)
        return state

    def _run_initialized(
        self, ws: RunWorkspace, problem: Problem, *,
        allow_research_continuation: bool = True,
        research_context: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """Finish shared initialization, then hand off to the saved engine."""
        profiler = ProfileRecorder(
            enabled=self.config.profile,
            workdir=ws.root,
            path=ws.path(ws.work_ref("events.jsonl")) if self.config.profile else None,
        )
        state = self._prepare_setup(
            ws, problem, allow_research_continuation=allow_research_continuation,
            research_context=research_context,
        )
        from pace.runtime.output.events import publish_progress

        publish_progress(ws.root, task_id='orientation', stage='orientation', status='started')
        reason = "resource_setup_failed"
        try:
            if state["phase"] == "RESOURCES":
                self._commit_execution_policies(ws, state["policy"])
                state["phase"] = (
                    "HANDOFF" if state.get("source_turn_id") else "ORIENTATION"
                )
                save_setup(ws.root, state)
            validate_resource_snapshot(
                ws.path(ws.work_ref("resources")),
                resource_variant=state["policy"]["resource_variant"],
            )
            if state["phase"] == "HANDOFF":
                self._prepare_turn_handoff(ws, state["source_turn_id"])
                state["phase"] = "ORIENTATION"
                save_setup(ws.root, state)
            def select_mode(mode, source, reason):
                state["policy"].update(mode=mode, mode_source=source, mode_reason=reason)
                state["policy"]["control_context"] = "\n".join(
                    f"mode={mode}" if line.startswith("mode=") else line
                    for line in state["policy"]["control_context"].splitlines()
                )
                # The orientation receipt is committed before this derived policy.
                save_setup(ws.root, state)
                self._commit_execution_policies(ws, state["policy"])
                publish_progress(ws.root, task_id="mode-selection", stage="orientation", status="completed", execution_mode=mode, mode_source=source, mode_reason=reason)
            reason = "problem_orientation_failed"
            if state["phase"] == "ORIENTATION":
                with profiler.span("pipeline.problem_orientation"):
                    orientation = run_problem_orientation(
                        llm=self._llm(), workdir=ws.root, problem=problem,
                        requested_mode=state["policy"].get("requested_mode"),
                        on_mode_selected=select_mode,
                    )
                ws.write_json(
                    ws.work_ref("orientation/problem_orientation_result.json"),
                    orientation,
                )
                if (
                    not isinstance(orientation, dict)
                    or orientation.get("stage_status") != "READY"
                ):
                    return self._finalize_director_led_setup_failure(
                        ws,
                        problem,
                        profiler,
                        reason_code=reason,
                        detail=orientation
                        if isinstance(orientation, dict)
                        else {"invalid_result": str(orientation)},
                    )
                state.update(phase="CONTEXT", orientation=orientation)
                save_setup(ws.root, state)
            reason = "director_led_setup_failed"
            if state["phase"] == "CONTEXT":
                if state["policy"]["mode"] == "proof":
                    from pace.orchestrator.proof_mode import initialize_proof_state
                    from pace.pipeline_types import DEFAULT_PROOF_LOOPS
                    initialize_proof_state(
                        ws,
                        max_loops=state["policy"]["max_proof_states"] or DEFAULT_PROOF_LOOPS,
                        resource_variant=state["policy"]["resource_variant"],
                    )
                else:
                    target = load_active_target(ws.root)
                    state_path = ws.path(ws.work_ref("state/director_state.json"))
                    if state_path.exists():
                        initial = DirectorLedState.from_json(
                            json.loads(state_path.read_text(encoding="utf-8"))
                        )
                        if (
                            initial.phase != "DIRECTOR"
                            or initial.round_index != 0
                            or initial.proof_states_used != 0
                            or initial.target_contract_id != target["contract_id"]
                        ):
                            raise ValueError(
                                "initialization cannot overwrite an active Director checkpoint"
                            )
                    else:
                        cap = self._initialize_director_led_context(
                            ws,
                            target_contract=target,
                            orientation_result=state["orientation"],
                            configured_cap=state["policy"]["max_proof_states"],
                        )
                        initial = DirectorLedState(
                            target_contract_id=target["contract_id"],
                            max_proof_states=cap,
                            phase="DIRECTOR",
                        )
                        ws.write_json(
                            ws.work_ref("state/director_state.json"), initial.to_json()
                        )
                state["phase"] = "READY"
                save_setup(ws.root, state)
        except Exception as exc:
            return self._finalize_director_led_setup_failure(
                ws,
                problem,
                profiler,
                reason_code=reason,
                detail={"error_type": type(exc).__name__, "message": str(exc)},
            )
        # Old setup failure reports are history, not the terminal state of a
        # successfully resumed search. Archive before publishing new progress.
        terminal = ws.path(ws.work_ref("final_report.json"))
        if terminal.is_file():
            value = json.loads(terminal.read_text(encoding="utf-8"))
            if value.get("terminal_kind") != "SETUP_FAILURE":
                raise ValueError("initialization found an unrelated terminal report")
            archive = ws.path(ws.work_ref("state/setup_attempts"))
            archive.mkdir(exist_ok=True)
            terminal.replace(
                archive
                / f"resolved-{len(list(archive.glob('resolved-*'))) + 1:04d}.json"
            )
        publish_progress(ws.root, task_id='orientation', stage='orientation', status='completed', notice='orientation_ready')
        if state["policy"]["mode"] == "proof":
            from pace.orchestrator.proof_mode import run_proof
            return run_proof(self, ws, problem)
        initial = DirectorLedState.from_json(
            json.loads(
                ws.path(ws.work_ref("state/director_state.json")).read_text(
                    encoding="utf-8"
                )
            )
        )
        if (
            initial.phase != "DIRECTOR"
            or initial.round_index != 0
            or initial.proof_states_used != 0
        ):
            raise ValueError(
                "initialization handoff must use the pristine Director checkpoint"
            )
        target_id = load_active_target(ws.root)["contract_id"]
        try:
            policy_ref = work_ref(ws.root, "state/research_execution_policy.json")
            policy = json.loads(ws.path(policy_ref).read_text(encoding="utf-8"))
            engine = self._build_director_led_engine(ws, target_contract_id=target_id)
            run_result = engine.run(
                control_context=policy["control_context"],
                # The bootstrap state is deliberately committed above.  Using
                # the recovery entry avoids a second implicit state creation.
                resume=True,
            )
        except DeliveryPaused:
            return self._paused_delivery_result(ws, problem, profiler)
        except DirectorLedEngineError as exc:
            return self._finalize_director_led_engine_failure(ws, problem, profiler, target_contract_id=target_id, error=exc)
        return self._finish_delivery(ws, problem, run_result.state, profiler)

    def resume(self, workspace, checkpoint, continuation_action, **kwargs):
        from pace.runtime.input_protection import recover_open_input_snapshots
        from pace.runtime.sessions import run_session

        with workspace.execution_lock():
            if continuation_action == "WRITING":
                recover_open_input_snapshots(workspace.root)
                with run_session(workspace.root, profile=self.config.profile, config=self.config):
                    return self._resume(workspace, checkpoint, continuation_action, **kwargs)
            workspace.finish_turn("running")
            try:
                recover_open_input_snapshots(workspace.root)
                with run_session(
                    workspace.root, profile=self.config.profile, config=self.config
                ):
                    result = self._resume(
                        workspace, checkpoint, continuation_action, **kwargs
                    )
            except BaseException:
                workspace.finish_turn("interrupted")
                raise
            workspace.finish_turn("completed")
            return result

    def _resume(
        self,
        workspace: RunWorkspace,
        checkpoint: dict[str, Any],
        continuation_action: str,
        *,
        allow_research_continuation: bool = True,
        finalize: bool = False,
    ) -> PipelineResult:
        if finalize and continuation_action in {"WRITING", "PROOF", "SETUP"}:
            from pace.orchestrator.recovery import RecoveryError

            raise RecoveryError(
                "finalization_requires_research_boundary",
                "--finalize requires an initialized research turn at a Director boundary",
            )
        if continuation_action == "WRITING":
            from pace.orchestrator.recovery import RecoveryError

            if checkpoint != active_writing_request(workspace.root):
                raise RecoveryError(
                    "invalid_writing_checkpoint",
                    "the writing request changed before resume",
                )
            return self._complete_writing(workspace, checkpoint["action"])
        if continuation_action == "PROOF":
            from pace.orchestrator.proof_mode import run_proof
            from pace.orchestrator.recovery import RecoveryError

            try:
                saved = json.loads(
                    workspace.path(
                        workspace.work_ref("state/proof_state.json")
                    ).read_text()
                )
                policy = json.loads(
                    workspace.path(
                        workspace.work_ref("state/research_execution_policy.json")
                    ).read_text()
                )
                if not isinstance(saved, dict) or not isinstance(policy, dict):
                    raise ValueError(
                        "proof checkpoint and execution policy must be objects"
                    )
                if (
                    policy.get("mode") != "proof"
                    or saved != checkpoint
                    or saved.get("engine") != "proof"
                    or saved.get("turn_id") != workspace.turn_id
                ):
                    raise ValueError(
                        "proof action differs from the committed turn and checkpoint"
                    )
            except (OSError, ValueError, TypeError) as exc:
                raise RecoveryError("invalid_proof_checkpoint", str(exc)) from exc

            return run_proof(
                self,
                workspace,
                Problem(
                    problem_id=workspace.problem_id,
                    text=workspace.path(
                        workspace.work_ref("input/problem.md")
                    ).read_text(),
                    source="",
                ),
            )
        from pace.orchestrator.recovery import (
            RecoveryError,
            load_director_checkpoint,
        )

        if continuation_action == "SETUP":
            saved = load_setup(workspace.root)
            if saved != checkpoint or saved is None:
                raise RecoveryError(
                    "invalid_setup_checkpoint",
                    "initialization checkpoint differs from the requested action",
                )
            return self._run_initialized(
                workspace,
                Problem(
                    problem_id=workspace.problem_id,
                    text=saved["problem_text"],
                    source=str(workspace.path(workspace.work_ref("input/problem.md"))),
                ),
            )
        if (
            not prompt_root(workspace.root).is_dir()
            or not workspace.path(
                work_ref(workspace.root, "resources"), "skills"
            ).is_dir()
        ):
            raise RecoveryError(
                "missing_resource_snapshot", "run has no complete resource snapshot"
            )
        try:
            policy = json.loads(
                workspace.path(
                    work_ref(workspace.root, "state/research_execution_policy.json")
                ).read_text(encoding="utf-8")
            )
            if policy["resource_variant"] not in {"standard", "weak"}:
                raise ValueError("resource selection is invalid")
            if workspace.path(
                work_ref(workspace.root, "resources/manifest.json")
            ).is_file():
                validate_resource_snapshot(
                    workspace.path(work_ref(workspace.root, "resources")),
                    resource_variant=policy["resource_variant"],
                )
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryError(
                "invalid_resource_snapshot",
                "run resource selection is missing or invalid",
            ) from exc
        return self._resume_director_led(
            workspace,
            checkpoint,
            continuation_action,
            committed_state=load_director_checkpoint(workspace),
            allow_research_continuation=allow_research_continuation,
            finalize=finalize,
        )

    def _resume_director_led(
        self,
        workspace: RunWorkspace,
        checkpoint: dict[str, Any],
        continuation_action: str,
        *,
        committed_state: dict[str, Any],
        allow_research_continuation: bool = True,
        finalize: bool = False,
    ) -> PipelineResult:
        """Continue a Director-led state without replaying Orientation."""

        profiler = ProfileRecorder(
            enabled=self.config.profile,
            workdir=workspace.root,
            path=workspace.path(work_ref(workspace.root, "events.jsonl"))
            if self.config.profile
            else None,
        )
        if not isinstance(committed_state, dict) or committed_state != checkpoint:
            raise DirectorLedEngineError(
                "committed Director state differs from the supplied continuation state"
            )
        state = DirectorLedState.from_json(checkpoint)
        if str(continuation_action or "") != state.phase:
            raise DirectorLedEngineError(
                "continuation action does not match Director-led state phase"
            )
        # All committed run artifacts are Host inputs.  Keep their reads and
        # validation inside one operational boundary so malformed bytes or
        # state types become a terminal diagnostic rather than escaping the
        # public resume API.
        problem_path = workspace.path(work_ref(workspace.root, "input/problem.md"))
        problem = Problem(
            problem_id=workspace.problem_id,
            text="",
            source=str(problem_path),
        )
        if finalize and state.phase not in {"DIRECTOR", "FINALIZE", "STOPPED"}:
            from pace.orchestrator.recovery import RecoveryError

            raise RecoveryError(
                "finalization_requires_research_boundary",
                "complete the pending task or verification before --finalize",
            )
        try:
            problem_text = problem_path.read_text(encoding="utf-8")
            problem = Problem(
                problem_id=workspace.problem_id,
                text=problem_text,
                source=str(problem_path),
            )

            if finalize and state.phase == "DIRECTOR":
                from dataclasses import replace
                from pace.runtime.output.events import append_event

                target = load_active_target(workspace.root)
                if target["contract_id"] != state.target_contract_id:
                    raise DirectorLedEngineError(
                        "finalization target differs from the saved checkpoint"
                    )
                state = replace(
                    state,
                    phase="FINALIZE",
                    terminal_result="FAILED",
                    stop_reason="USER_REQUESTED_FINALIZATION",
                )
                workspace.write_json(
                    work_ref(workspace.root, "state/director_state.json"),
                    state.to_json(),
                )
                append_event(
                    workspace.root,
                    "RESEARCH_FINALIZATION_REQUESTED",
                    round_index=state.round_index,
                    target_contract_id=state.target_contract_id,
                    proof_states_used=state.proof_states_used,
                    max_proof_states=state.max_proof_states,
                )
            if state.phase != "FAILED":
                paused = delivery_pause(workspace.root)
                self._update_publication(workspace, state, trigger=str(paused.get('trigger') or 'recovery-findings'), retry_errors=True)

            # Terminal phases have no mathematical scheduling left. Editorial
            # model work may remain, so resume the same finalization boundary.
            if state.phase in {"FINALIZE", "STOPPED", "FAILED"}:
                return self._finish_delivery(workspace, problem, state, profiler, findings_checked=True)

            target_contract = load_active_target(workspace.root)
            target_id = target_contract["contract_id"]
            if target_id != state.target_contract_id:
                raise DirectorLedEngineError(
                    "committed Director-led target is invalid or changed"
                )
            engine = self._build_director_led_engine(workspace, target_contract_id=target_id)
            run_result = engine.run(
                control_context=json.loads(
                    workspace.path(
                        work_ref(workspace.root, "state/research_execution_policy.json")
                    ).read_text(encoding="utf-8")
                )["control_context"],
                resume=True,
            )
        except DeliveryPaused:
            return self._paused_delivery_result(workspace, problem, profiler)
        except DirectorLedEngineError as exc:
            return self._finalize_director_led_engine_failure(workspace, problem, profiler, target_contract_id=state.target_contract_id, error=exc)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return self._finalize_director_led_engine_failure(workspace, problem, profiler, target_contract_id=state.target_contract_id, error=DirectorLedEngineError(str(exc)))
        except Exception as exc:  # noqa: BLE001 - committed Host inputs
            return self._finalize_director_led_engine_failure(workspace, problem, profiler, target_contract_id=state.target_contract_id, error=DirectorLedEngineError(str(exc)))
        return self._finish_delivery(workspace, problem, run_result.state, profiler)

    def _update_publication(self, ws, state, *, trigger, retry_errors=False):
        if state.phase == "FAILED":
            return
        complete = publish_stage_findings(llm=self._llm(), workdir=ws.root,
            completed_result_refs=state.completed_result_refs,
            accepted_candidate_ref=state.candidate_ref if state.terminal_result == "PASS" else "",
            retry_errors=retry_errors)
        if complete is False:
            pause_delivery(ws.root, trigger=trigger, round_index=state.round_index, stage="findings")
            raise DeliveryPaused("Finding verification is incomplete; resume the saved task")
        refresh_memory_index(ws.root)
        clear_delivery_pause(ws.root)

    def _paused_delivery_result(
        self,
        ws: RunWorkspace,
        problem: Problem,
        profiler: ProfileRecorder,
    ) -> PipelineResult:
        state = DirectorLedState.from_json(
            json.loads(ws.path(ws.work_ref("state/director_state.json")).read_text())
        )
        if state.phase not in {"FINALIZE", "STOPPED", "FAILED"}:
            raise DeliveryPaused(
                "Publication paused at the saved research checkpoint; use nl-proof-agent resume"
            )
        return finalize_director_led(
            workspace=ws,
            problem=problem,
            run_result=state,
            profiler=profiler,
            defer_delivery=True,
        )

    def _finish_delivery(self, ws, problem, state, profiler, *, findings_checked=False):
        try:
            if not findings_checked:
                self._update_publication(ws, state, trigger='final-findings')
        except DeliveryPaused:
            return self._paused_delivery_result(ws, problem, profiler)
        return finalize_director_led(workspace=ws, problem=problem, run_result=state, profiler=profiler)

    def _initialize_director_led_context(
        self,
        ws: RunWorkspace,
        *,
        target_contract: dict[str, Any],
        orientation_result: dict[str, Any],
        configured_cap: int | None = None,
    ) -> int:
        """Write the small canonical context set shared by all Director roles."""

        initialize_memory(
            ws,
            context_manifest=build_context_manifest(
                target_contract=target_contract,
                problem_ref=ws.work_ref("input/problem.md"),
                target_contract_ref=ws.work_ref("input/target_contract.json"),
                problem_brief_ref=orientation_result.get("problem_brief_ref", ""),
                orientation_ref=ws.work_ref(
                    "orientation/problem_orientation_result.json"
                ),
            ),
        )

        assessment_ref = orientation_result.get("assessment_ref", "")
        if assessment_ref:
            assessment = json.loads(ws.path(assessment_ref).read_text(encoding="utf-8"))
            proof_budget = _proof_budget_from_assessment(assessment["proof_assessment"])
        else:
            proof_budget = {
                "max_total_proof_states": MAX_PROOF_STATES,
                "cap_origin": "DEFAULT_PROOF_STATES",
            }
        # The caller's optional upper bound can reduce, never expand, the
        # analysis-derived shared proof-state budget.
        max_proof_states = int(proof_budget["max_total_proof_states"])
        if configured_cap is None and load_setup(ws.root) is None:
            configured_cap = self.config.max_proof_states
        if configured_cap is not None:
            max_proof_states = min(max_proof_states, configured_cap)
            proof_budget["configured_upper_bound"] = configured_cap
            if max_proof_states < proof_budget["max_total_proof_states"]:
                proof_budget["cap_origin"] = "CONFIGURED_UPPER_BOUND"
            proof_budget["max_total_proof_states"] = max_proof_states
        policy = json.loads(
            ws.path(
                work_ref(ws.root, "state/research_execution_policy.json")
            ).read_text(encoding="utf-8")
        )
        policy.update(
            max_proof_states=max_proof_states,
            budget_origin=proof_budget["cap_origin"],
            mode=policy.get("mode", (self.config.mode or "research")),
        )
        ws.write_json(work_ref(ws.root, "state/research_execution_policy.json"), policy)
        ws.write_json(work_ref(ws.root, "state/proof_loop_budget.json"), proof_budget)

        return max_proof_states

    def _build_director_led_engine(
        self,
        ws: RunWorkspace,
        *,
        target_contract_id: str,
    ) -> DirectorLedEngine:
        llm = self._llm()
        max_proof_states = json.loads(
            ws.path(work_ref(ws.root, "state/director_state.json")).read_text(
                encoding="utf-8"
            )
        )["max_proof_states"]
        policy = json.loads(ws.path(ws.work_ref("state/research_execution_policy.json")).read_text())

        def director(**kwargs: Any) -> Any:
            result = run_global_director(
                llm=self._director_llm(),
                workdir=ws.root,
                target_contract_id=target_contract_id,
                input_refs=kwargs.get("input_refs") or (),
                control_context=(
                    str(kwargs.get("control_context") or "")
                    + "\nRemaining shared proof states: "
                    + str(kwargs["proof_states_remaining"])
                    + "\nRemaining research rounds (including the next dispatch): "
                    + str(kwargs["research_rounds_remaining"])
                ),
                host_metadata={
                    "round_index": int(kwargs["round_index"]),
                    "agent_id": "director",
                    "task_id": f"round-{int(kwargs['round_index']):04d}-director",
                },
                call_name=(
                    f"global_research_director_"
                    f"{int(kwargs.get('round_index') or 1):04d}"
                ),
            )
            if isinstance(result, StructuredSemanticResult):
                return result.payload
            diagnostics = list(getattr(result, "diagnostics", ()) or ())
            raise DirectorLedEngineError(
                "Global Research Director semantic call failed "
                + f"(局部回执纠正已用 {result.host.get('host_metadata', {}).get('receipt_corrections', 0)}/1): "
                + json.dumps(diagnostics, ensure_ascii=False)
            )

        specialists = build_default_specialist_handlers(llm=llm, workdir=ws.root)
        specialists["survey"] = build_default_specialist_handlers(
            llm=self._survey_llm(), workdir=ws.root
        )["survey"]

        def publish_results(**kwargs: Any):
            state = kwargs.get("state")
            return self._update_publication(ws, state, trigger=str(kwargs.get('trigger_id') or 'round'))

        return DirectorLedEngine(
            workdir=ws.root,
            director=director,
            composer=build_default_composer(llm=llm),
            specialists=specialists,
            verifier=build_default_verifier(
                llm=llm,
                workdir=ws.root,
                target_contract_id=target_contract_id,
            ),
            target_contract_id=target_contract_id,
            max_proof_states=max_proof_states,
            max_rounds=policy.get("max_rounds"),
            on_round_committed=publish_results,
        )

    def _finalize_director_led_setup_failure(
        self,
        ws: RunWorkspace,
        problem: Problem,
        profiler: ProfileRecorder,
        *,
        reason_code: str,
        detail: dict[str, Any],
    ) -> PipelineResult:
        """Publish diagnostics without scheduling writers before a target exists."""
        state = load_setup(ws.root)
        if state is None:
            raise ValueError("initialization failure has no owned checkpoint")
        attempt = len(state["failure_refs"]) + 1
        ref = ws.work_ref(f"state/setup_attempts/failure-{attempt:04d}.json")
        ws.write_json(
            ref, {"reason_code": reason_code, "phase": state["phase"], "detail": detail}
        )
        state["failure_refs"].append(ref)
        state["last_failure_ref"] = ref
        save_setup(ws.root, state)
        note = ws.work_ref("orientation/setup_failure.md")
        ws.write_text_atomic(
            note,
            "# Initialization interrupted\n\n"
            f"Reason: `{reason_code}`. Saved phase: `{state['phase']}`.\n\n"
            "Correct the input environment or service, then use `nl-proof-agent resume --run-dir PATH`. "
            "Changing the literal problem requires a new turn or problem ID.\n\n"
            f"Diagnostics: [{Path(ref).name}](../../state/setup_attempts/{Path(ref).name}).\n",
        )
        report_ref = ws.work_ref("final_report.json")
        report = {
            "schema_version": "final_report/v8",
            "problem_id": ws.problem_id,
            "run_status": "completed_with_operational_error",
            "result": "FAILED",
            "reason_code": reason_code,
            "termination_owner": "INTERFACE",
            "operational_failure_owner": "INITIALIZATION",
            "operational_reason_code": reason_code,
            "operational_diagnostics": {
                "stage": "orientation", "message": str(detail.get("message") or reason_code),
                "diagnostic_ref": ref,
            },
            "mathematical_status": "NOT_STARTED",
            "outline_delivery": "NOT_REQUESTED",
            "terminal_kind": "SETUP_FAILURE",
            "target_contract_id": "",
            "target_contract_unavailable": True,
            "proof_binding": {},
            "artifact_refs": {
                "report": note,
                "setup_checkpoint": setup_ref(ws.root),
                "operational_diagnostic": ref,
            },
        }
        ws.write_json(report_ref, report)
        from pace.runtime.output.events import publish_progress

        publish_progress(ws.root, task_id=f'setup-failed-{attempt}', stage='orientation', status='failed')
        return PipelineResult(
            problem_id=ws.problem_id,
            run_dir=ws.root,
            run_status=report["run_status"],
            result="FAILED",
            proof_file=None,
            report_file=ws.path(note),
            final_report_file=ws.path(report_ref),
        )

    def _finalize_director_led_engine_failure(
        self,
        ws: RunWorkspace,
        problem: Problem,
        profiler: ProfileRecorder,
        *,
        target_contract_id: str,
        error: Exception,
    ) -> PipelineResult:
        diagnostic_ref = work_ref(
            ws.root, "state/diagnostics/director_led_engine_failure.json"
        )
        ws.write_json(
            diagnostic_ref,
            {
                "schema_version": "operational_diagnostic/v1",
                "stage": "DIRECTOR_LED_ENGINE",
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )
        try:
            state = DirectorLedState.from_json(
                json.loads(
                    ws.path(work_ref(ws.root, "state/director_state.json")).read_text(
                        encoding="utf-8"
                    )
                )
            )
        except (
            DirectorLedEngineError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            raise DirectorLedEngineError(
                "Director checkpoint is unavailable; preserve its diagnostics"
            ) from error
        if state.phase in {"DIRECTOR", "PENDING_SPECIALIST", "PENDING_BATCH", "VERIFY"}:
            diagnostic = json.loads(ws.path(diagnostic_ref).read_text(encoding="utf-8"))
            diagnostic["resume_checkpoint"] = state.to_json()
            ws.write_json(diagnostic_ref, diagnostic)
        # Reconstruct through the explicit fields to keep the state protocol
        # closed; ``schema_version`` is emitted only by ``to_json``.
        failed = DirectorLedState(
            target_contract_id=state.target_contract_id,
            # Round zero is reserved for the pristine DIRECTOR checkpoint.
            # Any engine exception is a terminal Host event, so promote an
            # untouched bootstrap state to the first terminal round rather
            # than constructing the invalid FAILED/round-0 combination.
            round_index=max(1, state.round_index),
            max_proof_states=state.max_proof_states,
            proof_states_used=state.proof_states_used,
            phase="FAILED",
            pending_dispatch_ref=state.pending_dispatch_ref,
            pending_prompt_ref=state.pending_prompt_ref,
            pending_assignment_ref=state.pending_assignment_ref,
            pending_verifier_request_ref=state.pending_verifier_request_ref,
            last_assignment_ref=state.last_assignment_ref,
            completed_result_refs=state.completed_result_refs,
            last_directive_ref=state.last_directive_ref,
            last_composer_manifest_ref=state.last_composer_manifest_ref,
            last_specialist_result_ref=state.last_specialist_result_ref,
            last_verification_ref=state.last_verification_ref,
            diagnostics_ref=diagnostic_ref,
            stop_reason="director_led_engine_failed",
            terminal_result="FAILED",
            candidate_ref=state.candidate_ref,
        )
        ws.write_json(work_ref(ws.root, "state/director_state.json"), failed.to_json())
        return finalize_director_led(
            workspace=ws,
            problem=problem,
            run_result=failed,
            profiler=profiler,
            reason_code="director_led_engine_failed",
        )
