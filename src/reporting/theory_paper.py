"""Outline and write a theory paper from existing mathematical evidence."""

from __future__ import annotations
import logging
from pace.runtime.math_format import require_math_format
import shutil
from pathlib import Path
from typing import Any
from pace.reporting.writing import read_state
from pace.runtime.output.events import publish_progress
from pace.runtime.output.paper import build_paper
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
PAPER_OUTLINE_REF = "results/paper_outline.md"
PAPER_SOURCE_REF = "results/paper/main.tex"


def paper_outline_state_ref(root: Path) -> str:
    return work_ref(root, "state/paper_outline.json")


def paper_state_ref(root: Path) -> str:
    return work_ref(root, "state/theory_paper.json")


def paper_outline_artifacts(root: Path, *, snapshots: bool = False) -> dict[str, str]:
    state = read_state(root, paper_outline_state_ref(root))
    if not state.get("outline_ref"):
        return {}
    ref = state["outline_ref"] if snapshots else PAPER_OUTLINE_REF
    return {"paper_outline": ref} if (Path(root) / ref).is_file() else {}


def paper_artifacts(root: Path, *, snapshots: bool = False) -> dict[str, str]:
    state = read_state(root, paper_state_ref(root))
    if not state.get("paper_ref"):
        return {}
    ref = state["paper_ref"] if snapshots else PAPER_SOURCE_REF
    source = Path(root) / ref
    if not source.is_file():
        return {}
    result = {"paper_source": ref}
    if source.with_name("references.bib").is_file():
        result["paper_bibliography"] = str(Path(ref).with_name("references.bib"))
    if paper_build_status(root) == "COMPLETE" and source.with_suffix(".pdf").is_file():
        result["paper_pdf"] = str(Path(ref).with_suffix(".pdf"))
    if snapshots:
        result["paper_used_outline"] = state["outline_ref"]
    return result


def paper_build_status(root: Path) -> str:
    state = read_state(root, paper_state_ref(root))
    return read_state(root, state["build_ref"])["status"] if state.get("build_ref") else ""


