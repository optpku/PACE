"""Resumable representation repair for research deliveries, without editorial review."""
from __future__ import annotations
from dataclasses import asdict
import json
from pathlib import Path
from pace.runtime.math_format import format_repair_plan, local_format_repair, validate_format_revision
from pace.runtime.output.calls import artifact_owner_metadata
from pace.runtime.output.workspace import _write_json_atomic, _write_text_atomic, path_has_symlink_component
from pace.runtime.text_revisions import apply_revision
from pace.runtime.semantic import ArtifactCallHandle, begin_artifact_call, commit_artifact_call_failure, commit_artifact_call_success

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


def repair_artifact_batch(*, llm, root: Path, state: dict, state_ref: str,
                          refs: tuple[str, ...]) -> None:
    """Preflight one specialist delivery and repair all defects in one batch.

    The caller has already saved the author handle. All repair calls and copies
    are checkpointed here; interruption never sends the task back to the author.
    No mathematical acceptance is granted by this representation check.
    """
    def read_content(ref):
        path = root / ref
        if path_has_symlink_component(root, path) or not path.is_file():
            raise ValueError(f"Artifact must be a regular run-local file: {ref}")
        return path.read_bytes().hex()

    def save():
        _write_json_atomic(root / state_ref, state)

    if "format_batch" not in state:
        contents = {ref: read_content(ref) for ref in refs}
        state["format_batch"] = {
            "refs": list(refs), "current": {ref: ref for ref in refs},
            "original_contents": contents.copy(), "contents": contents,
            "attempts": [], "pending": None,
        }
    batch = state["format_batch"]
    if batch["refs"] != list(refs):
        raise ValueError("Saved format delivery has different artifact references")
    save()
    calls = 0
    while True:
        # Validate before another repair reads its inputs, not only after a
        # repaired copy has replaced the saved source text.
        for ref, source in batch["current"].items():
            if read_content(source) != batch["contents"][ref]:
                raise ValueError(f"Saved format artifact changed: {source}")
            allowed = {batch["original_contents"][ref]}
            if batch.get("materializing"):
                allowed.add(batch["contents"][ref])
            if read_content(ref) not in allowed:
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
                        read_content(file["patch_ref"])
                        apply_revision(root, file["patch_ref"], base_ref=file["source_ref"], output_ref=output,
                                       format_plan=file.get("format_plan"))
                    output_content = read_content(output)
                    if not (root / output).read_bytes().decode("utf-8").strip():
                        raise ValueError(f"Empty repaired artifact: {output}")
                except (OSError, UnicodeError, ValueError) as exc:
                    error = str(exc)
                    break
                completed[ref] = (output, output_content)
            batch["attempts"].append(pending)
            batch["pending"] = None
            if error:
                save()
                commit_artifact_call_failure(handle, workdir=root,
                    failure_class="REPRESENTATION", retry_class="MODEL_REPRESENTATION",
                    diagnostics_ref=state_ref)
                raise ValueError(error)
            for ref, (output, output_content) in completed.items():
                batch["current"][ref] = output
                batch["contents"][ref] = output_content
            save()
            commit_artifact_call_success(handle, workdir=root,
                                         artifact_ref=pending["files"][0]["output_ref"])

        issues, plans = [], {}
        for index, (ref, source) in enumerate(batch["current"].items()):
            if read_content(source) != batch["contents"][ref]:
                raise ValueError(f"Saved format artifact changed: {source}")
            if read_content(ref) not in (batch["original_contents"][ref], batch["contents"][ref]):
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
                    "source_ref": source, "source_text": plan["source_text"], "output_ref": output,
                    "rule_version": plan["version"], "edits": [i for i in plan["issues"] if i["automatic"]]})
                batch["current"][ref], batch["contents"][ref] = output, read_content(output)
                save()
                plan = format_repair_plan(root, ref, text=corrected)
            plans[ref] = plan
            issues.extend(plan["issues"])
        batch["issues"] = issues
        save()
        if not issues:
            # Preserve exact original bytes before installing corrected copies.
            # The saved contents allow resume after any individual atomic replace.
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
            _write_json_atomic(root / file["context_ref"], {key: value for key, value in file["format_plan"].items() if key != "source_text"})
        save()
