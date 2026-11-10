"""Explicit draft versions; selection never confers mathematical acceptance."""

import hashlib
import json
from pathlib import Path

from .results import finding_publications
from .workspace import _write_json_atomic, path_has_symlink_component


FINDING_MANIFEST_POLICY = (
    "When creating or revising standalone findings, write finding_manifest.json beside "
    "your assigned main deliverable (inside your own phase directory for RCI). Format: "
    '{"current_refs":["run-relative finding.md"],"superseded":'
    '[{"old_ref":"old draft.md","new_ref":"replacement.md","reason":"specific correction"}]}. '
    "List the findings delivered by this invocation. Declare every newly created draft either "
    "current or superseded. A mathematical replacement uses a NEW manuscript; preserve the old "
    "one and explicitly bind old_ref to new_ref. Only pending drafts in this stage may be "
    "superseded, never a published finding. Do not edit another phase's manifest. This is a "
    "version inventory, not a verdict; every current draft still needs independent verification. "
)


def scoped_stage(root: Path, stage: Path) -> bool:
    path = stage / "route_context.json"
    return path.is_file() and json.loads(path.read_text()).get("context_policy") == "scoped/v1"


def read_finding_manifest(root: Path, path: Path, stage: Path) -> dict:
    if path_has_symlink_component(root, path):
        raise ValueError("finding manifest must not be a symlink")
    return validate_finding_manifest(root, json.loads(path.read_text()), stage)


def validate_finding_manifest(root: Path, value: dict, stage: Path) -> dict:
    """Validate a proposed inventory before installing it or selecting drafts."""
    if (not isinstance(value, dict) or set(value) != {"current_refs", "superseded"}
            or not isinstance(value["current_refs"], list) or not isinstance(value["superseded"], list)):
        raise ValueError("finding manifest requires current_refs and superseded lists")

    def check(ref):
        if not isinstance(ref, str) or not ref:
            raise ValueError("empty finding reference")
        file = root / ref
        if (Path(ref).is_absolute() or ".." in Path(ref).parts or "\\" in ref
                or file.parent != stage / "findings" or file.suffix != ".md"
                or not file.is_file() or path_has_symlink_component(root, file)):
            raise ValueError(f"finding reference must name a manuscript in this stage: {ref}")

    for ref in value["current_refs"]:
        check(ref)
    published = set()
    for row in finding_publications(root):
        published.add(row["source_ref"])
        receipt = root / row["verification_ref"]
        if receipt.is_file():
            published.add(json.loads(receipt.read_text()).get("source_draft_ref", row["source_ref"]))
    old = set()
    for row in value["superseded"]:
        if not isinstance(row, dict) or set(row) != {"old_ref", "new_ref", "reason"}:
            raise ValueError("supersession requires old_ref, new_ref and reason")
        check(row["old_ref"]); check(row["new_ref"])
        if (row["old_ref"] == row["new_ref"] or row["old_ref"] in published
                or row["old_ref"] in value["current_refs"]
                or row["new_ref"] not in value["current_refs"] or row["old_ref"] in old
                or not isinstance(row["reason"], str) or not row["reason"].strip()):
            raise ValueError("invalid pending-draft supersession")
        old.add(row["old_ref"])
    return value


