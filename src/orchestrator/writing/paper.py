"""Outline and write a theory paper from existing mathematical evidence."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from pace.agents import paper_writer
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
    settle_writing_call,
)
from pace.runtime.agent_environment import prepare_role_environment
from pace.runtime.paper import build_paper
from pace.runtime.prompts import materialize_run_resources

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
    status = (
        read_state(root, state["build_ref"]).get("status")
        if state.get("build_ref")
        else None
    )
    retained_previous_pdf = status != "COMPLETE" and _built_pdf(
        (root / PAPER_SOURCE_REF).parent
    )
    ref = state["paper_ref"] if snapshots or retained_previous_pdf else PAPER_SOURCE_REF
    source = Path(root) / ref
    if not source.is_file():
        return {}
    result = {"paper_source": ref}
    if source.with_name("references.bib").is_file():
        result["paper_bibliography"] = str(Path(ref).with_name("references.bib"))
    if status == "COMPLETE" and source.with_suffix(".pdf").is_file():
        result["paper_pdf"] = str(Path(ref).with_suffix(".pdf"))
    if snapshots:
        result["paper_used_outline"] = state["outline_ref"]
    return result


def paper_build_status(root: Path) -> str:
    state = read_state(root, paper_state_ref(root))
    return (
        read_state(root, state["build_ref"]).get("status", "")
        if state.get("build_ref")
        else ""
    )


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

    def request_outline(
        self, text: str, *, requirements: str = "", resource_variant: str
    ) -> None:
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
            self.workdir,
            resource_variant=resource_variant,
            destination=self.workdir / resources,
        )
        _write_json_atomic(
            self.workdir / paper_outline_state_ref(self.workdir),
            {"request_ref": request_ref, "resources_ref": resources, "outline_ref": ""},
        )

    def request(
        self,
        *,
        outline: str = PAPER_OUTLINE_REF,
        message: str = "",
        requirements: str = "",
        resource_variant: str,
    ) -> None:
        source, body, original = read_selected_outline(
            self.workdir,
            outline,
            PAPER_OUTLINE_REF,
            paper_outline_state_ref(self.workdir),
            message,
        )
        attempt = self._attempt("paper")
        frozen_ref = f"{attempt}/paper_outline.md"
        _write_text_atomic(
            self.workdir / frozen_ref,
            relocate_links(body, source, self.workdir / frozen_ref),
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
            self.workdir,
            resource_variant=resource_variant,
            destination=self.workdir / resources,
        )
        _write_json_atomic(
            self.workdir / paper_state_ref(self.workdir),
            {
                "request_ref": request_ref,
                "outline_ref": frozen_ref,
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
        state_ref = (
            paper_outline_state_ref(self.workdir)
            if outline
            else paper_state_ref(self.workdir)
        )
        state = read_state(self.workdir, state_ref)
        if outline and project_accepted_text(
            self.workdir, state_ref, "outline_ref", PAPER_OUTLINE_REF
        ):
            return True
        if not outline and (
            source_ref := state.get("accepted_source_ref") or state.get("paper_ref")
        ):
            if state.get("accepted_source_ref") or "paper_pdf" not in paper_artifacts(
                self.workdir
            ):
                settle_writing_call(self.workdir, state)
                self._build_delivery(source_ref, state)
                state.pop("accepted_source_ref", None)
                _write_json_atomic(self.workdir / state_ref, state)
                publish_progress(
                    self.workdir,
                    task_id="reporter-artifacts-paper-build",
                    stage="reporter",
                    status="completed",
                    final=True,
                    publication_refs=paper_artifacts(self.workdir, snapshots=True),
                )
            return True
        attempt = self._attempt("outline" if outline else "paper")
        name = "theory_paper_outline" if outline else "theory_paper_writer"
        output_ref = (
            f"{attempt}/paper_outline.md" if outline else f"{attempt}/paper/main.tex"
        )
        (self.workdir / output_ref).parent.mkdir(parents=True, exist_ok=True)
        task_id = f"reporter-artifacts-{name}-{Path(attempt).name}"
        role, stage = (name.upper(), "HOST_" + name.upper())
        resources = state["resources_ref"]
        prepare_role_environment(
            self.workdir,
            role,
            instructions=paper_writer.build_instructions(
                workdir=self.workdir,
                resources=self.workdir / resources / "prompts",
                outline=outline,
            ),
        )
        guides = (
            f"{resources}/prompts/paper_writer/{'outline' if outline else 'writer'}.md",
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
        refs = tuple(
            dict.fromkeys((ref for ref in candidates if (self.workdir / ref).is_file()))
        )
        task = paper_writer.build_prompt(
            workdir=self.workdir,
            resources=resources,
            state=state,
            guides=guides,
            output_ref=output_ref,
            outline=outline,
        )

        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="started",
            final=True,
        )
        try:
            composed = compose_prompt(
                resource_root=self.workdir / resources / "prompts",
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
            _write_text_atomic(
                self.workdir / attempt / "final_prompt.md", composed.final_prompt
            )
            run_writing_author(
                paper_writer.run,
                self.llm,
                composed.final_prompt,
                workdir=self.workdir,
                input_refs=refs,
                state=state,
                state_ref=state_ref,
                output_ref=output_ref,
                diagnostic_ref=f"{attempt}/failure.json",
                metadata={
                    "agent_id": name,
                    "output_refs": [output_ref],
                    "output_dirs": [] if outline else [str(Path(output_ref).parent)],
                    "inline_task": True,
                    "task_id": task_id,
                    "protected_input_refs": refs,
                    "stage_ref": stage_ref(workdir=self.workdir, final=True),
                    "request_ref": state["request_ref"],
                },
                outline=outline,
            )
            if outline:
                project_accepted_text(
                    self.workdir, state_ref, "outline_ref", PAPER_OUTLINE_REF
                )
            else:
                settle_writing_call(self.workdir, state)
                self._build_delivery(output_ref, state)
                state.pop("accepted_source_ref")
                _write_json_atomic(self.workdir / state_ref, state)
            publications = (
                {"paper_outline": output_ref}
                if outline
                else paper_artifacts(self.workdir, snapshots=True)
            )
        except Exception as exc:
            logger.warning("Theory paper writing failed", exc_info=True)
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
            )
            return False
        publish_progress(
            self.workdir,
            task_id=task_id,
            stage="reporter",
            status="completed",
            final=True,
            publication_refs=publications,
        )
        return True

    def _build_delivery(self, source_ref: str, state: dict[str, Any]) -> None:
        directory = (self.workdir / source_ref).parent
        build_ref = f"{Path(source_ref).parent}/build.json"
        if state.get("build_ref") != build_ref:
            # The author can write throughout the source bundle. Only trust a
            # build receipt produced after the Host takes ownership of this draft.
            (self.workdir / build_ref).unlink(missing_ok=True)
            state["build_ref"] = build_ref
            _write_json_atomic(self.workdir / paper_state_ref(self.workdir), state)
        if not _built_pdf(directory):
            build_paper(directory)
        state["paper_ref"] = source_ref
        destination = (self.workdir / PAPER_SOURCE_REF).parent
        if _built_pdf(destination) and not _built_pdf(directory):
            return  # Keep the last successful reader bundle; the new source is in its attempt.
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(directory, destination)


def _built_pdf(directory: Path) -> bool:
    pdf = directory / "main.pdf"
    return (
        pdf.is_file()
        and pdf.stat().st_size > 0
        and read_state(directory, "build.json").get("status") == "COMPLETE"
    )
