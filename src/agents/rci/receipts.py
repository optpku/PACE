"""Bind scheduled actions to the input and output files used by the Host."""

from pathlib import Path

from .roles import AUTHORS


def output_names(phase):
    if phase == "CONJECTURE_EXPLORER":
        return ("change.md", "candidate.md", "probe_task.md")
    if phase == "MECHANISM_EXPLORER":
        return ("change.md", "mechanism.md")
    return ("review.md",)


def host_receipt_fields(root, directory, phase, payload, routing):
    action = payload.get("action")
    references = {}

    def own(name):
        ref = (directory / name).relative_to(root).as_posix()
        references[Path(name).stem + "_ref"] = ref
        return ref

    if phase in AUTHORS:
        own("change.md")
    else:
        own("review.md")
    if action == "PROPOSE_CONJECTURE":
        own("candidate.md")
    elif action in {"PROBE_PROOF", "PROBE_REFUTATION"}:
        own("probe_task.md")
    elif action == "PROPOSE_MECHANISM":
        ref = own("mechanism.md")
        base = routing.get("base_mechanism_ref") or payload.get("base_mechanism_ref")
        if base:
            references["base_mechanism_ref"] = base
        references["mechanism_id"] = routing.get("mechanism_ids", {}).get(base, ref)
    if "candidate_ref" in routing:
        key = "candidate_ref" if phase in AUTHORS else "reviewed_candidate_ref"
        references[key] = routing["candidate_ref"]
    if "mechanism_ref" in routing:
        references["reviewed_mechanism_ref"] = routing["mechanism_ref"]
    return references


def validate_receipt(root, directory, phase, payload, *, inputs, routing):
    action = payload.get("action")
    issues = []

    def error(key, message, reason="phase_receipt_invalid"):
        issues.append(
            {"json_pointer": f"/{key}", "message": message, "reason_code": reason}
        )

    artifacts = set(output_names(phase))
    for key, ref in host_receipt_fields(
        root, directory, phase, payload, routing
    ).items():
        path = root / ref
        if (
            path.parent == directory
            and path.name in artifacts
            and (not path.is_file() or not path.read_bytes().strip())
        ):
            error(
                key,
                f"Write the mathematical manuscript at {ref}.",
                "phase_artifact_missing",
            )

    if phase == "CONJECTURE_EXPLORER":
        selected = payload.get("mechanism_ref")
        if (
            action == "REVIEW_MECHANISM"
            or (action == "EXPLORE_MECHANISM" and selected is not None)
        ) and selected not in inputs.get("MECHANISM", ()):
            error("mechanism_ref", "Select an exact MECHANISM input file.")
    elif action == "PROPOSE_MECHANISM":
        base = payload.get("base_mechanism_ref")
        assigned = routing.get("base_mechanism_ref")
        if base is not None and (
            base not in inputs.get("MECHANISM", ()) or (assigned and base != assigned)
        ):
            error(
                "base_mechanism_ref",
                "Select the mechanism being repaired from the supplied files.",
            )
    elif action == "REUSE":
        if payload.get("existing_candidate_ref") not in inputs.get(
            "RELATED_CANDIDATE", ()
        ):
            error(
                "existing_candidate_ref",
                "Select an exact RELATED_CANDIDATE input file.",
            )
    return issues