def current_finding_drafts(root: Path, stage: Path, *, freeze: bool = True,
                           manifest_value: dict | None = None) -> list[Path]:
    """Resolve replacements; freeze versions only after author format delivery."""
    findings = stage / "findings"
    if not findings.is_dir():
        return []
    checkpoint = findings / "publication.json"
    if checkpoint.is_file():
        saved = json.loads(checkpoint.read_text())
        for ref, digest in saved["versions"].items():
            file = root / ref
            if path_has_symlink_component(root, file) or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise ValueError("frozen finding inventory changed")
        return [root / ref for ref in saved["current_refs"]]
    revisions = {ref for p in findings.glob("*.format.json")
                 for ref in json.loads(p.read_text())["editorial"].get("revision_refs", ())}
    drafts = {p.relative_to(root).as_posix() for p in findings.glob("*.md")
              if p.relative_to(root).as_posix() not in revisions}
    manifests = ([stage / "finding_manifest.json"] if (stage / "finding_manifest.json").is_file() else [])
    manifests += [p for p in sorted((stage / "rci").rglob("finding_manifest.json"))
                  if (p.parent / "completed_phase.json").is_file()]
    declared, superseded = set(), {}
    values = ([validate_finding_manifest(root, manifest_value, stage)]
              if manifest_value is not None else [])
    for path in manifests:
        if manifest_value is None or path != stage / "finding_manifest.json":
            values.append(read_finding_manifest(root, path, stage))
    for value in values:
        declared.update(value["current_refs"])
        for row in value["superseded"]:
            old, new = row["old_ref"], row["new_ref"]
            if old in superseded and superseded[old] != new:
                raise ValueError("conflicting finding replacements")
            superseded[old] = new
    for old in superseded:
        seen, ref = set(), old
        while ref in superseded:
            if ref in seen:
                raise ValueError("cyclic finding replacements")
            seen.add(ref); ref = superseded[ref]
    if scoped_stage(root, stage) and drafts - declared - superseded.keys():
        raise ValueError("finding manifests do not cover all delivered drafts")
    # Older checkpoints predate inventories; their exact versions remain usable.
    selected = sorted((declared if scoped_stage(root, stage) else drafts | declared) - superseded.keys())
    versions = {ref: hashlib.sha256((root / ref).read_bytes()).hexdigest()
                for ref in sorted(drafts | {p.relative_to(root).as_posix() for p in manifests})}
    if freeze:
        _write_json_atomic(checkpoint, {"current_refs": selected, "superseded": superseded,
                                       "versions": versions, "authority": "DRAFT_SELECTION_ONLY"})
    return [root / ref for ref in selected]


