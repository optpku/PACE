"""Canonical proof bindings and artifact resolvers.

The proof session is the owner of a canonical proof attempt.  This module
follows explicit manifest and report references. Execution, publication and
recovery share the same literal-target and independent-verdict checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pace.contracts.target import (
    target_contract_text,
    validate_target_contract,
)
from pace.contracts.verification import decide_final_result
from pace.runtime.output.workspace import work_ref, _write_json_atomic, path_has_symlink_component

MISSING_AUTHORITY_ARTIFACT = "MISSING_AUTHORITY_ARTIFACT"
MALFORMED_AUTHORITY_ARTIFACT = "MALFORMED_AUTHORITY_ARTIFACT"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
REF_MISMATCH = "REF_MISMATCH"
PROOF_BINDING_ERROR_CODES = frozenset(
    {
        MISSING_AUTHORITY_ARTIFACT,
        MALFORMED_AUTHORITY_ARTIFACT,
        IDENTITY_MISMATCH,
        REF_MISMATCH,
    }
)


class ProofBindingError(ValueError):
    """Raised when a canonical proof binding cannot be resolved safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = MALFORMED_AUTHORITY_ARTIFACT,
    ) -> None:
        if code not in PROOF_BINDING_ERROR_CODES:
            raise ValueError(f"unsupported proof binding error code: {code}")
        self.code = code
        super().__init__(message)


def load_active_target(root: Path) -> dict[str, Any]:
    """Load the one canonical target contract for a run.

    ``target_contract.json`` is the only source of the active target identity.
    A caller-supplied target id may be checked against this value, but is never
    used to create a replacement target or to fill an absent id.
    """

    root_path = Path(root).resolve()
    target_path = root_path / work_ref(root, 'input/target_contract.json')
    if path_has_symlink_component(root_path, target_path):
        raise ProofBindingError(
            "canonical target contract must not contain a symlink",
            code=REF_MISMATCH,
        )
    value = _read_object(target_path, "canonical target contract")
    problem_path = root_path / work_ref(root, 'input/problem.md')
    if path_has_symlink_component(root_path, problem_path):
        raise ProofBindingError(
            "canonical problem statement must not contain a symlink",
            code=REF_MISMATCH,
        )
    try:
        problem_text = problem_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProofBindingError(
            f"unable to read canonical problem statement: {problem_path}",
            code=MISSING_AUTHORITY_ARTIFACT,
        ) from exc
    except OSError as exc:
        raise ProofBindingError(
            f"unable to read canonical problem statement: {problem_path}",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        ) from exc
    if not problem_text.strip():
        raise ProofBindingError(
            "canonical problem statement is empty",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        )
    _, errors = validate_target_contract(value)
    if errors:
        raise ProofBindingError(
            "canonical target contract is invalid: " + ", ".join(errors),
            code=(
                IDENTITY_MISMATCH
                if errors == ["contract_id"]
                else MALFORMED_AUTHORITY_ARTIFACT
            ),
        )
    contract_id = str(value.get("contract_id") or "").strip()
    if not contract_id:
        raise ProofBindingError(
            "canonical target contract lacks contract_id",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        )
    if not target_contract_text(value):
        raise ProofBindingError(
            "canonical target contract lacks a literal target",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        )
    # Optional mathematical references remain prompt-owned context.  Only the
    # canonical target file, literal clauses, and contract identity are needed
    # here; validating every arbitrary ``*_ref`` field would make unrelated
    # evidence artifacts a Host gate.
    return value


