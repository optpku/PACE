"""Validate SMI delivery interfaces without interpreting mathematical claims."""

import json
from pathlib import Path, PureWindowsPath
from pace.runtime.output.workspace import (
    path_has_symlink_component,
    is_call_path,
)
from pace.runtime.text_revisions import render_revision
from pace.runtime.output.workspace import _write_text_atomic

REVIEW_CONTROL_FIELDS = frozenset(
    {
        "action",
        "brief",
        "rationale",
        "review_kind",
        "mechanism_id",
        "support_disposition",
        "selected_mechanism_ids",
        "representative_mechanism_id",
        "handoff_disposition",
        "handoff_reason",
        "resolution_kind",
        "resolution_argument",
    }
)
REVIEW_PHASES = frozenset(
    {
        "RAW_SEED_CRITIC_MATH",
        "RAW_SEED_CRITIC_RESEARCH",
        "MECHANISM_CRITIC",
        "COVERAGE_JUDGE",
        "FINAL_CHALLENGER",
        "FRONTIER_REFUTE_PROBE",
        "BLIND_PROVER_PROBE",
        "REFUTE_PROVER_PROBE",
    }
)


def host_receipt_fields(root, directory, phase, action, *, routing_context=None):
    """Bind deterministic delivery paths and the assigned mechanism identity."""
    files = {}
    if phase in REVIEW_PHASES:
        files["review_ref"] = "review.md"
    if phase == "SEED_SCOUT":
        files["seed_ref"] = "seed.md"
        if routing_context["seed_operation"] == "REVISE":
            files.update(patch_ref="patch.json", change_ref="change.md")
    elif phase == "FRONTIER_EXPLORER":
        files["change_ref"] = "change.md"
        if action in {"PROPOSE_MECHANISM", "ADD_MECHANISM"}:
            files["mechanism_ref"] = "mechanism.md"
        elif action == "REQUEST_REFUTATION_PROBE":
            files["probe_task_ref"] = "probe_task.md"
    elif phase == "COVERAGE_JUDGE" and action == "FULL_COVERAGE":
        files["closure_brief_ref"] = "closure_brief.md"
    elif phase == "FINAL_CHALLENGER" and action == "RUN_PROBES":
        files["probe_task_ref"] = "probe_task.md"
    elif phase == "FRONTIER_HARDENER" and action == "HARDEN":
        files["candidate_ref"] = "candidate.md"
    fields = {key: (directory / name).relative_to(root).as_posix() for key, name in files.items()}
    if phase == "SEED_SCOUT":
        fields["seed_operation"] = routing_context["seed_operation"]
    if phase == "MECHANISM_CRITIC":
        fields["mechanism_id"] = (routing_context or {})["mechanism_id"]
    return fields


