"""Select declared task dependencies, retaining full artifacts on disk."""

from pathlib import Path
import json

from pace.runtime.output.workspace import (
    is_call_path,
    path_has_symlink_component,
)


def linked_review_refs(root, refs):
    """Direct review/change artifacts named by supplied receipts, never call logs."""
    linked = []
    for ref in refs:
        if Path(ref).suffix != ".json" or not (root / ref).is_file():
            continue
        try:
            receipt = json.loads((root / ref).read_text())
        except ValueError:
            continue
        if not isinstance(receipt, dict):
            continue
        for key in ("review_ref", "change_ref"):
            value = receipt.get(key)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in value
                or is_call_path(path)
                or not (root / path).resolve().is_relative_to(root.resolve())
                or path_has_symlink_component(root, root / path)
            ):
                continue
            if (root / path).is_file():
                linked.append(value)
    return tuple(dict.fromkeys(linked))


def objection_key(root, ref):
    """Bind the objection's receipt and its actual Markdown content together."""
    key, content = dependency_key(root, ref)
    linked = linked_review_refs(root, (ref,))
    if linked:
        content = dict(dependency_key(root, item) for item in (ref, *linked))
    return key, content


# Roles receive their own predecessor classes; blind probes remain statement-only.
INPUT_CLASSES = {
    "SEED_SCOUT": {"TARGET_ANCHOR", "WORK_ITEM", "SOURCE", "SEED", "REPAIR_BRIEF"},
    "RAW_SEED_CRITIC_MATH": {
        "TARGET_ANCHOR",
        "WORK_ITEM",
        "SEED",
        "SOURCE",
        "REPAIR_BRIEF",
    },
    "RAW_SEED_CRITIC_RESEARCH": {
        "TARGET_ANCHOR",
        "WORK_ITEM",
        "SEED",
        "SOURCE",
        "REPAIR_BRIEF",
    },
    "FRONTIER_EXPLORER": {
        "TARGET_ANCHOR",
        "WORK_ITEM",
        "REVIEWED_SEED",
        "CANDIDATE",
        "SOURCE",
        "MECHANISM",
        "MECHANISM_RECEIPT",
        "COVERAGE",
        "FRONTIER_REFUTE_RECEIPT",
        "INTERNAL_SEARCH_CONTEXT",
        "REPAIR_BRIEF",
        "HISTORICAL_SUPPORT",
    },
    "MECHANISM_CRITIC": {
        "CHANGE",
        "TARGET_ANCHOR",
        "WORK_ITEM",
        "CANDIDATE",
        "MECHANISM",
        "MECHANISM_RECEIPT",
        "DEPENDENCY_MECHANISM",
        "DEPENDENCY_RECEIPT",
        "PREVIOUS_MECHANISM",
        "PREVIOUS_REVIEW",
        "REPAIR_BRIEF",
        "MECHANISM_SOURCE",
        "INTERNAL_SEARCH_CONTEXT",
    },
    "FINAL_CHALLENGER": {"TARGET_ANCHOR", "CANDIDATE", "SOURCE"},
    "BLIND_PROVER_PROBE": {"PROBE_TASK"},
    "REFUTE_PROVER_PROBE": {"PROBE_TASK"},
    "FRONTIER_REFUTE_PROBE": {"PROBE_TASK"},
}


def select_inputs(phase, inputs, root):
    selected = {}
    seen = set()
    # Source copies that repeat the anchor/seed are redundant, not extra premises.
    for kind, refs in inputs.items():
        if phase in INPUT_CLASSES and kind not in INPUT_CLASSES[phase]:
            continue
        kept = []
        for ref in refs:
            path = root / ref
            identity = dependency_key(root, ref)[1] if path.is_file() else ref
            if kind == "SOURCE" and identity in seen:
                continue
            kept.append(ref)
            seen.add(identity)
        if kept:
            selected[kind] = tuple(kept)
    return selected


def dependency_key(root, ref):
    """Use original dependency identity across immutable per-dispatch copies."""
    path = Path(ref)
    content = (
        (root / ref).read_text(encoding="utf-8").encode("utf-8")
        if path.suffix == ".md"
        else (root / ref).read_bytes()
    )
    if "source_snapshot" in path.parts:
        position = path.parts.index("source_snapshot")
        manifest = root.joinpath(*path.parts[:position], "source_snapshot.json")
        if manifest.is_file():
            row = next(
                (
                    row
                    for row in json.loads(manifest.read_text()).get("provenance", ())
                    if row["snapshot_ref"] == ref
                ),
                None,
            )
            if row:
                from pace.runtime.output.results import relocate_links

                if path.suffix == ".md":
                    content = relocate_links(
                        content.decode("utf-8"), root / ref, root / row["original_ref"]
                    ).encode("utf-8")
                return row["original_ref"], content.hex()
    return ref, content.hex()


def scope_snapshot(root, candidate, anchor, rules, dependencies):
    expanded = [*dependencies, *linked_review_refs(root, dependencies)]
    objections = {}
    for ref in dependencies:
        if Path(ref).suffix != ".json" or not (root / ref).is_file():
            continue
        try:
            receipt = json.loads((root / ref).read_text())
        except ValueError:
            continue
        if not isinstance(receipt, dict):
            continue
        verdict = receipt.get("verdict")
        if receipt.get("action") in {"REPAIR", "REJECT"} or (
            isinstance(verdict, dict) and verdict.get("stage_status") == "MATH_FAIL"
        ):
            key, content = objection_key(root, ref)
            objections[key] = content
    return {
        "candidate": (root / candidate).read_text(),
        "anchor": (root / anchor).read_text(),
        "rules": rules,
        "objections": objections,
        "dependencies": dict(
            dependency_key(root, ref) for ref in expanded if (root / ref).is_file()
        ),
    }


def scope_matches(previous, current):
    return (
        previous.get("objections", {}) == current.get("objections", {})
        and all(
            previous.get(key) == current.get(key)
            for key in ("candidate", "anchor", "rules")
        )
        and all(
            current["dependencies"].get(ref) == content
            for ref, content in previous.get("dependencies", {}).items()
        )
    )