def write_proof_manifest(
    root: Path,
    *,
    target_contract_id: str,
    proof_id: str,
    proof_ref: str,
) -> str:
    """Persist and validate an immutable manifest for one canonical proof.

    The returned manifest reference is relative to ``root`` and is the sole
    lookup anchor for the proof binding. Mathematical contract/artifact
    content is owned by the prompt and verifier, not this manifest.
    """

    canonical_target = load_active_target(root)
    expected_target_id = str(canonical_target.get("contract_id") or "").strip()
    target_id = str(target_contract_id or "").strip()
    if target_id != expected_target_id:
        raise ProofBindingError(
            "proof manifest target_contract_id does not match canonical target",
            code=IDENTITY_MISMATCH,
        )
    proof_identifier = str(proof_id or "").strip()
    if not proof_identifier:
        raise ProofBindingError(
            "proof manifest requires proof_id",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        )
    proof_reference = str(proof_ref or "").strip()
    proof_path = _resolve_relative_path(root, proof_reference, "proof_ref")
    if not proof_path.is_file():
        raise ProofBindingError(
            f"proof_ref does not point to a file: {proof_reference}",
            code=MISSING_AUTHORITY_ARTIFACT,
        )

    manifest_path = proof_path.parent / "proof_manifest.json"
    manifest_ref = str(manifest_path.relative_to(Path(root).resolve()))
    manifest = {
        "target_contract_id": target_id,
        "target_contract_ref": work_ref(root, 'input/target_contract.json'),
        "proof_id": proof_identifier,
        "proof_ref": proof_reference,
    }
    if manifest_path.exists():
        existing = _read_object(manifest_path, "proof manifest")
        if existing != manifest:
            raise ProofBindingError(
                "canonical proof manifest already exists with different identity: "
                + manifest_ref,
                code=IDENTITY_MISMATCH,
            )
    else:
        _write_json_atomic(manifest_path, manifest)
    return manifest_ref


def resolve_proof_binding(
    root: Path,
    *,
    proof_manifest_ref: str,
    proof_id: str,
    proof_ref: str,
) -> dict[str, Any]:
    """Read the explicitly named manifest and bind it to the literal target."""

    target = load_active_target(root)
    manifest = _read_object(
        _resolve_relative_path(root, proof_manifest_ref, "proof_manifest_ref"),
        "proof manifest",
    )
    for field, expected in (
        ("proof_id", proof_id),
        ("proof_ref", proof_ref),
        ("target_contract_id", target["contract_id"]),
        ("target_contract_ref", work_ref(root, "input/target_contract.json")),
    ):
        if not expected or manifest.get(field) != expected:
            raise ProofBindingError(
                f"proof manifest {field} is inconsistent", code=_binding_mismatch_code(field),
            )
    proof_path = _resolve_relative_path(root, proof_ref, "proof_ref")
    if not proof_path.is_file():
        raise ProofBindingError("proof_ref does not point to a file", code=MISSING_AUTHORITY_ARTIFACT)
    return {**manifest, "proof_manifest_ref": proof_manifest_ref}


