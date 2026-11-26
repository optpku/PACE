"""Compose a research outline and write its complete report from existing evidence."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pace.agents import report_writer
from pace.agents.prompt_composer import PromptAssembly, compose_prompt
from pace.artifacts.events import publish_progress
from pace.artifacts.results import relocate_links
from pace.artifacts.workspace import (
    _write_json_atomic,
    _write_text_atomic,
    stage_ref,
    work_ref,
)
from pace.orchestrator.writing.tasks import (
    run_writing_author,
    project_accepted_text,
    read_state,
    read_selected_outline,
)
from pace.runtime.agent_environment import prepare_role_environment
from pace.runtime.prompts import materialize_run_resources

logger = logging.getLogger(__name__)
RESEARCH_OUTLINE_REF = "results/outline.md"
RESEARCH_REPORT_REF = "results/research_report.md"


def outline_state_ref(workdir: Path) -> str:
    return work_ref(workdir, "state/research_outline.json")


def report_state_ref(workdir: Path) -> str:
    return work_ref(workdir, "state/research_report.json")


def outline_artifacts(workdir: Path, *, snapshots: bool = False) -> dict[str, str]:
    source = read_state(workdir, outline_state_ref(workdir)).get("outline_ref")
    if not source:
        return {}
    ref = source if snapshots else RESEARCH_OUTLINE_REF
    return {"research_outline": ref} if (Path(workdir) / ref).is_file() else {}


def report_request(workdir: Path) -> dict[str, Any]:
    return read_state(workdir, report_state_ref(workdir))


def report_artifacts(workdir: Path, *, snapshots: bool = False) -> dict[str, str]:
    state = report_request(workdir)
    if not state.get("report_ref"):
        return {}
    ref = state["report_ref"] if snapshots else RESEARCH_REPORT_REF
    if not (Path(workdir) / ref).is_file():
        return {}
    return {
        "research_report": ref,
        **({"report_outline": state["outline_ref"]} if snapshots else {}),
    }


class ResearchReportOutliner:
    """Keep only request, file delivery and Composer provenance in the Host."""

    def __init__(self, *, llm: Any, workdir: Path) -> None:
        self.llm = llm
        self.workdir = Path(workdir)

    def _attempt(self) -> str:
        base = f"{stage_ref(workdir=self.workdir, final=True)}/reports/outline"
        index = len(tuple((self.workdir / base).glob("attempt-*"))) + 1
        ref = f"{base}/attempt-{index:04d}"
        (self.workdir / ref).mkdir(parents=True, exist_ok=False)
        return ref

    def request(
        self, text: str, *, requirements: str = "", resource_variant: str | None = None
    ) -> None:
        if not text.strip():
            raise ValueError("Outline writing request must not be empty")
        attempt = self._attempt()
        request_ref = f"{attempt}/writing_request.md"
        _write_text_atomic(
            self.workdir / request_ref,
            "# 本次用户输入\n\n" + text + "\n\n# 附加要求\n\n" + requirements + "\n",
        )
        resources = work_ref(self.workdir, "resources")
        if resource_variant is not None:
            resources = f"{attempt}/resources"
            materialize_run_resources(
                self.workdir,
                resource_variant=resource_variant,
                destination=self.workdir / resources,
            )
        _write_json_atomic(
            self.workdir / outline_state_ref(self.workdir),
            {"request_ref": request_ref, "resources_ref": resources, "outline_ref": ""},
        )

    def update(
        self, *, round_index: int = 0, completed_result_refs: tuple[str, ...] = ()
    ) -> bool:
        if project_accepted_text(
            self.workdir,
            outline_state_ref(self.workdir),
            "outline_ref",
            RESEARCH_OUTLINE_REF,
        ):
            return True
        if not read_state(self.workdir, outline_state_ref(self.workdir)):
            self.request(
                "根据原始问题、用户历次输入及已有研究，为完整研究报告生成提纲。"
            )
        state = read_state(self.workdir, outline_state_ref(self.workdir))
        attempt = self._attempt()
        output_ref = f"{attempt}/outline.md"
        task_id = f"reporter-artifacts-outline-{Path(attempt).name}"
        resources = state["resources_ref"]
        prepare_role_environment(
            self.workdir,
            "RESEARCH_REPORT_OUTLINE",
            instructions=report_writer.build_instructions(
                workdir=self.workdir,
                resources=self.workdir / resources / "prompts",
                outline=True,
            ),
        )
        guides = (
            f"{resources}/prompts/report_writer/outline.md",
            f"{resources}/skills/research-report-composition/SKILL.md",
        )
        required = (state["request_ref"], *guides)
        candidates = (
            *required,
            "work/state/current_turn.json",
            "memory/README.md",
            "results/findings/README.md",
            "results/findings/archive.md",
            work_ref(self.workdir, "input/problem.md"),
            work_ref(self.workdir, "input/request.md"),
            work_ref(self.workdir, "input/target_contract.json"),
            work_ref(self.workdir, "final_report.json"),
            *completed_result_refs,
        )
        refs = tuple(
            dict.fromkeys((ref for ref in candidates if (self.workdir / ref).is_file()))
        )
        task = report_writer.build_prompt(
            workdir=self.workdir,
            resources=resources,
            state=state,
            guides=guides,
            output_ref=output_ref,
            outline=True,
        )
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="started",
            final=True,
            round_index=round_index,
        )
        try:
            composed = compose_prompt(
                resource_root=self.workdir / resources / "prompts",
                workdir=self.workdir,
                assembly=PromptAssembly(
                    stage="HOST_RESEARCH_REPORT_OUTLINE",
                    role="RESEARCH_REPORT_OUTLINE",
                    mode="",
                    target_contract_id="",
                    candidate_refs=refs,
                    required_refs=required,
                    prompt_seed=task,
                    output_artifact_ref=output_ref,
                ),
                input_refs=refs,
            )
            _write_text_atomic(
                self.workdir / attempt / "final_prompt.md", composed.final_prompt
            )
            state_ref = outline_state_ref(self.workdir)
            run_writing_author(
                report_writer.run,
                self.llm,
                composed.final_prompt,
                workdir=self.workdir,
                input_refs=refs,
                state=state,
                state_ref=state_ref,
                output_ref=output_ref,
                diagnostic_ref=f"{attempt}/failure.json",
                metadata={
                    "agent_id": "research_report_outline",
                    "output_refs": [output_ref],
                    "inline_task": True,
                    "task_id": task_id,
                    "protected_input_refs": refs,
                    "round_index": round_index,
                    "stage_ref": stage_ref(workdir=self.workdir, final=True),
                    "request_ref": state["request_ref"],
                },
                outline=True,
            )
            project_accepted_text(
                self.workdir, state_ref, "outline_ref", RESEARCH_OUTLINE_REF
            )
        except Exception as exc:
            logger.warning("Research outline generation failed", exc_info=True)
            diagnostic = f"{attempt}/failure.json"
            _write_json_atomic(
                self.workdir / diagnostic,
                {"reason": str(exc), "request_ref": state["request_ref"]},
            )
            publish_progress(
                self.workdir,
                task_id=task_id,
                stage="reporter",
                status="failed",
                final=True,
                round_index=round_index,
            )
            return False
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="completed",
            final=True,
            round_index=round_index,
            publication_refs={"research_outline": output_ref},
        )
        return True


class ResearchReportWriter:
    """Write one complete report using a frozen outline and the full user request."""

    def __init__(self, *, llm: Any, workdir: Path) -> None:
        self.llm = llm
        self.workdir = Path(workdir)

    def _attempt(self) -> str:
        base = f"{stage_ref(workdir=self.workdir, final=True)}/reports/report"
        index = len(tuple((self.workdir / base).glob("attempt-*"))) + 1
        ref = f"{base}/attempt-{index:04d}"
        (self.workdir / ref).mkdir(parents=True, exist_ok=False)
        return ref

    def request(
        self,
        *,
        outline: str = RESEARCH_OUTLINE_REF,
        message: str = "",
        requirements: str = "",
        resource_variant: str,
    ) -> None:
        source, body, original = read_selected_outline(
            self.workdir,
            outline,
            RESEARCH_OUTLINE_REF,
            outline_state_ref(self.workdir),
            message,
        )
        attempt = self._attempt()
        frozen_ref = f"{attempt}/outline.md"
        _write_text_atomic(
            self.workdir / frozen_ref,
            relocate_links(body, source, self.workdir / frozen_ref),
        )
        request_ref = f"{attempt}/writing_request.md"
        _write_text_atomic(
            self.workdir / request_ref,
            ("# 提纲阶段的原始写作要求\n\n" + original + "\n\n" if original else "")
            + "# 本次报告要求\n\n"
            + (message or "依据选定提纲完成完整研究报告。")
            + "\n\n# 本次附加要求\n\n"
            + requirements
            + "\n",
        )
        resources = f"{attempt}/resources"
        materialize_run_resources(
            self.workdir,
            resource_variant=resource_variant,
            destination=self.workdir / resources,
        )
        _write_json_atomic(
            self.workdir / report_state_ref(self.workdir),
            {
                "request_ref": request_ref,
                "outline_ref": frozen_ref,
                "resources_ref": resources,
                "report_ref": "",
            },
        )

    def update(self, *, round_index: int = 0) -> bool:
        if project_accepted_text(
            self.workdir,
            report_state_ref(self.workdir),
            "report_ref",
            RESEARCH_REPORT_REF,
        ):
            return True
        state = report_request(self.workdir)
        attempt = self._attempt()
        output_ref = f"{attempt}/research_report.md"
        task_id = f"reporter-artifacts-report-{Path(attempt).name}"
        resources = state["resources_ref"]
        prepare_role_environment(
            self.workdir,
            "RESEARCH_REPORT_WRITER",
            instructions=report_writer.build_instructions(
                workdir=self.workdir,
                resources=self.workdir / resources / "prompts",
                outline=False,
            ),
        )
        guides = (
            f"{resources}/prompts/report_writer/writer.md",
            f"{resources}/skills/research-report-composition/SKILL.md",
        )
        required = (state["request_ref"], state["outline_ref"], *guides)
        candidates = (
            *required,
            "memory/README.md",
            "results/findings/README.md",
            "results/findings/archive.md",
            work_ref(self.workdir, "input/problem.md"),
            work_ref(self.workdir, "input/request.md"),
            work_ref(self.workdir, "input/target_contract.json"),
            work_ref(self.workdir, "final_report.json"),
        )
        refs = tuple(
            dict.fromkeys((ref for ref in candidates if (self.workdir / ref).is_file()))
        )
        task = report_writer.build_prompt(
            workdir=self.workdir,
            resources=resources,
            state=state,
            guides=guides,
            output_ref=output_ref,
            outline=False,
        )
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="started",
            final=True,
            round_index=round_index,
        )
        try:
            composed = compose_prompt(
                resource_root=self.workdir / resources / "prompts",
                workdir=self.workdir,
                assembly=PromptAssembly(
                    stage="HOST_RESEARCH_REPORT_WRITING",
                    role="RESEARCH_REPORT_WRITER",
                    mode="",
                    target_contract_id="",
                    candidate_refs=refs,
                    required_refs=required,
                    prompt_seed=task,
                    output_artifact_ref=output_ref,
                ),
                input_refs=refs,
            )
            _write_text_atomic(
                self.workdir / attempt / "final_prompt.md", composed.final_prompt
            )
            state_ref = report_state_ref(self.workdir)
            run_writing_author(
                report_writer.run,
                self.llm,
                composed.final_prompt,
                workdir=self.workdir,
                input_refs=refs,
                state=state,
                state_ref=state_ref,
                output_ref=output_ref,
                diagnostic_ref=f"{attempt}/failure.json",
                metadata={
                    "agent_id": "research_report_writer",
                    "output_refs": [output_ref],
                    "inline_task": True,
                    "task_id": task_id,
                    "protected_input_refs": refs,
                    "round_index": round_index,
                    "stage_ref": stage_ref(workdir=self.workdir, final=True),
                    "request_ref": state["request_ref"],
                    "outline_ref": state["outline_ref"],
                },
                outline=False,
            )
            project_accepted_text(
                self.workdir, state_ref, "report_ref", RESEARCH_REPORT_REF
            )
        except Exception as exc:
            logger.warning("Research report generation failed", exc_info=True)
            diagnostic = f"{attempt}/failure.json"
            _write_json_atomic(
                self.workdir / diagnostic,
                {
                    "reason": str(exc),
                    "request_ref": state["request_ref"],
                    "outline_ref": state["outline_ref"],
                },
            )
            publish_progress(
                self.workdir,
                task_id=task_id,
                stage="reporter",
                status="failed",
                final=True,
                round_index=round_index,
            )
            return False
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="completed",
            final=True,
            round_index=round_index,
            publication_refs={
                "research_report": output_ref,
                "report_outline": state["outline_ref"],
            },
        )
        return True
