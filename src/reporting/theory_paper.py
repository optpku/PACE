"""Outline and write a theory paper from existing mathematical evidence."""

from __future__ import annotations
import logging
from pace.reporting.editorial import review_draft, request_manuscript_revision
import shutil
from pathlib import Path
from typing import Any
from pace.reporting.writing import read_state
from pace.runtime.output.events import publish_progress
from pace.runtime.output.paper import build_paper, build_failure_kind
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


def paper_outline_state_ref(root: Path, scope: str = "") -> str:
    return work_ref(root, f"state/{scope + '/' if scope else ''}paper_outline.json")


def paper_state_ref(root: Path, scope: str = "") -> str:
    return work_ref(root, f"state/{scope + '/' if scope else ''}theory_paper.json")


def paper_outline_artifacts(root: Path, *, scope: str = "") -> dict[str, str]:
    state = read_state(root, paper_outline_state_ref(root, scope))
    if not state.get("outline_ref"):
        return {}
    ref = state["outline_ref"]
    return {"paper_outline": ref} if (Path(root) / ref).is_file() else {}


def paper_artifacts(root: Path, *, snapshots: bool = False, scope: str = "") -> dict[str, str]:
    state = read_state(root, paper_state_ref(root, scope))
    if not state.get("paper_ref"):
        return {}
    ref = state["paper_ref"]
    source = Path(root) / ref
    if not source.is_file():
        return {}
    result = {"paper_source": ref}
    if source.with_name("references.bib").is_file():
        result["paper_bibliography"] = str(Path(ref).with_name("references.bib"))
    if paper_build_status(root, scope) == "COMPLETE" and source.with_suffix(".pdf").is_file():
        result["paper_pdf"] = str(Path(ref).with_suffix(".pdf"))
    if snapshots:
        result["paper_used_outline"] = state["outline_ref"]
    return result


def paper_build_status(root: Path, scope: str = "") -> str:
    state = read_state(root, paper_state_ref(root, scope))
    return read_state(root, state["build_ref"])["status"] if state.get("build_ref") else ""


