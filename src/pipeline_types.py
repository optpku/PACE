from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pace.model_defaults import default_resource_variant

MAX_PROOF_STATES = 16
DEFAULT_PROOF_LOOPS = 5
DEFAULT_CALL_TIMEOUT_SECONDS = 7200


@dataclass(frozen=True)
class PipelineConfig:
    output_root: Path
    mode: str | None = None
    max_calls: int | None = None
    max_seconds: int | None = None
    profile: bool = False
    provider: str = "codex"
    model: str = "gpt-5.6-sol"
    resource_variant: str | None = None
    max_proof_states: int | None = None
    reasoning_effort: str = "xhigh"
    web_search_mode: str = "on"
    # Search calls use the primary model unless an explicit override is
    # supplied.  This keeps custom-provider runs from silently switching to a
    # model that the provider does not expose.
    web_search_model: str = ""
    web_search_reasoning_effort: str = ""
    timeout_seconds: int | None = DEFAULT_CALL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.mode not in {None, "proof", "research"}:
            raise ValueError("mode must be proof or research")
        for name in ("max_calls", "max_seconds"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.web_search_mode not in {"off", "on"}:
            raise ValueError(f"unsupported web search mode: {self.web_search_mode}")
        variant = self.resource_variant
        if variant is None:
            variant = default_resource_variant(self.model, self.reasoning_effort)
        if variant not in {"standard", "weak"}:
            raise ValueError(f"unsupported resource variant: {variant}")
        object.__setattr__(self, "resource_variant", variant)
        if self.max_proof_states is not None and (
            type(self.max_proof_states) is not int or self.max_proof_states < 1
        ):
            raise ValueError("max_proof_states must be a positive integer")
        object.__setattr__(
            self,
            "web_search_model",
            str(self.web_search_model or "").strip() or str(self.model or "").strip(),
        )
        object.__setattr__(
            self,
            "web_search_reasoning_effort",
            str(self.web_search_reasoning_effort or "").strip()
            or str(self.reasoning_effort or "").strip(),
        )


@dataclass(frozen=True)
class PipelineResult:
    problem_id: str
    run_dir: Path
    run_status: str
    result: str
    proof_file: Path | None
    report_file: Path
    final_report_file: Path
    profile_file: Path | None = None
    outline_file: Path | None = None
    paper_outline_file: Path | None = None
    research_report_file: Path | None = None
    paper_source_file: Path | None = None
    paper_pdf_file: Path | None = None