def resolve_verification_binding(
    root: Path,
    *,
    verification_binding_ref: str,
    proof_id: str = "",
    proof_ref: str = "",
    target_contract_id: str = "",
    verification_ref: str = "",
) -> dict[str, Any]:
    """Use one persisted identity and acceptance check in every lifecycle phase."""

    binding = _read_object(
        _resolve_relative_path(root, verification_binding_ref, "verification_binding_ref"),
        "verification binding",
    )
    if binding.get("target_contract_ref") != work_ref(root, "input/target_contract.json"):
        raise ProofBindingError("verification binding target_contract_ref is not canonical", code=REF_MISMATCH)
    for field, requested in (
        ("proof_id", proof_id), ("proof_ref", proof_ref),
        ("target_contract_id", target_contract_id), ("verification_ref", verification_ref),
    ):
        if requested and binding.get(field) != requested:
            raise ProofBindingError(
                f"verification binding {field} is inconsistent", code=_binding_mismatch_code(field),
            )
    manifest = resolve_proof_binding(
        root, proof_manifest_ref=binding["proof_manifest_ref"],
        proof_id=binding["proof_id"], proof_ref=binding["proof_ref"],
    )
    if binding.get("target_contract_id") != manifest["target_contract_id"]:
        raise ProofBindingError("verification binding target_contract_id is inconsistent", code=IDENTITY_MISMATCH)
    verification = read_verification_report(root, binding["verification_ref"])
    for field in ("proof_id", "proof_ref", "target_contract_id", "source_authority_ref", "auditor_ref", "kernel_ref"):
        if verification.get(field) != binding.get(field):
            raise ProofBindingError(
                f"verification report {field} is inconsistent", code=_binding_mismatch_code(field),
            )
    auditor = _read_object(_resolve_relative_path(root, binding["auditor_ref"], "auditor_ref"), "Auditor report")
    kernel = _read_object(_resolve_relative_path(root, binding["kernel_ref"], "kernel_ref"), "Kernel report")
    _validate_verifier_bindings(
        auditor=auditor, kernel=kernel, proof_id=binding["proof_id"],
        proof_ref=binding["proof_ref"], target_contract_id=binding["target_contract_id"],
    )
    final_result = _read_object(
        _resolve_relative_path(root, verification["final_result_ref"], "final_result_ref"), "final result",
    )
    expected = decide_final_result(auditor=auditor, kernel=kernel)
    if expected["verification_interface_failures"] or expected["failure_owner"] == "INTERFACE":
        raise ProofBindingError("verifier interface failure cannot form a mathematical binding")
    if final_result != expected:
        raise ProofBindingError("persisted final result disagrees with Auditor and Kernel")
    status = expected["result"]
    binding_status = "VERIFIED" if status == "PASS" else "MATH_FAIL"
    if binding.get("binding_status") != binding_status:
        raise ProofBindingError("verification binding disagrees with the explicit verdicts")
    blockers = final_result["canonical_blockers"]
    blocker_ref = binding.get("blocker_ref", "")
    if bool(blockers) != bool(blocker_ref):
        raise ProofBindingError("verification binding blocker_ref disagrees with delivered evidence")
    if blocker_ref and _resolve_json_pointer(root, blocker_ref) != blockers[0]:
        raise ProofBindingError("blocker_ref does not resolve to the canonical blocker", code=REF_MISMATCH)
    source = _read_object(
        _resolve_relative_path(root, binding["source_authority_ref"], "source_authority_ref"), "source authority",
    )
    input_ref = binding.get("verification_inputs_ref") or str(Path(binding["verification_ref"]).with_name("verification_inputs.json"))
    if binding.get("verification_inputs_ref") or (Path(root) / input_ref).is_file():
        frozen = _read_object(_resolve_relative_path(root, input_ref, "verification_inputs_ref"), "verification input snapshot")
        proof_text = _resolve_relative_path(root, binding["proof_ref"], "proof_ref").read_text(encoding="utf-8")
        if (frozen.get("proof_id") != binding["proof_id"] or frozen.get("proof_ref") != binding["proof_ref"]
                or frozen.get("proof_text") != proof_text or frozen.get("target") != load_active_target(root)
                or frozen.get("authority") != source):
            raise ProofBindingError("current proof, target or dependencies differ from the verified input version", code=IDENTITY_MISMATCH)
    proof_binding = {
        name: binding[name] for name in (
            "proof_id", "proof_ref", "proof_manifest_ref", "target_contract_id",
            "target_contract_ref", "verification_ref", "source_authority_ref",
        )
    }
    proof_binding["verification_binding_ref"] = verification_binding_ref
    artifact_refs = {
        "proof": binding["proof_ref"], "proof_manifest": binding["proof_manifest_ref"],
        "verification_binding": verification_binding_ref, "verification": binding["verification_ref"],
        "auditor": binding["auditor_ref"], "kernel": binding["kernel_ref"],
        "source_authority": binding["source_authority_ref"],
    }
    return {
        "status": status, "binding": binding, "manifest": manifest,
        "verification_report": verification, "final_result": final_result,
        "auditor": auditor, "kernel": kernel, "source_authority": source,
        "proof_binding": proof_binding, "artifact_refs": artifact_refs,
    }


def read_verification_report(root: Path, reference: str) -> dict[str, Any]:
    """Read the Host's reference index, never a second copy of the reports."""

    return _read_object(_resolve_relative_path(root, reference, "verification_ref"), "verification report")


