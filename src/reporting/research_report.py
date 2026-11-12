"""Compose a research outline and write its complete report from existing evidence."""

from __future__ import annotations
import logging
from pace.runtime.math_format import require_math_format
from pathlib import Path
from typing import Any
from pace.reporting.writing import read_state
from pace.runtime.output.events import publish_progress
from pace.runtime.output.results import relocate_links
from pace.runtime.output.workspace import (
    _write_json_atomic,
    _write_text_atomic,
    stage_ref,
    work_ref,
)
from pace.runtime.prompt_composer import compose_prompt
from pace.runtime.prompt_policy import PromptAssembly
from pace.runtime.prompts import materialize_run_resources
from pace.runtime.semantic import (
    begin_artifact_call,
    commit_artifact_call_failure,
    commit_artifact_call_success,
)

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
        (self.workdir / report_state_ref(self.workdir)).unlink(missing_ok=True)

    def update(self, *, round_index: int = 0, completed_result_refs: tuple[str, ...] = ()) -> bool:
        if outline_artifacts(self.workdir):
            return True
        if not read_state(self.workdir, outline_state_ref(self.workdir)):
            self.request("根据原始问题、用户历次输入及已有研究，为完整研究报告生成提纲。")
        state = read_state(self.workdir, outline_state_ref(self.workdir))
        attempt = self._attempt()
        output_ref = f"{attempt}/outline.md"
        task_id = f"reporter-artifacts-outline-{Path(attempt).name}"
        resources = state["resources_ref"]
        guides = (
            f"{resources}/prompts/roles/research_report_outline.md",
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
        refs = tuple(dict.fromkeys((ref for ref in candidates if (self.workdir / ref).is_file())))
        task = f"Run directory: `{self.workdir.resolve()}`\nRead the complete current user input and additional requirements: `{state['request_ref']}`.\nApply the assigned role `{guides[0]}` and report composition Skill `{guides[1]}` once.\nBrowse the entire run, including historical turns, and follow source references as needed. The listed files are entry points, not a reading allowlist. Preserve the original request; do not replace it with a summary. Generate only the complete research report's outline. All mathematical evidence, targets, budgets and verdicts are read-only.\nRequired output file: `{output_ref}`\nUse Markdown links relative to this exact output file.\n"
        handle = None
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="started",
            round_index=round_index,
        )
        try:
            composed = compose_prompt(
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
            _write_text_atomic(self.workdir / attempt / "final_prompt.md", composed.final_prompt)
            handle = begin_artifact_call(
                self.llm,
                composed.final_prompt,
                workdir=self.workdir,
                call_name="research_report_outline",
                role="RESEARCH_REPORT_OUTLINE",
                stage="HOST_RESEARCH_REPORT_OUTLINE",
                input_refs=refs,
                metadata={
                    "agent_id": "research_report_outline",
                    "inline_task": True,
                    "task_id": task_id,
                    "protected_input_refs": refs,
                    "round_index": round_index,
                    "stage_ref": stage_ref(workdir=self.workdir, final=True),
                    "request_ref": state["request_ref"],
                    "composer_call_id": composed.call_id,
                },
            )
            if handle.transport_status != "SUCCESS":
                raise ValueError(f"Outline transport failed: {handle.error_code}")
            source = self.workdir / output_ref
            body = source.read_text(encoding="utf-8")
            require_math_format(body)
            if not body.strip():
                raise ValueError("Outline file is empty")
            destination = self.workdir / RESEARCH_OUTLINE_REF
            _write_text_atomic(destination, relocate_links(body, source, destination))
            state["outline_ref"] = output_ref
            _write_json_atomic(self.workdir / outline_state_ref(self.workdir), state)
            commit_artifact_call_success(handle, workdir=self.workdir, artifact_ref=output_ref)
        except Exception as exc:
            logger.warning("Research outline generation failed", exc_info=True)
            diagnostic = f"{attempt}/failure.json"
            _write_json_atomic(
                self.workdir / diagnostic, {"reason": str(exc), "request_ref": state["request_ref"]}
            )
            if handle is not None:
                commit_artifact_call_failure(
                    handle,
                    workdir=self.workdir,
                    failure_class="INTERFACE"
                    if handle.transport_status != "SUCCESS"
                    else "REPRESENTATION",
                    retry_class=handle.retry_class
                    if handle.transport_status != "SUCCESS"
                    else "MODEL_REPRESENTATION",
                    diagnostics_ref=diagnostic,
                )
            publish_progress(
                self.workdir,
                task_id=task_id,
                stage="reporter",
                status="failed",
                round_index=round_index,
            )
            return False
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="completed",
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
        parent = read_state(self.workdir, outline_state_ref(self.workdir))
        if outline == RESEARCH_OUTLINE_REF and parent and (not parent["outline_ref"]):
            raise ValueError("The current outline request has not delivered an outline")
        source = (self.workdir / outline).resolve()
        body = source.read_text(encoding="utf-8")
        if not body.strip():
            raise ValueError("Selected outline is empty")
        original = (
            (self.workdir / parent["request_ref"]).read_text(encoding="utf-8") if parent else ""
        )
        if not original and (not message.strip()):
            raise ValueError(
                "Report writing needs a user request; supply --message for a standalone outline"
            )
        attempt = self._attempt()
        frozen_ref = f"{attempt}/outline.md"
        _write_text_atomic(
            self.workdir / frozen_ref, relocate_links(body, source, self.workdir / frozen_ref)
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
            self.workdir, resource_variant=resource_variant, destination=self.workdir / resources
        )
        _write_json_atomic(
            self.workdir / report_state_ref(self.workdir),
            {
                "request_ref": request_ref,
                "outline_ref": frozen_ref,
                "outline_source": source.relative_to(self.workdir.resolve()).as_posix()
                if source.is_relative_to(self.workdir.resolve())
                else str(source),
                "resources_ref": resources,
                "report_ref": "",
            },
        )

    def update(self, *, round_index: int = 0) -> bool:
        if report_artifacts(self.workdir):
            return True
        state = report_request(self.workdir)
        attempt = self._attempt()
        output_ref = f"{attempt}/research_report.md"
        task_id = f"reporter-artifacts-report-{Path(attempt).name}"
        resources = state["resources_ref"]
        guides = (
            f"{resources}/prompts/roles/research_report_writer.md",
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
        refs = tuple(dict.fromkeys((ref for ref in candidates if (self.workdir / ref).is_file())))
        task = f"Run directory: `{self.workdir.resolve()}`\nRead the complete current user input and additional requirements: `{state['request_ref']}`.\nSelected outline snapshot (read-only): `{state['outline_ref']}`.\nApply the assigned role `{guides[0]}` and report composition Skill `{guides[1]}` once.\nFollow the outline's source links and read the original statements, proofs and evidence. The listed files are entry points, not a reading allowlist. Complete the whole report, including technical appendices, in this single author task. All source evidence, the selected outline, mathematical targets and verdicts are read-only.\nRequired output file: `{output_ref}`\nUse Markdown links relative to this exact output file.\n"
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="started",
            round_index=round_index,
        )
        handle = None
        try:
            composed = compose_prompt(
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
            _write_text_atomic(self.workdir / attempt / "final_prompt.md", composed.final_prompt)
            handle = begin_artifact_call(
                self.llm,
                composed.final_prompt,
                workdir=self.workdir,
                call_name="research_report_writer",
                role="RESEARCH_REPORT_WRITER",
                stage="HOST_RESEARCH_REPORT_WRITING",
                input_refs=refs,
                metadata={
                    "agent_id": "research_report_writer",
                    "inline_task": True,
                    "task_id": task_id,
                    "protected_input_refs": refs,
                    "round_index": round_index,
                    "stage_ref": stage_ref(workdir=self.workdir, final=True),
                    "request_ref": state["request_ref"],
                    "outline_ref": state["outline_ref"],
                    "composer_call_id": composed.call_id,
                },
            )
            if handle.transport_status != "SUCCESS":
                raise ValueError(f"Report transport failed: {handle.error_code}")
            source = self.workdir / output_ref
            body = source.read_text(encoding="utf-8")
            require_math_format(body)
            if not body.strip():
                raise ValueError("Research report file is empty")
            destination = self.workdir / RESEARCH_REPORT_REF
            _write_text_atomic(destination, relocate_links(body, source, destination))
            state["report_ref"] = output_ref
            _write_json_atomic(self.workdir / report_state_ref(self.workdir), state)
            commit_artifact_call_success(handle, workdir=self.workdir, artifact_ref=output_ref)
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
            if handle is not None:
                commit_artifact_call_failure(
                    handle,
                    workdir=self.workdir,
                    failure_class="INTERFACE"
                    if handle.transport_status != "SUCCESS"
                    else "REPRESENTATION",
                    retry_class=handle.retry_class
                    if handle.transport_status != "SUCCESS"
                    else "MODEL_REPRESENTATION",
                    diagnostics_ref=diagnostic,
                )
            publish_progress(
                self.workdir,
                task_id=task_id,
                stage="reporter",
                status="failed",
                round_index=round_index,
            )
            return False
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="completed",
            round_index=round_index,
            publication_refs={
                "research_report": output_ref,
                "report_outline": state["outline_ref"],
            },
        )
        return True
