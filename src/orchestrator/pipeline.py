from __future__ import annotations

import json
from functools import partial
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
    run_problem_orientation,
)
from pace.agents.research.global_director import run_global_director
from pace.contracts.problem.loader import Problem
from pace.orchestrator.director_led import (
    DirectorLedEngine,
    DirectorLedEngineError,
    DirectorLedState,
)
from pace.orchestrator.director_led_adapters import (
    build_default_composer,
    build_default_specialist_handlers,
)
from pace.orchestrator.director_workflow import finalize_engine_failure, initialize_research_turn, run_research_turn
from pace.orchestrator.director_verifier import build_default_verifier
from pace.orchestrator.finding_publication import publish_stage_findings
from pace.pipeline_types import (
    PipelineConfig,
    PipelineResult,
)
from pace.runtime.output.profiling import ProfileRecorder
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.prompts import (
    materialize_run_resources,
    prompt_root,
    validate_resource_snapshot,
)
from pace.runtime.output.setup import load_setup, save_setup, setup_ref
from pace.runtime.semantic import StructuredSemanticResult
from pace.runtime.llm import build_llm


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
    research_context: dict[str, Any] | None,
    mode: str | None,
) -> str:
    """Expose only scheduling metadata to the Director prompt."""

    rows = [
        f"mode={str(mode or '').strip()}",
    ]
    if isinstance(research_context, dict):
        refs = research_context.get("input_refs")
        if isinstance(refs, (list, tuple)):
            visible = [str(ref).strip() for ref in refs if str(ref).strip()]
            if visible:
                rows.append("inherited_input_refs=" + ",".join(visible))
    return "\n".join(rows)


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
                web_search=False,
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
                web_search=False,
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
            variant = self.config.resource_variant
            assert variant is not None  # PipelineConfig resolves the model default at construction.
            selected_state = ""
            if writing_action == "outline":
                outliner = self._outliner(ws)
                outliner.request(
                    problem.text,
                    requirements=additional_requirements,
                    resource_variant=variant,
                )
                selected_state = outline_state_ref(ws.root)
            elif writing_action == "report":
                writer = self._writer(ws)
                writer.request(
                    outline=outline_ref or RESEARCH_OUTLINE_REF,
                    message=problem.text,
                    requirements=additional_requirements,
                    resource_variant=variant,
                )
                selected_state = report_state_ref(ws.root)
            elif writing_action == "paper-outline":
                self._paper_writer(ws).request_outline(
                    problem.text,
                    requirements=additional_requirements,
                    resource_variant=variant,
                )
                selected_state = paper_outline_state_ref(ws.root)
            elif writing_action == "paper":
                self._paper_writer(ws).request(
                    outline=outline_ref or PAPER_OUTLINE_REF,
                    message=problem.text,
                    requirements=additional_requirements,
                    resource_variant=variant,
                )
                selected_state = paper_state_ref(ws.root)
            elif writing_action:
                raise ValueError(f"Unknown writing action: {writing_action}")
            if selected_state:
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
            setup = self._prepare_setup(
                ws, problem,
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
                    result = self._run_initialized(ws, problem, setup)
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
        from pace.runtime.agent_environment import prepare_agent_environments

        prepare_agent_environments(ws.root, mode=policy["mode"])
        if policy["mode"] is None:
            return
        ws.write_json(
            ws.work_ref("state/research_execution_policy.json"),
            {
                "schema_version": "research_execution_policy/v1",
                **({"research_memory_version": 1, "reassessment_after_attempts": 3}
                   if policy.get("research_memory_version") == 1 else {}),
                **({"research_delta_owner": "reporter"} if policy.get("research_delta_owner") == "reporter" else {}),
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
                    "research_memory_version": 1,
                    "research_delta_owner": "reporter",
                    "resource_variant": self.config.resource_variant,
                    "max_proof_states": self.config.max_proof_states,
                    "max_calls": self.config.max_calls,
                    "max_seconds": self.config.max_seconds,
                    "max_rounds": self.config.max_rounds,
                    "mode": self.config.mode,
                    "requested_mode": self.config.mode,
                    "control_context": _director_control_context(
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
        self, ws: RunWorkspace, problem: Problem, state: dict[str, Any],
    ) -> PipelineResult:
        """Finish shared initialization, then hand off to the saved engine."""
        profiler = ProfileRecorder(
            enabled=self.config.profile,
            workdir=ws.root,
            path=ws.path(ws.work_ref("events.jsonl")) if self.config.profile else None,
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
                    initialize_research_turn(ws, state["orientation"], state["policy"]["max_proof_states"])
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
        return run_research_turn(ws, problem, profiler, initial, self._build_director_led_engine)

    def resume(self, workspace: RunWorkspace, checkpoint: dict[str, Any],
               continuation_action: str, *, finalize: bool = False) -> PipelineResult | Path:
        from pace.runtime.input_protection import recover_open_input_snapshots
        from pace.runtime.sessions import run_session

        with workspace.execution_lock():
            if continuation_action == "WRITING":
                recover_open_input_snapshots(workspace.root)
                with run_session(workspace.root, profile=self.config.profile, config=self.config):
                    return self._resume(workspace, checkpoint, continuation_action, finalize=finalize)
            workspace.finish_turn("running")
            try:
                recover_open_input_snapshots(workspace.root)
                with run_session(
                    workspace.root, profile=self.config.profile, config=self.config
                ):
                    result = self._resume(
                        workspace, checkpoint, continuation_action, finalize=finalize
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
        finalize: bool = False,
    ) -> PipelineResult | Path:
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
                saved,
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
            finalize=finalize,
        )

    def _resume_director_led(
        self,
        workspace: RunWorkspace,
        checkpoint: dict[str, Any],
        continuation_action: str,
        *,
        committed_state: dict[str, Any],
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
        problem_path = workspace.path(workspace.work_ref("input/problem.md"))
        problem = Problem(problem_id=workspace.problem_id, text="", source=str(problem_path))
        try:
            problem = Problem(problem_id=workspace.problem_id, text=problem_path.read_text(encoding="utf-8"),
                              source=str(problem_path))
        except (OSError, UnicodeError) as exc:
            return finalize_engine_failure(workspace, problem, profiler, error=exc)
        return run_research_turn(workspace, problem, profiler, state, self._build_director_led_engine,
                                 finalize=finalize)

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
                decision_inputs=kwargs["decision_inputs"],
                on_accept=kwargs["on_accept"],
                control_context=str(kwargs.get("control_context") or ""),
                proof_states_remaining=kwargs["proof_states_remaining"],
                research_rounds_remaining=kwargs["research_rounds_remaining"],
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

        from pace.reporting.research_progress import run_research_reporter

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
            review_findings=partial(publish_stage_findings, llm=llm),
            reporter=lambda result_ref: run_research_reporter(
                llm=llm, workdir=ws.root, result_ref=result_ref),
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
