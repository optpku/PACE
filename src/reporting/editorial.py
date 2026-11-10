"""Resumable manuscript format repair and independent editorial review."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil

from pace.runtime.math_format import (artifact_format_issues, format_repair_plan,
                                     local_format_repair, validate_format_revision)
from pace.runtime.output.calls import artifact_owner_metadata
from pace.runtime.output.workspace import _write_json_atomic, _write_text_atomic, path_has_symlink_component
from pace.runtime.text_revisions import apply_revision, apply_bundle_revision
from pace.runtime.semantic import (
    ArtifactCallHandle, SemanticCallSpec, StructuredSemanticResult,
    begin_artifact_call, commit_artifact_call_failure, commit_artifact_call_success,
    semantic_call,
)

_FORMAT_PATCH_INSTRUCTION = (
    'Write only a JSON patch {"edits":[{"old":"unique exact passage","new":"corrected passage"}]} '
    "to each assigned patch_ref. Each old passage must occur exactly once in its source. "
    "Use the smallest sufficient surrounding context; never replace the whole manuscript. "
    "Preserve original line endings and all unflagged text. The Host applies the patch to "
    "a complete new copy at output_ref; do not write that output file yourself. "
)

_SCOPED_FORMAT_INSTRUCTION = (
    "Read the supplied diagnostic excerpts first. Full sources are read-only archives, "
    "available only if the excerpts leave a concrete ambiguity. Only the listed token "
    "replacements are permitted; all other bytes, including mathematical statements, "
    "conditions and evidence links, must remain unchanged. Do not guess missing evidence. "
    "For a diagnosed missing link without a fixed replacement, only its destination may "
    "change, and it must reference existing run-local evidence; preserve its label. "
    "If no permitted edit resolves the defect, explain that it requires content revision. "
)


def manuscript_hashes(root: Path, ref: str, *, markdown: bool) -> dict[str, str]:
    """Bind review/recovery to manuscript content, excluding generated build logs."""
    source = root / ref
    files = [source] if markdown else sorted(
        p for p in source.parent.rglob("*")
        if p.is_file() and p != source.with_suffix(".pdf") and p.suffix.lower() in {
            ".tex", ".bib", ".bbl", ".sty", ".cls", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg"})
    if source not in files:
        raise ValueError("Manuscript source is missing")
    result = {}
    for file in files:
        if path_has_symlink_component(root, file):
            raise ValueError("Manuscript source must not contain symlinks")
        key = "text" if markdown else file.relative_to(source.parent).as_posix()
        result[key] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def repair_artifact_batch(*, llm, root: Path, state: dict, state_ref: str,
                          refs: tuple[str, ...]) -> None:
    """Preflight one specialist delivery and repair all defects in one batch.

    The caller has already saved the author handle. All repair calls and copies
    are checkpointed here; interruption never sends the task back to the author.
    No mathematical acceptance is granted by this representation check.
    """
    def digest(ref):
        path = root / ref
        if path_has_symlink_component(root, path) or not path.is_file():
            raise ValueError(f"Artifact must be a regular run-local file: {ref}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save():
        _write_json_atomic(root / state_ref, state)

    if "format_batch" not in state:
        hashes = {ref: digest(ref) for ref in refs}
        state["format_batch"] = {
            "refs": list(refs), "current": {ref: ref for ref in refs},
            "source_hashes": hashes.copy(), "hashes": hashes,
            "attempts": [], "pending": None,
        }
    batch = state["format_batch"]
    if batch["refs"] != list(refs):
        raise ValueError("Saved format delivery has different artifact references")
    save()
    calls = 0
    while True:
        # Validate before another repair reads its inputs, not only after a
        # repaired copy has replaced the saved source hash.
        for ref, source in batch["current"].items():
            if digest(source) != batch["hashes"][ref]:
                raise ValueError(f"Saved format artifact changed: {source}")
            allowed = {batch["source_hashes"][ref]}
            if batch.get("materializing"):
                allowed.add(batch["hashes"][ref])
            if digest(ref) not in allowed:
                raise ValueError(f"Author artifact changed after delivery: {ref}")
        pending = batch["pending"]
        if pending:
            if not pending.get("handle"):
                for file in pending["files"]:
                    file.setdefault("patch_ref", str(Path(file["output_ref"]).with_suffix(".patch.json")))
                save()
                if calls >= 2:
                    raise ValueError("Format batch still needs correction; resume the saved repair")
                calls += 1
                scoped = all(file.get("format_plan") for file in pending["files"])
                inputs = tuple(dict.fromkeys(file["context_ref"] if scoped else file["source_ref"]
                                            for file in pending["files"]))
                task_files = [{k: v for k, v in file.items() if k not in {"format_plan", "issues"}}
                              for file in pending["files"]] if scoped else pending["files"]
                handle = begin_artifact_call(
                    llm,
                    "Correct only the listed formatting and local-link defects in this batch. "
                    "Do not change mathematical statements, assumptions, formula meanings, proof "
                    "steps, source versions or evidence. Do not research or retrieve sources. "
                    + (_SCOPED_FORMAT_INSTRUCTION if scoped else "Read each source below. ")
                    + _FORMAT_PATCH_INSTRUCTION +
                    "Relative links are relative "
                    "to the logical original file, not the repair directory. Preserve backslashes "
                    "through every string/JSON/shell layer; use raw literals or correct escaping. "
                    "If a link cannot be repaired from saved evidence, retain the gap and explain "
                    "it; never invent an evidence file. Do not repair inputs in place.\n"
                    + json.dumps(task_files, ensure_ascii=False, indent=2),
                    workdir=root, call_name="artifact_format_repair",
                    role="ARTIFACT_FORMAT_REPAIR", stage="HOST_ARTIFACT_FORMAT_REPAIR",
                    input_refs=inputs,
                    metadata={**artifact_owner_metadata(root, state_ref), "inline_task": True,
                              "protected_input_refs": (*refs, *batch["current"].values(), *inputs, state_ref),
                              "output_refs": [file.get("patch_ref", file["output_ref"]) for file in pending["files"]]},
                )
                if handle.transport_status != "SUCCESS":
                    commit_artifact_call_failure(handle, workdir=root,
                        failure_class="INTERFACE", retry_class=handle.retry_class,
                        diagnostics_ref=state_ref)
                    raise ValueError("Format batch interrupted; resume its saved repair")
                pending["handle"] = asdict(handle)
                save()
            handle = ArtifactCallHandle(**pending["handle"])
            error = ""
            completed = {}
            for file in pending["files"]:
                ref, output = file["logical_ref"], file["output_ref"]
                try:
                    if file.get("patch_ref"):
                        digest(file["patch_ref"])
                        apply_revision(root, file["patch_ref"], base_ref=file["source_ref"], output_ref=output,
                                       format_plan=file.get("format_plan"))
                    output_hash = digest(output)
                    if not (root / output).read_bytes().decode("utf-8").strip():
                        raise ValueError(f"Empty repaired artifact: {output}")
                except (OSError, UnicodeError, ValueError) as exc:
                    error = str(exc)
                    break
                completed[ref] = (output, output_hash)
            batch["attempts"].append(pending)
            batch["pending"] = None
            if error:
                save()
                commit_artifact_call_failure(handle, workdir=root,
                    failure_class="REPRESENTATION", retry_class="MODEL_REPRESENTATION",
                    diagnostics_ref=state_ref)
                raise ValueError(error)
            for ref, (output, output_hash) in completed.items():
                batch["current"][ref] = output
                batch["hashes"][ref] = output_hash
            save()
            commit_artifact_call_success(handle, workdir=root,
                                         artifact_ref=pending["files"][0]["output_ref"])

        issues, plans = [], {}
        for index, (ref, source) in enumerate(batch["current"].items()):
            if digest(source) != batch["hashes"][ref]:
                raise ValueError(f"Saved format artifact changed: {source}")
            if digest(ref) not in (batch["source_hashes"][ref], batch["hashes"][ref]):
                raise ValueError(f"Author artifact changed after delivery: {ref}")
            text = (root / source).read_bytes().decode("utf-8")
            plan = format_repair_plan(root, ref, text=text)
            corrected = local_format_repair(text, plan)
            if corrected != text:
                validate_format_revision(text, corrected, plan, root=root)
                number = len(batch.get("local_repairs", [])) + 1
                output = str(Path(state_ref).parent / "artifact_format" / f"local-{number:03d}" / f"{index:03d}.md")
                _write_text_atomic(root / output, corrected)
                batch.setdefault("local_repairs", []).append({
                    "source_ref": source, "source_sha256": plan["source_sha256"], "output_ref": output,
                    "rule_version": plan["version"], "edits": [i for i in plan["issues"] if i["automatic"]]})
                batch["current"][ref], batch["hashes"][ref] = output, digest(output)
                save()
                plan = format_repair_plan(root, ref, text=corrected)
            plans[ref] = plan
            issues.extend(plan["issues"])
        batch["issues"] = issues
        save()
        if not issues:
            # Preserve exact original bytes before installing corrected copies.
            # The saved hashes allow resume after any individual atomic replace.
            batch["materializing"] = True
            save()
            for index, (ref, source) in enumerate(batch["current"].items()):
                if ref == source:
                    continue
                backup = root / Path(state_ref).parent / "artifact_format" / "originals" / f"{index:03d}.md"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    backup.write_bytes((root / ref).read_bytes())
                _write_text_atomic(root / ref, (root / source).read_bytes().decode("utf-8"))
            batch["applied"] = True
            save()
            return
        number = len(batch["attempts"]) + 1
        directory = Path(state_ref).parent / "artifact_format" / f"repair-{number:03d}"
        (root / directory).mkdir(parents=True, exist_ok=True)
        batch["pending"] = {"files": [
            {"logical_ref": ref, "source_ref": batch["current"][ref],
             "output_ref": str(directory / f"{index:03d}.md"),
             "patch_ref": str(directory / f"{index:03d}.patch.json"),
             "format_plan": plans[ref],
             "context_ref": str(directory / f"{index:03d}.context.json"),
             "issues": [issue for issue in issues if issue["file"] == ref]}
            for index, ref in enumerate(refs) if any(issue["file"] == ref for issue in issues)
        ]}
        for file in batch["pending"]["files"]:
            _write_json_atomic(root / file["context_ref"], file["format_plan"])
        save()


def request_manuscript_revision(*, root: Path, state: dict, state_ref: str,
                                source_ref: str, issues: str, markdown: bool,
                                input_refs: tuple[str, ...] = (),
                                format_only: bool = False, local_patch: bool = False) -> None:
    """Save one local revision before calling its author, retaining past reviews."""
    editorial = state.setdefault("editorial", {
        "current_ref": source_ref, "reviews": [], "revision": None, "approved": False,
    })
    if editorial.get("revision"):
        return
    base = Path(source_ref).parent if markdown else Path(source_ref).parent.parent
    number = max((int(match[1]) for file in (root / base).glob("revision-*")
                  if (match := re.match(r"revision-(\d+)", file.name))), default=0) + 1
    output = base / (f"revision-{number:03d}.md" if markdown else f"revision-{number:03d}/main.tex")
    editorial.update(current_ref=source_ref, approved=False, last_review=None,
                     revision={"output_ref": str(output), "issues": issues,
                               "format_only": format_only,
                               "input_refs": list(input_refs),
                               "source_hashes": manuscript_hashes(root, source_ref, markdown=markdown)})
    editorial.setdefault("revision_refs", []).append(str(output))
    if (format_only and markdown) or local_patch:
        editorial["revision"]["patch_ref"] = str(output.with_suffix(".patch.json"))
        if not markdown:
            editorial["revision"]["bundle_patch"] = True
    if format_only:
        plan = format_repair_plan(root, source_ref)
        if plan["issues"]:
            revision = editorial["revision"]
            revision["format_plan"] = plan
            revision["patch_ref"] = str(output.with_suffix(".patch.json"))
            revision["context_ref"] = str(output.with_suffix(".context.json"))
            revision["local_repair"] = any(issue["automatic"] for issue in plan["issues"])
            if not markdown:
                revision["bundle_patch"] = True
            _write_json_atomic(root / revision["context_ref"], plan)
    _write_json_atomic(root / state_ref, state)


def review_draft(*, llm, root: Path, state: dict, state_ref: str,
                 source_ref: str, input_refs: tuple[str, ...], markdown: bool,
                 independent: bool = True, continuous: bool = False) -> str:
    """Keep accepted author/reviewer stages; revise only the recorded defect."""
    editorial = state.setdefault("editorial", {
        "current_ref": source_ref, "reviews": [], "revision": None,
        "approved": False,
    })

    def save():
        _write_json_atomic(root / state_ref, state)

    review_base = Path(source_ref).parent if markdown else Path(source_ref).parent.parent
    save()
    revisions = 0
    while True:
        current = editorial["current_ref"]
        hashes = manuscript_hashes(root, current, markdown=markdown)
        if editorial.setdefault("current_hashes", hashes) != hashes:
            raise ValueError("Saved manuscript changed outside its revision task")
        if editorial["approved"]:
            return current
        revision = editorial["revision"]
        if revision:
            local = revision.get("local_repair", False)
            if not local and not continuous and revisions >= 2:
                raise ValueError("Manuscript still requires revision; resume its saved review task")
            revisions += int(not local)
            if revision.setdefault("source_hashes", hashes) != hashes:
                raise ValueError("Revision belongs to a different manuscript version")
            output = root / revision["output_ref"]
            format_only = revision.get("format_only", revision["issues"].startswith("Formatting-only repair"))
            revision_inputs = tuple(dict.fromkeys((current,
                *(() if format_only else input_refs), *revision.get("input_refs", ()))))
            if revision.get("format_plan"):
                revision_inputs = (revision["context_ref"],)
            if local:
                output.parent.mkdir(parents=True, exist_ok=True)
                if not markdown:
                    shutil.copytree((root / current).parent, output.parent, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("revision-*", "editorial"))
                original = (root / current).read_bytes().decode("utf-8")
                corrected = local_format_repair(original, revision["format_plan"])
                validate_format_revision(original, corrected, revision["format_plan"], root=root)
                _write_text_atomic(output, corrected)
            elif not revision.get("handle"):
                if (format_only and markdown) or continuous:
                    revision.setdefault("patch_ref", str(output.relative_to(root).with_suffix(".patch.json")))
                    if not markdown:
                        revision["bundle_patch"] = True
                    save()
                output.parent.mkdir(parents=True, exist_ok=True)
                if not markdown:
                    # Preserve the complete manuscript bundle and relative asset paths.
                    shutil.copytree((root / current).parent, output.parent, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("revision-*", "editorial"))
                    if not revision.get("bundle_patch"):
                        output.unlink(missing_ok=True)
                prompt = (
                    f"Revise the saved manuscript `{current}` only for the recorded issues below. "
                    "Preserve all unaffected statements, assumptions, proofs and evidence links. "
                    "Do not restart research or retrieve external sources. "
                    "For formatting-only issues, do not change mathematical content. "
                    "Preserve LaTeX backslashes through every string/JSON/shell layer; "
                    "use raw literals or correctly escaped strings when writing files. "
                    "For substantive editorial issues, correct the manuscript to match the existing "
                    "evidence; explicitly retain unresolved gaps instead of inventing proofs. "
                    f"Read the assigned evidence references: {list(revision_inputs)}.\n"
                    f"Issues: {revision['issues']}\n"
                    f"## Deliverable\nWrite `{revision['output_ref']}`\n"
                    "Write the complete corrected file, preserving relative links and assets. "
                    "Original inputs are read-only."
                )
                if revision.get("patch_ref"):
                    prompt = (
                        f"Correct only the recorded issues in `{current}` using a local patch. "
                        + ("This is formatting-only: do not change mathematical content. " if format_only else
                           "Correct claims, hypotheses and formulas against the saved evidence. "
                           "Keep open gaps explicit; do not invent proofs or change acceptance records. ") +
                        "Do not conduct research or external retrieval. "
                        "Preserve TeX backslashes through every JSON/string/shell layer. "
                        + ("Write JSON {\"files\":[{\"path\":\"main.tex\",\"edits\":[{\"old\":\"unique exact passage\","
                           "\"new\":\"corrected passage\",\"issue_id\":\"review issue\",\"reason\":\"brief explanation\"}]}]}. "
                           "Paths are relative to the original source bundle. Edit only existing .tex/.bib/.bbl files; "
                           "use unique exact anchors and preserve all unrelated text and assets. "
                           "The Host copies and patches the complete bundle. Never write the output bundle yourself. "
                           if revision.get("bundle_patch") else _FORMAT_PATCH_INSTRUCTION)
                        + "For content corrections, identify the review issue and briefly explain each edit in the patch. "
                        + f"\nsource_ref: `{current}`\npatch_ref: `{revision['patch_ref']}`"
                        + f"\noutput_ref (Host only): `{revision['output_ref']}`\nIssues: {revision['issues']}"
                        + f"\nEvidence references: {list(revision_inputs)}"
                    )
                if revision.get("delivery_error"):
                    prompt += "\nPrevious local correction failed: " + revision["delivery_error"] + (
                        "\nRepair this precise failure against the unchanged source and review. "
                        "Do not rewrite the manuscript or repeat its review without an effective correction.")
                if revision.get("format_plan"):
                    prompt = (
                        f"Correct the diagnosed formatting defects in archive `{current}`. "
                        + _SCOPED_FORMAT_INSTRUCTION + _FORMAT_PATCH_INSTRUCTION
                        + ("For a TeX bundle wrap edits as {\"files\":[{\"path\":\""
                           + Path(current).name + "\",\"edits\":[...]}]}. " if not markdown else "")
                        + f"\nDiagnostics: `{revision['context_ref']}`"
                        + f"\npatch_ref: `{revision['patch_ref']}`"
                        + f"\noutput_ref (Host only): `{revision['output_ref']}`"
                        + f"\nPrevious delivery failure: {revision.get('delivery_error', '')}")
                handle = begin_artifact_call(
                    llm, prompt, workdir=root, call_name="manuscript_revision",
                    role="ARTIFACT_FORMAT_REPAIR" if format_only else "MANUSCRIPT_REVISION",
                    stage="HOST_ARTIFACT_FORMAT_REPAIR" if format_only else "HOST_MANUSCRIPT_REVISION",
                    input_refs=revision_inputs,
                    metadata={**artifact_owner_metadata(root, state_ref), "inline_task": True,
                              "protected_input_refs": (current, *input_refs, *revision_inputs, state_ref),
                              "output_ref": revision.get("patch_ref", revision["output_ref"]),
                              "output_dirs": [] if revision.get("patch_ref") else [str(output.parent.relative_to(root))]},
                )
                if handle.transport_status != "SUCCESS":
                    diagnostic = str(review_base / "editorial" / f"{handle.call_id}.failure.json")
                    _write_json_atomic(root / diagnostic, {"error": handle.error_code,
                                                           "output_ref": revision["output_ref"]})
                    commit_artifact_call_failure(handle, workdir=root,
                        failure_class="INTERFACE", retry_class=handle.retry_class,
                        diagnostics_ref=diagnostic)
                    raise ValueError("Manuscript revision interrupted; resume the saved task")
                revision["handle"] = asdict(handle)
                save()
            if manuscript_hashes(root, current, markdown=markdown) != hashes:
                raise ValueError("Original manuscript changed during local revision")
            try:
                if revision.get("patch_ref") and not local:
                    patch = root / revision["patch_ref"]
                    if path_has_symlink_component(root, patch) or not patch.is_file():
                        raise ValueError("Format patch must be a regular run-local file")
                    apply = apply_bundle_revision if revision.get("bundle_patch") else apply_revision
                    guard = {}
                    if revision.get("format_plan"):
                        guard = ({"format_plans": {Path(current).name: revision["format_plan"]}}
                                 if revision.get("bundle_patch") else {"format_plan": revision["format_plan"]})
                    apply(root, revision["patch_ref"], base_ref=current, output_ref=revision["output_ref"], **guard)
                if output.is_symlink() or not output.is_file():
                    raise ValueError("Manuscript revision must be a regular file")
                if not output.read_text(encoding="utf-8").strip():
                    raise ValueError("Manuscript revision is empty")
                updated_hashes = manuscript_hashes(root, revision["output_ref"], markdown=markdown)
                if continuous and (updated_hashes == hashes or any(
                        row["source_hashes"] == updated_hashes for row in editorial.get("changes", ()))):
                    raise ValueError("Local correction made no change or returned to an already rejected version")
            except (OSError, UnicodeError, ValueError) as exc:
                if local:
                    raise
                failed = ArtifactCallHandle(**revision.pop("handle"))
                if output.is_symlink():
                    output.unlink()
                revision["delivery_error"] = str(exc)
                diagnostic = str(review_base / "editorial" / f"{failed.call_id}.failure.json")
                _write_json_atomic(root / diagnostic, {"error": str(exc),
                                                       "output_ref": revision["output_ref"]})
                save()
                commit_artifact_call_failure(failed, workdir=root,
                    failure_class="REPRESENTATION", retry_class="MODEL_REPRESENTATION",
                    diagnostics_ref=diagnostic)
                if continuous and not revision.get("focused_retry"):
                    editorial.setdefault("failed_revisions", []).append(dict(revision))
                    editorial["revision"] = None
                    request_manuscript_revision(root=root, state=state, state_ref=state_ref,
                        source_ref=current, issues=revision["issues"], markdown=markdown,
                        input_refs=tuple(revision.get("input_refs", ())), format_only=format_only, local_patch=True)
                    editorial["revision"].update(focused_retry=True, delivery_error=str(exc))
                    save()
                    continue
                raise ValueError("Manuscript revision delivery failed; resume this revision: " + str(exc)) from exc
            # Commit the new file even if another format repair is needed. Resume
            # never returns to the original full author call.
            handle = ArtifactCallHandle(**revision["handle"]) if not local else None
            editorial.setdefault("changes", []).append({
                "source_ref": current, "source_hashes": hashes,
                "output_ref": revision["output_ref"], "output_hashes": updated_hashes,
                "patch_ref": revision.get("patch_ref", "") if not local else "", "issues": revision["issues"],
                "format_only": format_only,
                "local_repair": local,
                "format_context_ref": revision.get("context_ref", ""),
            })
            editorial["current_ref"] = revision["output_ref"]
            editorial["current_hashes"] = updated_hashes
            editorial["revision"] = None
            editorial["last_review"] = None
            save()
            if handle is not None:
                commit_artifact_call_success(handle, workdir=root, artifact_ref=editorial["current_ref"])
            current = editorial["current_ref"]
        preflight = artifact_format_issues(root, current)
        issues = ("Formatting-only repair: " + json.dumps(preflight, ensure_ascii=False)
                  if preflight else "")
        if not issues and independent:
            review = editorial.get("last_review")
            if not review:
                index = len(editorial["reviews"]) + 1
                review_ref = str(review_base / "editorial" / f"review-{index:03d}.md")
                (root / review_ref).parent.mkdir(parents=True, exist_ok=True)

                def validate(payload):
                    path = root / review_ref
                    if manuscript_hashes(root, current, markdown=markdown) != editorial["current_hashes"]:
                        return [{"message": "Manuscript changed during review", "stage": "acceptance"}]
                    return [] if path.is_file() and path.read_text().strip() else [
                        {"message": "The assigned independent review file is missing", "stage": "acceptance"}]

                def accept(payload):
                    editorial["last_review"] = {**payload, "review_ref": review_ref, "source_ref": current,
                                                "source_hashes": editorial["current_hashes"]}
                    editorial["reviews"].append(editorial["last_review"])
                    if payload["verdict"] == "APPROVE":
                        editorial["approved"] = True
                    save()

                prior = editorial["reviews"][-1] if editorial["reviews"] else None
                followup = (
                    f"\nPrior independent review: `{prior['review_ref']}` on `{prior['source_ref']}`. "
                    "Keep stable issue IDs. Check each unresolved issue and the impact of the changed passages "
                    "against the evidence; do not reopen unchanged resolved issues without a concrete reason. "
                    "New substantive errors still require correction. Separate required corrections from "
                    "optional style suggestions; optional preferences alone must not produce REVISE. "
                    "Record resolved IDs, remaining IDs, newly found defects and exact locations in the review. "
                    + json.dumps(editorial.get("changes", [])[-1:], ensure_ascii=False)
                    if prior else
                    "\nPerform a complete first review. Collect all concrete required corrections in one "
                    "numbered issue list with stable IDs, locations and evidence references. "
                    "Separate optional style preferences; they alone must not produce REVISE. ")
                review_inputs = tuple(dict.fromkeys((current, *input_refs,
                    *((prior["review_ref"], prior["source_ref"]) if prior else ()),
                    *(row["patch_ref"] for row in editorial.get("changes", [])[-1:] if row.get("patch_ref")))))
                result = semantic_call(
                    llm,
                    f"Independently review manuscript `{current}` against the saved evidence "
                    f"and author requirements in {list(input_refs)}. Do not reuse the author's "
                    "self-assessment. Check claim strength, hypotheses, complete proof scope, "
                    "citations, unresolved gaps, formulas and readability. No external retrieval "
                    "or new research. Never change mathematical acceptance records. "
                    f"Write substantive review and precise corrections to `{review_ref}`. "
                    "Return only {\"verdict\":\"APPROVE\"} or {\"verdict\":\"REVISE\"}. "
                    "APPROVE requires a faithful, usable manuscript; an honest partial result is valid."
                    + followup,
                    workdir=root, call_name="manuscript_independent_review",
                    spec=SemanticCallSpec(role="MANUSCRIPT_INDEPENDENT_REVIEW",
                        semantic_schema={"type":"object", "properties":{"verdict":{"type":"string","enum":["APPROVE","REVISE"]}},
                                         "required":["verdict"], "additionalProperties":False},
                        host_schema_version="manuscript_review/v1", authority="EDITORIAL_ONLY",
                        validator=validate, on_accept=accept),
                    input_refs=review_inputs,
                    host_metadata={**artifact_owner_metadata(root, state_ref),
                                   "protected_input_refs": review_inputs, "output_refs":[review_ref]},
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
        request_manuscript_revision(root=root, state=state, state_ref=state_ref,
                                    source_ref=current, issues=issues, markdown=markdown,
                                    input_refs=(review["review_ref"],) if independent and not preflight else (),
                                    format_only=issues.startswith("Formatting-only repair:"), local_patch=continuous)
