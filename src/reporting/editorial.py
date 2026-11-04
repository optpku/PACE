"""Resumable manuscript format repair and independent editorial review."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil

from pace.runtime.math_format import require_math_format
from pace.runtime.output.workspace import _write_json_atomic
from pace.runtime.semantic import (
    ArtifactCallHandle, SemanticCallSpec, StructuredSemanticResult,
    begin_artifact_call, commit_artifact_call_failure, commit_artifact_call_success,
    semantic_call,
)


def review_draft(*, llm, root: Path, state: dict, state_ref: str,
                 source_ref: str, input_refs: tuple[str, ...], markdown: bool,
                 independent: bool = True) -> str:
    """Keep accepted author/reviewer stages; revise only the recorded defect."""
    editorial = state.setdefault("editorial", {
        "current_ref": source_ref, "reviews": [], "revision": None,
        "approved": False,
    })

    def save():
        _write_json_atomic(root / state_ref, state)

    review_base = Path(source_ref).parent if markdown else Path(source_ref).parent.parent
    save()
    for _ in range(3):
        current = editorial["current_ref"]
        if editorial["approved"]:
            return current
        revision = editorial["revision"]
        if revision:
            output = root / revision["output_ref"]
            if not revision.get("handle"):
                output.parent.mkdir(parents=True, exist_ok=True)
                if not markdown:
                    # Preserve the complete manuscript bundle and relative asset paths.
                    shutil.copytree((root / current).parent, output.parent, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("revision-*", "editorial"))
                    output.unlink(missing_ok=True)
                prompt = (
                    f"Revise the saved manuscript `{current}` only for the recorded issues below. "
                    "Preserve all unaffected statements, assumptions, proofs and evidence links. "
                    "Do not restart research or retrieve external sources. "
                    "For formatting-only issues, do not change mathematical content. "
                    "For substantive editorial issues, correct the manuscript to match the existing "
                    "evidence; explicitly retain unresolved gaps instead of inventing proofs. "
                    f"Read the assigned evidence references: {list(input_refs)}.\n"
                    f"Issues: {revision['issues']}\n"
                    f"## Deliverable\nWrite `{revision['output_ref']}`\n"
                    "Write the complete corrected file, preserving relative links and assets. "
                    "Original inputs are read-only."
                )
                handle = begin_artifact_call(
                    llm, prompt, workdir=root, call_name="manuscript_revision",
                    role="MANUSCRIPT_REVISION", stage="HOST_MANUSCRIPT_REVISION",
                    input_refs=(current, *input_refs),
                    metadata={"inline_task": True, "protected_input_refs": (current, *input_refs),
                              "output_ref": revision["output_ref"],
                              "output_dirs": [str(output.parent.relative_to(root))]},
                )
                if handle.transport_status != "SUCCESS":
                    commit_artifact_call_failure(handle, workdir=root,
                        failure_class="INTERFACE", retry_class=handle.retry_class)
                    raise ValueError("Manuscript revision interrupted; resume the saved task")
                revision["handle"] = asdict(handle)
                save()
            body = output.read_text(encoding="utf-8")
            if not body.strip():
                revision.pop("handle", None)
                save()
                raise ValueError("Manuscript revision is empty")
            # Commit the new file even if another format repair is needed. Resume
            # never returns to the original full author call.
            handle = ArtifactCallHandle(**revision["handle"])
            editorial["current_ref"] = revision["output_ref"]
            editorial["revision"] = None
            editorial["last_review"] = None
            save()
            commit_artifact_call_success(handle, workdir=root, artifact_ref=editorial["current_ref"])
            current = editorial["current_ref"]
        try:
            require_math_format((root / current).read_text(encoding="utf-8"), markdown=markdown)
            issues = ""
        except ValueError as exc:
            issues = "Formatting-only repair: " + str(exc)
        if not issues and independent:
            review = editorial.get("last_review")
            if not review:
                index = len(editorial["reviews"]) + 1
                review_ref = str(review_base / "editorial" / f"review-{index:03d}.md")
                (root / review_ref).parent.mkdir(parents=True, exist_ok=True)

                def validate(payload):
                    path = root / review_ref
                    return [] if path.is_file() and path.read_text().strip() else [
                        {"message": "The assigned independent review file is missing", "stage": "acceptance"}]

                def accept(payload):
                    editorial["last_review"] = {**payload, "review_ref": review_ref, "source_ref": current}
                    editorial["reviews"].append(editorial["last_review"])
                    if payload["verdict"] == "APPROVE":
                        editorial["approved"] = True
                    save()

                result = semantic_call(
                    llm,
                    f"Independently review manuscript `{current}` against the saved evidence "
                    f"and author requirements in {list(input_refs)}. Do not reuse the author's "
                    "self-assessment. Check claim strength, hypotheses, complete proof scope, "
                    "citations, unresolved gaps, formulas and readability. No external retrieval "
                    "or new research. Never change mathematical acceptance records. "
                    f"Write substantive review and precise corrections to `{review_ref}`. "
                    "Return only {\"verdict\":\"APPROVE\"} or {\"verdict\":\"REVISE\"}. "
                    "APPROVE requires a faithful, usable manuscript; an honest partial result is valid.",
                    workdir=root, call_name="manuscript_independent_review",
                    spec=SemanticCallSpec(role="MANUSCRIPT_INDEPENDENT_REVIEW",
                        semantic_schema={"type":"object", "properties":{"verdict":{"type":"string","enum":["APPROVE","REVISE"]}},
                                         "required":["verdict"], "additionalProperties":False},
                        host_schema_version="manuscript_review/v1", authority="EDITORIAL_ONLY",
                        validator=validate, on_accept=accept),
                    input_refs=(current, *input_refs),
                    host_metadata={"protected_input_refs": (current, *input_refs), "output_refs":[review_ref]},
                )
                if not isinstance(result, StructuredSemanticResult):
                    raise ValueError("Independent review interrupted; resume the saved task")
                review = editorial["last_review"]
            if editorial["approved"]:
                return current
            issues = f"Revise only the issues in `{review['review_ref']}`."
        elif not issues:
            editorial["approved"] = True
            save()
            return current
        # New copies stay alongside the original so Markdown link bases remain
        # stable; TeX bundles get their own directory.
        number = len(list((root / review_base).glob("revision-*"))) + 1
        output = (review_base / (f"revision-{number:03d}.md" if markdown
                  else f"revision-{number:03d}/main.tex"))
        editorial["revision"] = {"output_ref": str(output), "issues": issues}
        save()
    raise ValueError("Manuscript still requires revision; resume its saved review task")