class TheoryPaperWriter:
    """The Host persists instructions and files; the author chooses the content."""

    def __init__(self, *, llm: Any, workdir: Path) -> None:
        self.llm, self.workdir = llm, Path(workdir)

    def _attempt(self, kind: str) -> str:
        base = f"{stage_ref(workdir=self.workdir, final=True)}/papers/{kind}"
        index = len(tuple((self.workdir / base).glob("attempt-*"))) + 1
        ref = f"{base}/attempt-{index:04d}"
        (self.workdir / ref).mkdir(parents=True, exist_ok=False)
        return ref

    def request_outline(self, text: str, *, requirements: str = "", resource_variant: str) -> None:
        if not text.strip():
            raise ValueError("Paper outline writing request must not be empty")
        attempt = self._attempt("outline")
        request_ref = f"{attempt}/writing_request.md"
        _write_text_atomic(
            self.workdir / request_ref,
            "# 本次用户输入\n\n" + text + "\n\n# 附加要求\n\n" + requirements + "\n",
        )
        resources = f"{attempt}/resources"
        materialize_run_resources(
            self.workdir, resource_variant=resource_variant, destination=self.workdir / resources
        )
        _write_json_atomic(
            self.workdir / paper_outline_state_ref(self.workdir),
            {"request_ref": request_ref, "resources_ref": resources, "outline_ref": ""},
        )
        (self.workdir / paper_state_ref(self.workdir)).unlink(missing_ok=True)

    def request(
        self,
        *,
        outline: str = PAPER_OUTLINE_REF,
        message: str = "",
        requirements: str = "",
        resource_variant: str,
    ) -> None:
        parent = read_state(self.workdir, paper_outline_state_ref(self.workdir))
        if outline == PAPER_OUTLINE_REF and parent and (not parent["outline_ref"]):
            raise ValueError("The current paper outline request has not delivered an outline")
        source = (self.workdir / outline).resolve()
        body = source.read_text(encoding="utf-8")
        if not body.strip():
            raise ValueError("Selected paper outline is empty")
        original = (
            (self.workdir / parent["request_ref"]).read_text(encoding="utf-8") if parent else ""
        )
        if not original and (not message.strip()):
            raise ValueError(
                "Paper writing needs a user request; supply --message for a standalone outline"
            )
        attempt = self._attempt("paper")
        frozen_ref = f"{attempt}/paper_outline.md"
        _write_text_atomic(
            self.workdir / frozen_ref, relocate_links(body, source, self.workdir / frozen_ref)
        )
        request_ref = f"{attempt}/writing_request.md"
        _write_text_atomic(
            self.workdir / request_ref,
            ("# 提纲阶段的原始写作要求\n\n" + original + "\n\n" if original else "")
            + "# 本次论文要求\n\n"
            + (message or "依据选定提纲完成理论论文。")
            + "\n\n# 本次附加要求\n\n"
            + requirements
            + "\n",
        )
        resources = f"{attempt}/resources"
        materialize_run_resources(
            self.workdir, resource_variant=resource_variant, destination=self.workdir / resources
        )
        _write_json_atomic(
            self.workdir / paper_state_ref(self.workdir),
            {
                "request_ref": request_ref,
                "outline_ref": frozen_ref,
                "outline_source": source.relative_to(self.workdir.resolve()).as_posix()
                if source.is_relative_to(self.workdir.resolve())
                else str(source),
                "resources_ref": resources,
                "paper_ref": "",
                "build_ref": "",
            },
        )

    def update_outline(self) -> bool:
        return self._update(outline=True)

    def update(self) -> bool:
        return self._update(outline=False)

    def _update(self, *, outline: bool) -> bool:
        delivered = (
            paper_outline_artifacts(self.workdir) if outline else paper_artifacts(self.workdir)
        )
        if delivered:
            if not outline and paper_build_status(self.workdir) != "COMPLETE":
                state_ref = paper_state_ref(self.workdir)
                state = read_state(self.workdir, state_ref)
                self._build_delivery(state["paper_ref"], state)
                _write_json_atomic(self.workdir / state_ref, state)
                publish_progress(
                    self.workdir,
                    task_id="reporter-artifacts-paper-build",
                    stage="reporter",
                    status="completed",
                    publication_refs=paper_artifacts(self.workdir, snapshots=True),
                )
            return True
        state_ref = (
            paper_outline_state_ref(self.workdir) if outline else paper_state_ref(self.workdir)
        )
        state = read_state(self.workdir, state_ref)
        attempt = self._attempt("outline" if outline else "paper")
        name = "theory_paper_outline" if outline else "theory_paper_writer"
        output_ref = f"{attempt}/paper_outline.md" if outline else f"{attempt}/paper/main.tex"
        (self.workdir / output_ref).parent.mkdir(parents=True, exist_ok=True)
        task_id = f"reporter-artifacts-{name}-{Path(attempt).name}"
        role, stage = (name.upper(), "HOST_" + name.upper())
        resources = state["resources_ref"]
        guides = (
            f"{resources}/prompts/roles/{name}.md",
            f"{resources}/skills/theory-paper-composition/SKILL.md",
        )
        required = (
            state["request_ref"],
            *guides,
            *((state["outline_ref"],) if not outline else ()),
        )
        candidates = (
            *required,
            "memory/README.md",
            "results/findings/README.md",
            "results/findings/archive.md",
            "results/research_report.md",
            work_ref(self.workdir, "input/problem.md"),
            work_ref(self.workdir, "input/request.md"),
            work_ref(self.workdir, "input/target_contract.json"),
            work_ref(self.workdir, "final_report.json"),
        )
        refs = tuple(dict.fromkeys((ref for ref in candidates if (self.workdir / ref).is_file())))
        task = f"Run directory: `{self.workdir.resolve()}`\nRead the complete user input and additional requirements: `{state['request_ref']}`.\nApply the assigned role `{guides[0]}` and paper composition Skill `{guides[1]}` once.\nBrowse the entire run and historical turns, following original proof and literature sources. The listed files are entry points, not a reading allowlist. A research report is optional context. All source evidence, mathematical targets, budgets and verdicts are read-only. Do not perform new research or experiments.\nRequired output file: `{output_ref}`\n"
        if outline:
            task += "Deliver only the paper outline. Use Markdown links relative to the exact output file.\n"
        else:
            task += f"Selected outline snapshot (read-only): `{state['outline_ref']}`.\nWrite the whole paper and its complete supplied proofs in `{Path(output_ref).parent}`. Keep main.tex and all required assets together using relative paths. Include bibliography files when cited; an uncited paper needs no bibliography file. The Host compiles with XeLaTeX and BibTeX. For Chinese use ctex with the installed Fandol fonts. Keep compilation logs and PDF text exports outside the delivered source directory. Complete all prose in this single author task; no independent editorial review or revision loop.\nNeurIPS template: `{resources}/templates/neurips2026/neurips_2026.sty`; use this NeurIPS preprint style and copy it unchanged into the output directory. Do not generate a checklist or submission questionnaire.\nYou may compile and fix concrete formatting errors within this task. The Host builds the delivered source and records build status separately from source delivery.\n"
        publish_progress(self.workdir, task_id=task_id, stage="reporter", status="started")
        handle = None
        try:
            composed = compose_prompt(
                workdir=self.workdir,
                assembly=PromptAssembly(
                    stage=stage,
                    role=role,
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
                call_name=name,
                role=role,
                stage=stage,
                input_refs=refs,
                metadata={
                    "agent_id": name,
                    "inline_task": True,
                    "task_id": task_id,
                    "protected_input_refs": refs,
                    "stage_ref": stage_ref(workdir=self.workdir, final=True),
                    "request_ref": state["request_ref"],
                    "composer_call_id": composed.call_id,
                },
            )
            if handle.transport_status != "SUCCESS":
                raise ValueError(f"Paper transport failed: {handle.error_code}")
            source = self.workdir / output_ref
            body = source.read_text(encoding="utf-8")
            require_math_format(body, markdown=outline)
            if not body.strip():
                raise ValueError("Paper output file is empty")
            if outline:
                destination = self.workdir / PAPER_OUTLINE_REF
                _write_text_atomic(destination, relocate_links(body, source, destination))
                state["outline_ref"] = output_ref
                publications = {"paper_outline": output_ref}
            else:
                self._build_delivery(output_ref, state)
            _write_json_atomic(self.workdir / state_ref, state)
            commit_artifact_call_success(handle, workdir=self.workdir, artifact_ref=output_ref)
            if not outline:
                publications = paper_artifacts(self.workdir, snapshots=True)
        except Exception as exc:
            logger.warning("Theory paper writing failed", exc_info=True)
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
            publish_progress(self.workdir, task_id=task_id, stage="reporter", status="failed")
            return False
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="completed",
            publication_refs=publications,
        )
        return True

    def _build_delivery(self, source_ref: str, state: dict[str, Any]) -> None:
        directory = (self.workdir / source_ref).parent
        build_paper(directory)
        destination = (self.workdir / PAPER_SOURCE_REF).parent
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(directory, destination)
        state.update(paper_ref=source_ref, build_ref=f"{Path(source_ref).parent}/build.json")