class TheoryPaperWriter:
    """The Host persists instructions and files; the author chooses the content."""

    def __init__(self, *, llm: Any, workdir: Path, scope: str = "") -> None:
        self.llm, self.workdir = llm, Path(workdir)
        self.scope = scope

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
            self.workdir / paper_outline_state_ref(self.workdir, self.scope),
            {"request_ref": request_ref, "resources_ref": resources, "outline_ref": ""},
        )
        (self.workdir / paper_state_ref(self.workdir, self.scope)).unlink(missing_ok=True)

    def request(
        self,
        *,
        outline: str = PAPER_OUTLINE_REF,
        message: str = "",
        requirements: str = "",
        resource_variant: str,
    ) -> None:
        parent = read_state(self.workdir, paper_outline_state_ref(self.workdir, self.scope))
        if outline == PAPER_OUTLINE_REF and parent and (not parent["outline_ref"]):
            raise ValueError("The current paper outline request has not delivered an outline")
        if not parent and self.scope:
            parent = read_state(self.workdir, paper_outline_state_ref(self.workdir))
        if outline == PAPER_OUTLINE_REF and parent:
            if not parent.get("outline_ref"):
                raise ValueError("The selected paper outline is not complete")
            outline = parent["outline_ref"]
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
            self.workdir / paper_state_ref(self.workdir, self.scope),
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
        state_ref = (paper_outline_state_ref(self.workdir, self.scope) if outline
                     else paper_state_ref(self.workdir, self.scope))
        state = read_state(self.workdir, state_ref)
        if outline and paper_outline_artifacts(self.workdir, scope=self.scope):
            return True
        if not outline and state.get("paper_ref"):
            return self._complete_paper(state)
        if state.get("draft_ref"):
            self._publish_draft(state, outline=outline)
            return outline or paper_build_status(self.workdir, self.scope) == "COMPLETE"
        attempt = state.get("pending_attempt") or self._attempt("outline" if outline else "paper")
        state["pending_attempt"] = attempt
        _write_json_atomic(self.workdir / state_ref, state)
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
        from pace.reporting.research_report import report_artifacts
        reports = report_artifacts(self.workdir, snapshots=True, scope=self.scope)
        if not reports and self.scope:
            reports = report_artifacts(self.workdir, snapshots=True)
        candidates = (
            *required,
            "memory/README.md",
            "results/findings/README.md",
            "results/findings/archive.md",
            *([reports["research_report"]] if reports else []),
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
            task += f"Selected outline snapshot (read-only): `{state['outline_ref']}`.\nWrite the whole paper and its complete supplied proofs in `{Path(output_ref).parent}`. Keep main.tex and all required assets together using relative paths. Include bibliography files when cited; an uncited paper needs no bibliography file. The Host compiles with XeLaTeX and BibTeX. For Chinese use ctex with the installed Fandol fonts. Keep compilation logs and PDF text exports outside the delivered source directory. Complete the initial manuscript in this author task. The Host will independently review it and schedule necessary local revisions until independent review and PDF delivery succeed.\nNeurIPS template: `{resources}/templates/neurips2026/neurips_2026.sty`; use this NeurIPS preprint style and copy it unchanged into the output directory. Do not generate a checklist or submission questionnaire.\nYou may compile and fix concrete formatting errors within this task. The Host builds the delivered source and records build status separately from source delivery.\n"
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
                    "output_ref": output_ref,
                    "output_dirs": [] if outline else [str(Path(output_ref).parent)],
                },
            )
            if handle.transport_status != "SUCCESS":
                raise ValueError(f"Paper transport failed: {handle.error_code}")
            source = self.workdir / output_ref
            if not source.read_text(encoding="utf-8").strip():
                raise ValueError("Paper output file is empty")
            state.update(draft_ref=output_ref, draft_inputs=list(refs), task_id=task_id)
            _write_json_atomic(self.workdir / state_ref, state)
            accepted_handle, handle = handle, None
            commit_artifact_call_success(accepted_handle, workdir=self.workdir, artifact_ref=output_ref)
            self._publish_draft(state, outline=outline)
            publications = (paper_outline_artifacts(self.workdir, scope=self.scope) if outline
                            else paper_artifacts(self.workdir, snapshots=True, scope=self.scope))

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
            status="completed" if outline or paper_build_status(self.workdir, self.scope) == "COMPLETE" else "failed",
            publication_refs=publications,
        )
        return outline or paper_build_status(self.workdir, self.scope) == "COMPLETE"

    def _publish_draft(self, state, *, outline):
        state_ref = paper_outline_state_ref(self.workdir, self.scope) if outline else paper_state_ref(self.workdir, self.scope)
        ref = review_draft(llm=self.llm, root=self.workdir, state=state,
                           state_ref=state_ref, source_ref=state["draft_ref"],
                           input_refs=tuple(state["draft_inputs"]), markdown=outline,
                           independent=not outline, continuous=True)
        if outline:
            source = self.workdir / ref
            destination = self.workdir / PAPER_OUTLINE_REF
            _write_text_atomic(destination, relocate_links(source.read_text(encoding="utf-8"), source, destination))
            state["outline_ref"] = ref
        else:
            # Commit accepted source before the optional PDF toolchain runs.
            state["paper_ref"] = ref
            _write_json_atomic(self.workdir / state_ref, state)
            self._complete_paper(state)
        _write_json_atomic(self.workdir / state_ref, state)

    def _complete_paper(self, state: dict[str, Any]) -> bool:
        """Resume accepted source until it builds, or a concrete blocker persists."""
        state_ref = paper_state_ref(self.workdir, self.scope)
        while True:
            editorial = state.get("editorial", {})
            if editorial and (not editorial.get("approved")
                              or editorial.get("current_ref") != state["paper_ref"]):
                ref = review_draft(llm=self.llm, root=self.workdir, state=state,
                                  state_ref=state_ref, source_ref=state["draft_ref"],
                                  input_refs=tuple(state["draft_inputs"]), markdown=False, continuous=True)
                # The existing build receipt belongs only to the previous source.
                state.update(paper_ref=ref, build_ref="")
                _write_json_atomic(self.workdir / state_ref, state)
            build = read_state(self.workdir, state["build_ref"]) if state.get("build_ref") else {}
            log_ref = str(Path(state["paper_ref"]).with_name("build.log"))
            diagnostics = ((self.workdir / log_ref).read_text(encoding="utf-8")
                           if (self.workdir / log_ref).is_file() else "")
            kind = build.get("failure_kind") or build_failure_kind(
                build.get("status", ""), diagnostics + "\n" + build.get("reason", ""))
            if build.get("status") == "FAILED" and kind == "SOURCE":
                signature = build.get("source_error_signature") or build.get("reason", "")
                history = state.setdefault("compiler_repairs", [])
                previous = history[-1] if history else {}
                repeated = previous.get("signature") == signature and previous.get("build_ref") != state["build_ref"]
                # Normal, effective repairs have no total-count cap. A repeated
                # unchanged diagnostic gets one focused correction, then stops
                # instead of generating cosmetic versions indefinitely.
                if repeated and previous.get("focused") and previous["build_ref"] != state["build_ref"]:
                    state["build_blocker"] = "Compiler diagnostic persisted after its focused correction: " + signature
                    _write_json_atomic(self.workdir / state_ref, state)
                    return False
                focused = repeated or bool(previous.get("focused") and previous.get("build_ref") == state["build_ref"])
                if previous.get("build_ref") != state["build_ref"]:
                    history.append({"signature": signature, "build_ref": state["build_ref"], "focused": focused})
                state.pop("build_blocker", None)
                request_manuscript_revision(root=self.workdir, state=state, state_ref=state_ref,
                    source_ref=state["paper_ref"], markdown=False,
                    format_only=True, local_patch=True,
                    input_refs=(log_ref,),
                    issues=f"Formatting-only repair of compiler diagnostics in `{log_ref}`. "
                           "Read that log and correct only the reported LaTeX/bibliography defects. "
                           "Preserve mathematical statements, proofs and citations. Do not install "
                           "tools or fetch sources. Keep the complete local source bundle. "
                           + ("The same diagnostic survived the previous repair. Correct its precise root cause; "
                              "do not make cosmetic changes or repeat the ineffective edit." if focused else ""))
                continue
            if build.get("status") == "COMPLETE":
                self._publish_build(state)
                return True
            self._build_delivery(state["paper_ref"], state)
            _write_json_atomic(self.workdir / state_ref, state)
            build = read_state(self.workdir, state["build_ref"])
            self._publish_build(state)
            if build.get("status") == "COMPLETE":
                return True
            if build.get("failure_kind") != "SOURCE":
                return False

    def _publish_build(self, state):
        build = read_state(self.workdir, state["build_ref"])
        identity = [state["build_ref"], build["status"]]
        if state.get("build_publication") == identity:
            return
        publish_progress(self.workdir, task_id=state.get("task_id", state["paper_ref"]) + "-build",
                         stage="reporter", status="completed" if build["status"] == "COMPLETE" else "failed",
                         publication_refs=paper_artifacts(self.workdir, snapshots=True, scope=self.scope))
        state["build_publication"] = identity
        _write_json_atomic(self.workdir / paper_state_ref(self.workdir, self.scope), state)

    def _build_delivery(self, source_ref: str, state: dict[str, Any]) -> None:
        directory = (self.workdir / source_ref).parent
        build_paper(directory)
        destination = (self.workdir / PAPER_SOURCE_REF).parent
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(directory, destination)
        state.update(paper_ref=source_ref, build_ref=f"{Path(source_ref).parent}/build.json")
