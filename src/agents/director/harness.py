from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from pace.agents.director.agent import run_global_director
from pace.agents.director.dispatch import (
    build_default_composer,
    build_default_specialist_handlers,
)
from pace.agents.director.findings import check_stage_findings
from pace.agents.director.loop import DirectorLedEngine
from pace.agents.director.state import DirectorLedEngineError, DirectorLedState
from pace.agents.director.verification import build_default_verifier
from pace.agents.director.workflow import (
    finalize_engine_failure,
    initialize_research_turn,
    run_research_turn,
)
from pace.agents.orientation import (
    run_problem_orientation,
)
from pace.artifacts.setup import load_setup, save_setup, setup_ref
from pace.artifacts.workspace import RunWorkspace, work_ref
from pace.contracts.problem.loader import Problem
from pace.pipeline_types import (
    PipelineConfig,
    PipelineResult,
)
from pace.runtime.llm import build_llm
from pace.runtime.profiling import ProfileRecorder
from pace.runtime.prompts import (
    materialize_run_resources,
    prompt_root,
    validate_resource_snapshot,
)
from pace.runtime.semantic import StructuredSemanticResult


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


class DirectorHarness:
    """Own one turn from resource setup and Orientation to its terminal delivery."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        initialize_memory,
        commit_memory,
        commit_results,
        publish_terminal,
    ) -> None:
        self.config = config
        self.initialize_memory = initialize_memory
        self.commit_memory = commit_memory
        self.commit_results = commit_results
        self.publish_terminal = publish_terminal
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

    def _prepare_turn_handoff(self, ws: RunWorkspace, source_turn_id: str) -> None:
        """Bind the prior committed records for the next mathematical task."""
        context = json.loads(
            ws.path(ws.work_ref("input/continuation_context.json")).read_text()
        )
        refs = tuple(dict.fromkeys(context["input_refs"]))
        ws.write_text_atomic(
            ws.work_ref("input/handoff.md"),
            f"# Previous mathematical work: {source_turn_id}\n\n"
            + "These records are search context. The current literal target remains authoritative.\n\n"
            + "\n".join(f"- `{ref}`" for ref in refs)
            + "\n",
        )

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
        prepare_agent_environments(ws.root, mode=policy["mode"])
        if policy["mode"] is None:
            return
        ws.write_json(
            ws.work_ref("state/research_execution_policy.json"),
            {
                "schema_version": "research_execution_policy/v1",
                **(
                    {"research_memory_version": 1, "reassessment_after_attempts": 3}
                    if policy.get("research_memory_version") == 1
                    else {}
                ),
                **(
                    {"research_delta_owner": "reporter"}
                    if policy.get("research_delta_owner") == "reporter"
                    else {}
                ),
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

    def run(
        self,
        ws: RunWorkspace,
        problem: Problem,
        state: dict[str, Any],
    ) -> PipelineResult:
        """Finish shared initialization, then hand off to the saved engine."""
        profiler = ProfileRecorder(
            enabled=self.config.profile,
            workdir=ws.root,
            path=ws.path(ws.work_ref("events.jsonl")) if self.config.profile else None,
        )
        from pace.artifacts.events import publish_progress

        publish_progress(
            ws.root, task_id="orientation", stage="orientation", status="started"
        )
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
                state["policy"].update(
                    mode=mode, mode_source=source, mode_reason=reason
                )
                state["policy"]["control_context"] = "\n".join(
                    f"mode={mode}" if line.startswith("mode=") else line
                    for line in state["policy"]["control_context"].splitlines()
                )
                # The orientation receipt is committed before this derived policy.
                save_setup(ws.root, state)
                self._commit_execution_policies(ws, state["policy"])
                publish_progress(
                    ws.root,
                    task_id="mode-selection",
                    stage="orientation",
                    status="completed",
                    execution_mode=mode,
                    mode_source=source,
                    mode_reason=reason,
                )

            reason = "problem_orientation_failed"
            if state["phase"] == "ORIENTATION":
                with profiler.span("pipeline.problem_orientation"):
                    orientation = run_problem_orientation(
                        llm=self._llm(),
                        workdir=ws.root,
                        problem=problem,
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
                    from pace.agents.director.proof import initialize_proof_state
                    from pace.artifacts.context import build_context_manifest
                    from pace.contracts.proof.binding import load_active_target

                    self.initialize_memory(
                        ws,
                        context_manifest=build_context_manifest(
                            target_contract=load_active_target(ws.root),
                            problem_ref=ws.work_ref("input/problem.md"),
                            target_contract_ref=ws.work_ref(
                                "input/target_contract.json"
                            ),
                            problem_brief_ref=state["orientation"].get(
                                "problem_brief_ref", ""
                            ),
                            orientation_ref=ws.work_ref(
                                "orientation/problem_orientation_result.json"
                            ),
                        ),
                    )
                    from pace.pipeline_types import DEFAULT_PROOF_LOOPS

                    initialize_proof_state(
                        ws,
                        max_loops=state["policy"]["max_proof_states"]
                        or DEFAULT_PROOF_LOOPS,
                        resource_variant=state["policy"]["resource_variant"],
                    )
                else:
                    initialize_research_turn(
                        ws,
                        state["orientation"],
                        state["policy"]["max_proof_states"],
                        self.initialize_memory,
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
        publish_progress(
            ws.root,
            task_id="orientation",
            stage="orientation",
            status="completed",
            notice="orientation_ready",
        )
        if state["policy"]["mode"] == "proof":
            from pace.agents.director.proof import run_proof

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
        return run_research_turn(
            ws,
            problem,
            profiler,
            initial,
            self._build_director_led_engine,
            publish_terminal=self.publish_terminal,
        )

    def resume(
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
        if continuation_action == "PROOF":
            from pace.agents.director.proof import run_proof
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
        from pace.agents.director.recovery import (
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
            return self.run(
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
        problem = Problem(
            problem_id=workspace.problem_id, text="", source=str(problem_path)
        )
        try:
            problem = Problem(
                problem_id=workspace.problem_id,
                text=problem_path.read_text(encoding="utf-8"),
                source=str(problem_path),
            )
        except (OSError, UnicodeError) as exc:
            return finalize_engine_failure(
                workspace,
                problem,
                profiler,
                error=exc,
                publish_terminal=self.publish_terminal,
            )
        return run_research_turn(
            workspace,
            problem,
            profiler,
            state,
            self._build_director_led_engine,
            finalize=finalize,
            publish_terminal=self.publish_terminal,
        )

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
        policy = json.loads(
            ws.path(ws.work_ref("state/research_execution_policy.json")).read_text()
        )

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

        from pace.agents.reporter import run_research_reporter

        return DirectorLedEngine(
            workdir=ws.root,
            director=director,
            composer=build_default_composer(),
            specialists=specialists,
            verifier=build_default_verifier(
                llm=llm,
                workdir=ws.root,
                target_contract_id=target_contract_id,
            ),
            target_contract_id=target_contract_id,
            max_proof_states=max_proof_states,
            max_rounds=policy.get("max_rounds"),
            review_findings=partial(check_stage_findings, llm=llm),
            commit_memory=self.commit_memory,
            commit_results=self.commit_results,
            reporter=lambda result_ref: run_research_reporter(
                llm=llm, workdir=ws.root, result_ref=result_ref
            ),
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
            "Correct the input environment or service, then use `pace resume --run-dir PATH`. "
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
                "stage": "orientation",
                "message": str(detail.get("message") or reason_code),
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
        from pace.artifacts.events import publish_progress

        publish_progress(
            ws.root,
            task_id=f"setup-failed-{attempt}",
            stage="orientation",
            status="failed",
        )
        return PipelineResult(
            problem_id=ws.problem_id,
            run_dir=ws.root,
            run_status=report["run_status"],
            result="FAILED",
            proof_file=None,
            report_file=ws.path(note),
            final_report_file=ws.path(report_ref),
        )

    def prepare(
        self,
        ws: RunWorkspace,
        problem: Problem,
        *,
        request: str,
        research_context: dict[str, Any] | None = None,
        source_turn_id: str = "",
    ) -> dict[str, Any]:
        ws.write_text(ws.work_ref("input/request.md"), request)
        ws.write_text(
            ws.work_ref("input/problem.md"), _canonical_problem_text(problem.text)
        )
        return self._prepare_setup(
            ws,
            problem,
            research_context=research_context,
            source_turn_id=source_turn_id,
        )


def prepare_agent_environments(workdir: Path, *, mode: str | None) -> None:
    from pace.agents import (
        auditor,
        finding_verifier,
        framer,
        kernel,
        orientation,
        paper_writer,
        planner,
        prover,
        report_writer,
        reporter,
        research,
        survey,
        web_reporter,
    )
    from pace.agents.director import agent as director
    from pace.runtime.agent_environment import prepare_role_environment

    resources = prompt_root(workdir)
    ordinary = (
        ("DIRECTOR_LED_FRAMER", framer),
        ("PLANNER", planner),
        ("SURVEY", survey),
        ("RESEARCH", research),
        ("WHOLE_PROVER", prover),
        ("PROOF_CONTRACT_AUDITOR", auditor),
        ("MATH_VERIFIER_KERNEL", kernel),
        ("FINDING_VERIFIER", finding_verifier),
        ("RESEARCH_REPORTER", reporter),
        ("WEB_REPORTER", web_reporter),
    )
    for role, module in ordinary:
        prepare_role_environment(
            workdir,
            role,
            instructions=module.build_instructions(
                workdir=workdir, resources=resources, mode=mode
            ),
        )
    prepare_role_environment(
        workdir,
        "GLOBAL_RESEARCH_DIRECTOR",
        instructions=director.build_instructions(workdir=workdir, resources=resources),
    )
    for stage, role in (
        ("extraction", "PROBLEM_ORIENTATION_EXTRACTOR"),
        ("assessment", "PROBLEM_RESEARCH_DECISION"),
    ):
        prepare_role_environment(
            workdir,
            role,
            instructions=orientation.build_instructions(
                workdir=workdir, resources=resources, stage=stage
            ),
        )
    for module, prefix in (
        (report_writer, "RESEARCH_REPORT"),
        (paper_writer, "THEORY_PAPER"),
    ):
        for outline in (True, False):
            prepare_role_environment(
                workdir,
                prefix + ("_OUTLINE" if outline else "_WRITER"),
                instructions=module.build_instructions(
                    workdir=workdir, resources=resources, outline=outline
                ),
            )