def validate_receipt(
    root, directory, phase, payload, *, seed_base="", inputs=(), routing_context=None
):
    fixed = host_receipt_fields(
        root, directory, phase, payload.get("action"), routing_context=routing_context
    )
    payload = {**payload, **fixed}
    errors = []

    def error(key, message, *, reason_code="phase_receipt_invalid"):
        errors.append(
            {
                "stage": "receipt",
                "json_pointer": "/" + key,
                "reason_code": reason_code,
                "message": message,
            }
        )

    def file(key, ref, *, owned=False):
        if (
            not isinstance(ref, str)
            or not ref
            or Path(ref).is_absolute()
            or PureWindowsPath(ref).drive
            or (".." in Path(ref).parts)
            or ("\\" in ref)
        ):
            error(key, "expected a run-relative file reference")
            return False
        path = root / ref
        if (
            not path.resolve().is_relative_to(root.resolve())
            or path_has_symlink_component(root, path)
            or is_call_path(Path(ref))
        ):
            error(key, "file reference leaves the permitted artifact scope")
            return False
        if owned and (not path.resolve().is_relative_to(directory.resolve())):
            error(key, "new artifact must belong to this phase directory")
            return False
        if not path.is_file() or not path.read_bytes().strip():
            error(key, f"artifact is missing or empty: {ref}", reason_code=(
                "phase_artifact_missing" if key in fixed else "phase_receipt_invalid"
            ))
            return False
        return True

    action = payload["seed_operation"] if phase == "SEED_SCOUT" else payload.get("action")
    routing = routing_context or {}
    if phase == "COVERAGE_JUDGE" and "mechanism_ids" in routing:
        selected = payload.get("selected_mechanism_ids", [])
        if (
            not isinstance(selected, list)
            or any(
                (
                    not isinstance(item, str) or item not in routing["mechanism_ids"]
                    for item in selected
                )
            )
            or len(set(map(str, selected))) != len(selected)
        ):
            error(
                "selected_mechanism_ids", "select distinct promoted mechanism IDs only"
            )
        representative = payload.get("representative_mechanism_id", "")
        if representative and (
            not isinstance(selected, list) or representative not in selected
        ):
            error(
                "representative_mechanism_id",
                "representative must be a selected mechanism",
            )
    if phase == "SEMANTIC_DEDUP" and "tasks" in routing:
        rows = payload.get("decisions", [])
        ids = (
            [row.get("task_id") for row in rows if isinstance(row, dict)]
            if isinstance(rows, list)
            else []
        )
        expected = [row["task_id"] for row in routing["tasks"]]
        if len(ids) != len(expected) or set(map(str, ids)) != set(expected):
            error(
                "decisions", "return each assigned task ID exactly once, with no extras"
            )
        else:
            by_id = {row["task_id"]: row for row in rows}
            predecessors = set(routing.get("prior_refs", ()))
            for task in routing["tasks"]:
                row = by_id[task["task_id"]]
                if (
                    row.get("action") == "DUPLICATE"
                    and row.get("duplicate_of_ref") not in predecessors
                ):
                    error(
                        "decisions",
                        "a duplicate must reference a prior or earlier new task",
                    )
                predecessors.add(task["candidate_ref"])
    if phase == "SEED_SCOUT" and action == "REVISE":
        if file("patch_ref", payload.get("patch_ref"), owned=True) and file(
            "change_ref", payload.get("change_ref"), owned=True
        ):
            try:
                patch = json.loads((root / payload["patch_ref"]).read_text())
                if not isinstance(patch, dict) or not isinstance(
                    patch.get("edits"), list
                ):
                    raise ValueError("revision requires an edits list")
            except (ValueError, TypeError) as exc:
                error("patch_ref", str(exc), reason_code="phase_artifact_invalid")
                return errors
            if not seed_base:
                error("patch_ref", "supplied seed base is missing", reason_code="phase_artifact_invalid")
            elif any(
                (
                    edit.get("old") == (root / seed_base).read_text()
                    for edit in patch.get("edits", [])
                    if isinstance(edit, dict)
                )
            ):
                error(
                    "patch_ref", "whole-seed replacement requires a new research task",
                    reason_code="phase_artifact_invalid",
                )
            else:
                try:
                    path = directory / "seed.md"
                    if path_has_symlink_component(root, path):
                        raise ValueError("revision output must not contain a symlink")
                    text = render_revision(
                        root, payload["patch_ref"], base_ref=seed_base
                    )
                    if path.is_file() and path.read_text() != text:
                        raise ValueError(
                            "revision would overwrite an existing mathematical draft"
                        )
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    error("patch_ref", str(exc), reason_code="phase_artifact_invalid")
    required = []
    if phase == "SEED_SCOUT":
        if action == "CREATE":
            file(
                "seed_ref",
                (directory / "seed.md").relative_to(root).as_posix(),
                owned=True,
            )
    elif phase == "FRONTIER_EXPLORER":
        if action not in {"REJECT", "HANDOFF", "SPLIT"}:
            required.append("candidate_ref")
        if action in {"PROPOSE_MECHANISM", "ADD_MECHANISM"}:
            required.append("mechanism_ref")
        if action == "REQUEST_REFUTATION_PROBE":
            required.append("probe_task_ref")
    elif phase == "COVERAGE_JUDGE" and action == "FULL_COVERAGE":
        required = ["closure_brief_ref"]
    elif phase == "FINAL_CHALLENGER" and action == "RUN_PROBES":
        required = ["probe_task_ref"]
    elif phase == "FRONTIER_HARDENER" and action == "HARDEN":
        required = ["candidate_ref"]
    elif phase == "STATEMENT_NORMALIZER" and action == "NORMALIZE":
        required = ["candidate_ref"]
    if phase in REVIEW_PHASES:
        required.append("review_ref")
    if phase == "FRONTIER_EXPLORER":
        required.append("change_ref")
    for key in required:
        ref = payload.get(key)
        file(key, ref, owned=not (key == "candidate_ref" and ref in inputs))
    if action == "SPLIT" and phase == "FRONTIER_EXPLORER":
        refs = payload.get("child_candidate_refs")
        if (
            not isinstance(refs, list)
            or len(refs) != 2
            or len(set(map(str, refs))) != 2
        ):
            error(
                "child_candidate_refs", "SPLIT requires exactly two distinct task files"
            )
        else:
            for ref in refs:
                file("child_candidate_refs", ref, owned=True)
    for key, value in payload.items():
        if phase == "SEED_SCOUT" and key == "seed_ref":
            continue
        if (
            key.endswith("_ref")
            and value
            and (key not in required)
            and (key != "route_node_ref")
        ):
            file(key, value)
        elif key.endswith("_refs") and value:
            if not isinstance(value, list):
                error(key, "expected a list of file references")
            else:
                for ref in value:
                    file(key, ref)
    if phase in REVIEW_PHASES:
        for key in payload:
            if key not in REVIEW_CONTROL_FIELDS and (
                not key.endswith(("_ref", "_refs"))
            ):
                error(
                    key,
                    "put mathematical review content in review.md; the receipt contains only control fields and file references",
                )
    refs = payload.get("dependency_refs")
    if (phase in REVIEW_PHASES or phase == "FRONTIER_EXPLORER" or "dependency_refs" in payload) and (
        not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs)
    ):
        error(
            "dependency_refs",
            "declare the actually used inputs as a list, including an empty list",
        )
    if phase == "MECHANISM_CRITIC" and action == "PROMOTE":
        if payload.get("support_disposition") not in {
            "LOCAL_SUPPORT",
            "COMPOSITION_NEEDED",
        }:
            error("support_disposition", "PROMOTE requires its support disposition")
    if phase == "COVERAGE_JUDGE" and action in {"SUBSTANTIVE_PARTIAL", "FULL_COVERAGE"}:
        selected = payload.get("selected_mechanism_ids")
        if (
            not isinstance(selected, list)
            or not selected
            or (not all((isinstance(x, str) and x for x in selected)))
        ):
            error(
                "selected_mechanism_ids",
                "coverage action requires selected promoted mechanism IDs",
            )
    if phase == "SEMANTIC_DEDUP":
        for row in payload.get("decisions", ()):
            if row.get("action") == "DUPLICATE":
                ref = row.get("duplicate_of_ref")
                if ref not in inputs:
                    error("duplicate_of_ref", "duplicate must identify a supplied task")
                else:
                    file("duplicate_of_ref", ref)
    return errors


def materialize_seed_revision(root, directory, *, seed_base):
    """Acceptance hook: write a fully checked derived seed once, never during validation."""
    output = directory / "seed.md"
    if path_has_symlink_component(root, output):
        raise ValueError("invalid derived seed output")
    patch_ref = (directory / "patch.json").relative_to(root).as_posix()
    text = render_revision(root, patch_ref, base_ref=seed_base)
    if output.exists():
        if output.read_text() != text:
            raise ValueError(
                "derived seed differs from the existing mathematical draft"
            )
        return
    _write_text_atomic(output, text)