def ensure_finding_inventory(*, llm, root: Path, stage: Path,
                             delivery: dict, state_ref: str) -> list[Path]:
    """Repair only an accepted author's version inventory, never its mathematics.

    One unambiguous new manuscript needs no model call. Otherwise use a bounded
    control correction with saved inputs/result; ambiguity pauses rather than
    guessing which proof version supersedes another.
    """
    from pace.runtime.output.calls import artifact_owner_metadata
    from pace.runtime.semantic import SemanticCallSpec, StructuredSemanticResult, semantic_call

    def save():
        _write_json_atomic(root / state_ref, delivery)

    repair = delivery.get("manifest_repair")
    def check_sources():
        if not repair:
            return
        for ref, digest in repair["source_hashes"].items():
            allowed = {digest}
            batch = delivery.get("format_batch", {})
            # A later saved format batch can atomically replace logical files.
            # Resume must recognize that exact transition, including a partial
            # install; the batch separately validates its saved repair copies.
            if (batch.get("materializing")
                    and batch.get("source_hashes", {}).get(ref) == digest
                    and ref in batch.get("hashes", {})):
                allowed.add(batch["hashes"][ref])
            if (path_has_symlink_component(root, root / ref)
                    or hashlib.sha256((root / ref).read_bytes()).hexdigest() not in allowed):
                raise ValueError(f"Finding inventory repair input changed: {ref}")
    check_sources()
    try:
        return current_finding_drafts(root, stage, freeze=False)
    except (OSError, ValueError, TypeError) as exc:
        error = str(exc)
    if ((stage / "findings/publication.json").exists()
            or delivery.get("handle", {}).get("transport_status") != "SUCCESS"):
        raise ValueError(error)
    manifest = stage / "finding_manifest.json"
    if path_has_symlink_component(root, manifest):
        raise ValueError("finding manifest must not be a symlink")
    if repair is None:
        drafts = sorted((stage / "findings").glob("*.md"))
        refs = sorted(set(delivery["refs"]) | {p.relative_to(root).as_posix() for p in drafts})
        if any(path_has_symlink_component(root, root / ref) or not (root / ref).is_file() for ref in refs):
            raise ValueError("Finding inventory repair requires regular local artifacts")
        repair = delivery["manifest_repair"] = {
            "source_hashes": {ref: hashlib.sha256((root / ref).read_bytes()).hexdigest() for ref in refs},
            "original_manifest": manifest.read_text() if manifest.is_file() else None,
            "error": error,
        }
        # No predecessor manifest, revision or publication exists for this one draft.
        if (len(drafts) == 1 and not manifest.exists()
                and not list(stage.rglob("finding_manifest.json"))
                and not list((stage / "findings").glob("*.format.json"))):
            proposal = {"current_refs": [drafts[0].relative_to(root).as_posix()], "superseded": []}
            current_finding_drafts(root, stage, freeze=False, manifest_value=proposal)
            repair.update(manifest=proposal, method="single_new_manuscript")
        save()

    if not repair.get("manifest"):
        if repair.get("failure"):
            raise ValueError(repair["failure"])

        def validate(payload):
            check_sources()
            if payload["action"] == "DEFER":
                return []
            try:
                current_finding_drafts(root, stage, freeze=False, manifest_value={
                    "current_refs": payload["current_refs"], "superseded": payload["superseded"]})
            except (OSError, ValueError, TypeError) as exc:
                return [{"stage": "inventory", "message": str(exc)}]
            return []

        def accept(payload):
            if payload["action"] == "DEFER":
                repair["failure"] = "Finding inventory remains ambiguous: " + payload["brief"]
            else:
                repair.update(manifest={k: payload[k] for k in ("current_refs", "superseded")},
                              method="control_correction")
            save()

        item = {"type": "string"}
        result = semantic_call(
            llm,
            "Correct only the missing or invalid finding version inventory for the saved author delivery. "
            "Do not conduct research, revise any manuscript, retrieve sources, or decide mathematical truth. "
            "Read only the listed artifacts to identify which existing drafts the author delivered and "
            "explicit replacements. Preserve prior phase inventories and all published findings. "
            "Return SUBMIT with current_refs, superseded and a short brief, or DEFER with empty lists "
            "if the intended versions are ambiguous. Never guess a supersession. All refs must be "
            "existing Markdown manuscripts directly inside this stage's findings directory. "
            "Return JSON only; do not write files. The Host installs and validates the inventory.\n"
            + json.dumps({"stage_ref": stage.relative_to(root).as_posix(),
                          "original_manifest": repair["original_manifest"], "error": repair["error"],
                          "artifacts": list(repair["source_hashes"])}, ensure_ascii=False),
            workdir=root, call_name="finding_manifest_repair",
            spec=SemanticCallSpec(role="FINDING_MANIFEST_REPAIR", authority="DRAFT_SELECTION_ONLY",
                host_schema_version="finding_manifest_repair/v1",
                semantic_schema={"type": "object", "additionalProperties": False,
                    "properties": {"action": {"type": "string", "enum": ["SUBMIT", "DEFER"]},
                        "current_refs": {"type": "array", "items": item}, "brief": item,
                        "superseded": {"type": "array", "items": {"type": "object",
                            "properties": {k: item for k in ("old_ref", "new_ref", "reason")},
                            "required": ["old_ref", "new_ref", "reason"], "additionalProperties": False}}},
                    "required": ["action", "current_refs", "superseded", "brief"]},
                validator=validate, on_accept=accept),
            input_refs=tuple(repair["source_hashes"]),
            host_metadata={**artifact_owner_metadata(root, state_ref),
                           "protected_input_refs": (*repair["source_hashes"], state_ref,
                                                    manifest.relative_to(root).as_posix()),
                           "output_refs": []},
        )
        if not isinstance(result, StructuredSemanticResult):
            repair["failure"] = "Finding inventory control correction failed; saved author output retained"
            save()
            raise ValueError(repair["failure"])
    if not repair.get("manifest"):
        raise ValueError(repair["failure"])
    # Acceptance may have been saved immediately before an interrupted install.
    check_sources()
    current_finding_drafts(root, stage, freeze=False, manifest_value=repair["manifest"])
    _write_json_atomic(manifest, repair["manifest"])
    return current_finding_drafts(root, stage, freeze=False)
