from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pace.pipeline_types import PipelineResult

FINAL_REPORT_SCHEMA_VERSION = "final_report/v8"
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "completed_with_operational_error"}
)
TERMINAL_RESULTS = frozenset({"PASS", "FAILED"})


class FinalReportEnvelopeError(ValueError):
    """The final report cannot be interpreted as a terminal v8 envelope."""


@dataclass(frozen=True)
class FinalReportEnvelope:
    payload: dict[str, Any]
    run_status: str
    result: str
    artifact_refs: dict[str, Any]


    @classmethod
    def validate(cls, report: dict[str, Any]) -> FinalReportEnvelope:
        if report.get("schema_version") != FINAL_REPORT_SCHEMA_VERSION:
            raise FinalReportEnvelopeError(
                "final_report.json is not final_report/v8"
            )
        run_status = str(report.get("run_status") or "")
        result = str(report.get("result") or "")
        if run_status not in TERMINAL_RUN_STATUSES:
            raise FinalReportEnvelopeError("final_report.json has no terminal run_status")
        if result not in TERMINAL_RESULTS:
            raise FinalReportEnvelopeError("final_report.json has no terminal result")
        refs = report.get("artifact_refs")
        if not isinstance(refs, dict):
            raise FinalReportEnvelopeError(
                "final_report.json has no artifact_refs"
            )
        return cls(report, run_status, result, refs)

    def project(
        self,
        *,
        run_root: Path,
        default_problem_id: str,
        resolve_artifact: Callable[[str], Path | None],
        profile_file: Path | None,
        final_report_file: Path,
    ) -> PipelineResult:
        proof_file = resolve_artifact("proof") or resolve_artifact("candidate")
        verification_file = resolve_artifact("verification")
        return PipelineResult(
            problem_id=str(self.payload.get("problem_id") or default_problem_id),
            run_dir=Path(run_root),
            run_status=self.run_status,
            result=self.result,
            proof_file=proof_file,
            report_file=(
                resolve_artifact("report") or verification_file or final_report_file
            ),
            final_report_file=final_report_file,
            profile_file=profile_file,
            outline_file=resolve_artifact("research_outline"),
            research_report_file=resolve_artifact("research_report"),
            paper_outline_file=resolve_artifact("paper_outline"),
            paper_source_file=resolve_artifact("paper_source"),
            paper_pdf_file=resolve_artifact("paper_pdf"),
        )