def resolve_verification(
    root: Path,
    receipt: dict[str, Any],
    *,
    target_contract_id: str,
    candidate_ref: str,
) -> dict[str, Any]:
    """Read the single verifier delivery format and its persisted evidence."""

    status = receipt["status"]
    if status not in {"PASS", "FAILED", "VERIFIER_ERROR"}:
        raise ProofBindingError("verifier delivery has an invalid status")
    if status != "VERIFIER_ERROR":
        resolved = resolve_verification_binding(
            root, verification_binding_ref=receipt["verification_binding_ref"],
            proof_ref=candidate_ref, target_contract_id=target_contract_id,
            verification_ref=receipt["verification_ref"],
        )
        if resolved["status"] != status:
            raise ProofBindingError("verifier delivery disagrees with the persisted binding")
        return resolved
    if receipt["verification_binding_ref"]:
        raise ProofBindingError("an interface error cannot claim a mathematical binding")
    report = read_verification_report(root, receipt["verification_ref"])
    for field, expected in (("target_contract_id", target_contract_id), ("proof_ref", candidate_ref)):
        if report.get(field) and report[field] != expected:
            raise ProofBindingError(f"failed verifier {field} is inconsistent", code=_binding_mismatch_code(field))
    reports = {}
    for role in ("auditor", "kernel"):
        reference = report.get(f"{role}_ref")
        reports[role] = _read_object(_resolve_relative_path(root, reference, f"{role}_ref"), role) if reference else {}
    if reports["kernel"]:
        _validate_verifier_bindings(
            auditor=reports["auditor"], kernel=reports["kernel"],
            proof_id=report["proof_id"], proof_ref=report["proof_ref"],
            target_contract_id=report["target_contract_id"],
        )
    final = _read_object(
        _resolve_relative_path(root, report["final_result_ref"], "final_result_ref"), "final result",
    )
    return {"status": status, "verification_report": report, "final_result": final, **reports}


def _validate_verifier_bindings(
    *,
    auditor: dict[str, Any],
    kernel: dict[str, Any],
    proof_id: str,
    proof_ref: str,
    target_contract_id: str,
) -> None:
    target_identity = auditor.get("target_identity")
    artifact_identity = auditor.get("artifact_identity")
    if not isinstance(target_identity, dict) or not isinstance(artifact_identity, dict):
        raise ProofBindingError(
            "Auditor report binding metadata is missing",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        )
    for field, observed, expected in (
        ("Auditor target proof_id", target_identity.get("proof_id"), proof_id),
        (
            "Auditor target_contract_id",
            target_identity.get("target_contract_id"),
            target_contract_id,
        ),
        ("Auditor artifact proof_id", artifact_identity.get("proof_id"), proof_id),
        ("Auditor artifact proof_ref", artifact_identity.get("proof_ref"), proof_ref),
        ("Kernel proof_id", kernel.get("proof_id"), proof_id),
        ("Kernel proof_ref", kernel.get("proof_ref"), proof_ref),
        (
            "Kernel target_contract_id",
            kernel.get("target_contract_id"),
            target_contract_id,
        ),
    ):
        if str(observed or "") != str(expected or ""):
            raise ProofBindingError(
                f"{field} is inconsistent",
                code=(REF_MISMATCH if field.endswith("ref") else IDENTITY_MISMATCH),
            )


def _binding_mismatch_code(field: str) -> str:
    return REF_MISMATCH if str(field).endswith("ref") else IDENTITY_MISMATCH


def _resolve_json_pointer(root: Path, reference: str) -> Any:
    artifact_ref, separator, pointer = str(reference or "").partition("#")
    if not separator or not pointer.startswith("/"):
        raise ProofBindingError(
            "blocker_ref must include a JSON pointer",
            code=REF_MISMATCH,
        )
    value: Any = _read_object(
        _resolve_relative_path(root, artifact_ref, "blocker_ref"),
        "canonical blocker artifact",
    )
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ProofBindingError(
                "blocker_ref JSON pointer does not resolve",
                code=REF_MISMATCH,
            )
    return value


def _resolve_relative_path(root: Path, reference: str, field: str) -> Path:
    root_path = Path(root).resolve()
    value = str(reference or "").strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ProofBindingError(
            f"{field} must be a non-empty relative path",
            code=REF_MISMATCH,
        )
    lexical = root_path / path
    if path_has_symlink_component(root_path, lexical):
        raise ProofBindingError(
            f"{field} must not contain a symlink",
            code=REF_MISMATCH,
        )
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise ProofBindingError(
            f"{field} escapes the run root",
            code=REF_MISMATCH,
        ) from exc
    return resolved


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProofBindingError(
            f"unable to read {label}: {path}",
            code=MISSING_AUTHORITY_ARTIFACT,
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofBindingError(
            f"unable to read {label}: {path}",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        ) from exc
    if not isinstance(value, dict):
        raise ProofBindingError(
            f"{label} must be a JSON object: {path}",
            code=MALFORMED_AUTHORITY_ARTIFACT,
        )
    return value
