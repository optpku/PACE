"""Default verification boundary for the Director-led engine.

The Director-led loop deliberately keeps the selected specialist opaque.  A
Prover handoff is different: its file is a candidate mathematical artifact and
must cross the existing Proof Contract Auditor and Math Verifier Kernel before
the loop can report success.  This module adapts that existing verification
boundary to the small callback protocol used by :mod:`director_led`.

The callback never interprets a transport response as a proof.  It accepts only
the explicitly named deliverable file, binds that file to the canonical target,
and then persists the ordinary verification report and (when valid) the full
verification binding.
"""

from __future__ import annotations

import json
import copy
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.contracts.proof_binding import (
    load_active_target,
    resolve_proof_binding,
    resolve_verification_binding,
    resolve_verification,
    read_verification_report,
    write_proof_manifest,
)
from pace.orchestrator.verification_round import (
    VerificationContext,
    run_verification_round,
)
from pace.runtime.context_state import stage_verified_progress
from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.output.workspace import (
    RunWorkspace,
    is_call_path,
    is_private_transport_path,
    path_has_symlink_component,
    work_ref,
)


class DirectorVerifierError(RuntimeError):
    """A candidate cannot enter the Auditor/Kernel verification boundary."""


def build_default_verifier(
    *, llm: Any, workdir: Path, target_contract_id: str = "",
) -> Callable[..., dict[str, str]]:
    """Return the one production verifier: a persisted status and reference receipt."""

    root = Path(workdir).resolve()

    def verify(*, context: Mapping[str, Any], result: Mapping[str, Any],
               result_ref: str, round_index: int) -> dict[str, str]:
        return _verify_candidate(
            llm=llm, root=root, configured_target_id=target_contract_id,
            context=context, result=result, result_ref=result_ref, round_index=round_index,
        )

    return verify


def _verify_candidate(
    *, llm: Any, root: Path, configured_target_id: str,
    context: Mapping[str, Any], result: Mapping[str, Any],
    result_ref: str, round_index: int,
) -> dict[str, str]:
    """Persist independent reports once and return references to their outcome."""

    state_dir = f"{work_ref(root)}/stages/{round_index:04d}/verification"
    verification_ref = f"{state_dir}/verification_report.json"
    workspace = RunWorkspace(root=root, problem_id=root.name)
    proof_id = ""
    proof_manifest_ref = ""
    proof_ref = str(context.get("output_ref") or result.get("deliverable_ref") or "")
    target_id = configured_target_id
    auditor_ref = f"{state_dir}/proof_contract_audit.json"
    kernel_ref = f"{state_dir}/math_verifier_kernel.json"
    authority_ref = f"{state_dir}/verification_authority.json"
    report = {}
    receipt = {
        "status": "VERIFIER_ERROR", "verification_binding_ref": "",
        "verification_ref": verification_ref, "verified_progress_ref": "", "repair_context_ref": "",
    }
    try:
        state_dir = _state_dir(root, context, result_ref, round_index)
        # The canonical target is always loaded from the run root.  A
        # Director/context projection may identify it, but cannot replace it.
        _safe_output_path(root, work_ref(root, 'input/target_contract.json'), "target_contract_ref")
        target = load_active_target(root)
        projected_target = context.get("target_contract")
        if isinstance(projected_target, Mapping) and _string(
            projected_target.get("contract_id")
        ) not in {"", _string(target.get("contract_id"))}:
            raise DirectorVerifierError(
                "Director context target_contract differs from canonical target"
            )
        problem_ref, problem_text = _load_problem(root, context)
        target_id = _string(target.get("contract_id"))
        if not target_id:
            raise DirectorVerifierError("canonical target contract has no contract_id")
        context_target_id = _string(context.get("target_contract_id"))
        if context_target_id and context_target_id != target_id:
            raise DirectorVerifierError(
                "Director context target_contract_id differs from canonical target"
            )
        if configured_target_id and configured_target_id != target_id:
            raise DirectorVerifierError(
                "Director-led verifier target_contract_id differs from canonical target"
            )

        result_status = _string(result.get("status")).upper()
        if result_status != "SUCCEEDED":
            raise DirectorVerifierError(
                "only a successful Prover handoff can enter verification"
            )
        proof_ref = _candidate_ref(root, result)
        # In the production Director-led path the Host assignment commits the
        # output path before the Prover runs.  The verifier must inspect that
        # exact path, never a path inferred from another result field.
        assigned_ref = _string(context.get("output_ref")).replace("\\", "/")
        if assigned_ref:
            _ensure_relative_ref(assigned_ref, "output_ref")
            if proof_ref != assigned_ref:
                raise DirectorVerifierError(
                    "Prover deliverable_ref differs from Host assignment output_ref"
                )
        proof_text = _read_proof(root, proof_ref)
        proof_id, proof_manifest_ref = _ensure_proof_binding(
            root,
            proof_ref=proof_ref,
            target_contract_id=target_id,
        )

        _, plan_ref = _load_optional_object(
            root,
            context,
            key="theorem_decomposition_plan",
            ref_key="plan_artifact_ref",
            default_ref="proof_plan.json",
        )
        progress_ref = _string(context.get("verified_progress_ref"))
        progress_path = (
            _resolve_optional_file(root, progress_ref, "verified_progress_ref")
            if progress_ref else None
        )
        verified_progress = _read_object(progress_path) if progress_path is not None else {}
        previous_proof_md, previous_proof_id, previous_kernel = _previous_candidate(
            root, context, target_contract_id=target_id,
        )

        authority_ref = _artifact_ref(
            context.get("authority_artifact_ref")
            or context.get("verification_authority_ref")
            or f"{state_dir}/verification_authority.json",
            default=f"{state_dir}/verification_authority.json",
        )
        auditor_ref = _artifact_ref(
            context.get("auditor_artifact_ref")
            or f"{state_dir}/proof_contract_audit.json",
            default=f"{state_dir}/proof_contract_audit.json",
        )
        verification_ref = f"{state_dir}/verification_report.json"
        _ensure_relative_ref(authority_ref, "authority_artifact_ref")
        _ensure_relative_ref(auditor_ref, "auditor_artifact_ref")
        # Every Host-owned output is committed under the exact lexical run
        # path.  An in-run alias is still a different artifact identity and a
        # parent alias could redirect writes outside this run.
        for field, reference in (
            ("authority_artifact_ref", authority_ref),
            ("auditor_artifact_ref", auditor_ref),
            ("verification_ref", verification_ref),
            ("kernel_ref", f"{state_dir}/math_verifier_kernel.json"),
            ("binding_ref", f"{state_dir}/verification_binding.json"),
        ):
            _safe_output_path(root, reference, field)
        for name in (
            "proof_contract_audit.json",
            "math_verifier_kernel.json",
            "final_result.json",
            "repair_progress.json",
            "repair_context.json",
            "verified_progress.json",
            "verification_report.json",
        ):
            _safe_output_path(
                root,
                f"{state_dir}/{name}",
                f"verification artifact {name}",
            )

        # The authority projection is produced by run_verification_round.  Its
        # source input remains optional search context; no source is invented.
        artifact_refs = {
            "problem_file": problem_ref,
            "target_contract_file": _existing_ref(
                root, context, "target_contract_ref", work_ref(root, 'input/target_contract.json')
            ),
            "problem_brief_file": _existing_ref(
                root, context, "problem_brief_ref", work_ref(root, 'input/problem_brief.md')
            ),
            "proof_file": proof_ref,
            "plan_file": plan_ref,
            "source_file": authority_ref,
        }

        verification_call_ids: dict[str, str] = {}

        def audit_runner() -> dict[str, Any]:
            from pace.agents.contract_auditor import (
                run_proof_contract_auditor,
            )

            return run_proof_contract_auditor(
                llm=llm,
                workdir=root,
                call_name=f"director_led_proof_contract_auditor_round_{round_index:04d}",
                artifact_refs=artifact_refs,
                round_index=round_index,
                output_ref=auditor_ref,
                target_contract_id=str(context.get("target_contract_id") or ""),
                call_metadata=verification_call_ids,
            )

        verification_context = VerificationContext(
            proof_md=proof_text,
            problem_text=problem_text,
            source_cards={},
            proof_id=proof_id,
            target_contract_id=target_id,
            proof_search_state={
                "verified_progress": verified_progress,
                "survey_evidence_delta": _as_dict(context.get("survey_evidence_delta")),
            },
            contract_audit_runner=audit_runner,
            math_kernel_llm=llm,
            workdir=root,
            round_index=round_index,
            proof_artifact_ref=proof_ref,
            plan_artifact_ref=plan_ref,
            authority_artifact_ref=authority_ref,
            auditor_artifact_ref=auditor_ref,
            kernel_artifact_ref=f"{state_dir}/math_verifier_kernel.json",
            report_artifact_ref=verification_ref,
            previous_proof_md=previous_proof_md,
            previous_proof_id=previous_proof_id,
            previous_math_verifier_kernel=previous_kernel,
            agent_call_ids=verification_call_ids,
        )
        report = run_verification_round(verification_context)
        final = _read_object(root / report["final_result_ref"])
        if final["verification_interface_failures"]:
            report["interface_failure"] = {
                "reason_code": "verifier_interface_failed",
                "diagnostics": final["verification_interface_failures"],
            }
        else:
            blockers = final["canonical_blockers"]
            binding = {
                "binding_status": "VERIFIED" if final["result"] == "PASS" else "MATH_FAIL",
                "target_contract_id": target_id,
                "target_contract_ref": work_ref(root, "input/target_contract.json"),
                "proof_id": proof_id, "proof_ref": proof_ref,
                "proof_manifest_ref": proof_manifest_ref,
                "source_authority_ref": authority_ref, "verification_ref": verification_ref,
                "auditor_ref": auditor_ref, "kernel_ref": kernel_ref,
                "verification_inputs_ref": f"{state_dir}/verification_inputs.json",
                "blocker_ref": f"{report['final_result_ref']}#/canonical_blockers/0" if blockers else "",
            }
            binding_ref = f"{state_dir}/verification_binding.json"
            workspace.write_json(binding_ref, binding)
            resolved = resolve_verification_binding(
                root, verification_binding_ref=binding_ref,
                proof_id=proof_id, proof_ref=proof_ref,
                target_contract_id=target_id, verification_ref=verification_ref,
            )
            receipt.update(status=resolved["status"], verification_binding_ref=binding_ref)
    except Exception as exc:
        report = {
            "proof_id": proof_id, "proof_ref": proof_ref, "target_contract_id": target_id,
            **{field: ref if (root / ref).is_file() else "" for field, ref in (
                ("auditor_ref", auditor_ref), ("kernel_ref", kernel_ref),
                ("source_authority_ref", authority_ref),
            )},
            "final_result_ref": f"{state_dir}/final_result.json",
            "repair_progress_ref": "",
            "interface_failure": {
                "reason_code": "director_verifier_failed",
                "diagnostics": [{"error_type": type(exc).__name__, "message": str(exc)}],
            },
        }
        if not (root / report["final_result_ref"]).is_file():
            workspace.write_json(report["final_result_ref"], {
                "result": "FAILED", "failure_owner": "INTERFACE",
                "interface_failure": report["interface_failure"],
            })
    if receipt["status"] == "VERIFIER_ERROR":
        workspace.write_json(verification_ref, report)
    resolved = resolve_verification(
        root, receipt, target_contract_id=target_id, candidate_ref=proof_ref,
    )
    receipt["verified_progress_ref"] = stage_verified_progress(
        root, output_ref=f"{state_dir}/verified_progress.json",
        binding={
            "proof_id": proof_id, "proof_ref": proof_ref, "target_contract_id": target_id,
            "target_contract_ref": work_ref(root, "input/target_contract.json"),
            "verification_ref": verification_ref, "kernel_ref": report.get("kernel_ref", ""),
        },
        kernel=resolved["kernel"], final_result=resolved["final_result"],
    )
    receipt["repair_context_ref"] = _write_repair_context(root, verification_ref, report)
    return receipt


def _write_repair_context(root: Path, verification_ref: str, report: Mapping[str, Any]) -> str:
    """Deliver the original evidence files to the next mathematical role."""

    refs = [verification_ref, *(report.get(name, "") for name in (
        "proof_ref", "auditor_ref", "kernel_ref", "source_authority_ref",
        "final_result_ref", "repair_progress_ref",
    ))]
    reference = (Path(verification_ref).parent / "repair_context.json").as_posix()
    RunWorkspace(root=root, problem_id=root.name).write_json(reference, {
        "input_refs": list(dict.fromkeys(ref for ref in refs if ref and (root / ref).is_file())),
    })
    return reference


def _previous_candidate(
    root: Path, context: Mapping[str, Any], *, target_contract_id: str,
) -> tuple[str, str, dict[str, Any]]:
    """Read the explicit previous verification checkpoint for this target."""

    ref = str(context.get("previous_verification_ref") or "")
    if ref:
        path = _resolve_optional_file(root, ref, "previous_verification_ref")
        if path is None:
            return "", "", {}
        value = _as_dict(_read_object(path).get("verification"))
        report = read_verification_report(root, value["verification_ref"])
        kernel_ref = report.get("kernel_ref")
        kernel = _read_object(root / kernel_ref) if kernel_ref else {}
        proof_id = _string(report.get("proof_id"))
        proof_ref = _string(report.get("proof_ref"))
        if (
            report.get("target_contract_id") != target_contract_id
            or kernel.get("stage_status") not in {"PASS", "MATH_FAIL"}
            or not proof_id or not proof_ref
            or kernel.get("proof_id") != proof_id
            or kernel.get("proof_ref") != proof_ref
            or kernel.get("target_contract_id") != target_contract_id
        ):
            return "", "", {}
        prior_path = _resolve_optional_file(root, proof_ref, "previous_proof_ref")
        if prior_path is not None:
            return prior_path.read_text(encoding="utf-8"), proof_id, kernel
    return "", "", {}


def _ensure_proof_binding(
    root: Path,
    *,
    proof_ref: str,
    target_contract_id: str,
) -> tuple[str, str]:
    path = _resolve_file(root, proof_ref, "deliverable_ref")
    manifest_path = path.parent / "proof_manifest.json"
    _safe_output_path(
        root,
        manifest_path.relative_to(Path(root).resolve()).as_posix(),
        "proof_manifest_ref",
    )
    if manifest_path.is_file():
        manifest = _read_object(manifest_path)
        proof_id = _string(manifest.get("proof_id"))
        manifest_ref = manifest_path.relative_to(root).as_posix()
        resolve_proof_binding(root, proof_manifest_ref=manifest_ref, proof_id=proof_id, proof_ref=proof_ref)
        return proof_id, manifest_ref
    # The Host, rather than the Prover response, owns the proof identity.
    proof_id = f"PROOF-{uuid.uuid4().hex}"
    manifest_ref = write_proof_manifest(
        root,
        target_contract_id=target_contract_id,
        proof_id=proof_id,
        proof_ref=proof_ref,
    )
    return proof_id, manifest_ref


def _candidate_ref(root: Path, result: Mapping[str, Any]) -> str:
    # ``deliverable_ref`` is the sole current Prover handoff field.  The
    # proof-specific names were removed with the old response protocol; never
    # treat them as aliases during verification.
    if "proof_ref" in result or "candidate_proof_ref" in result:
        raise DirectorVerifierError(
            "Prover result uses an unsupported proof reference alias"
        )
    candidate = result.get("deliverable_ref")
    if not isinstance(candidate, str) or not candidate.strip():
        raise DirectorVerifierError("Prover result has no deliverable_ref")
    ref = candidate.strip().replace("\\", "/")
    _ensure_relative_ref(ref, "deliverable_ref")
    if Path(ref).name.startswith("private_"):
        raise DirectorVerifierError(
            "Prover deliverable_ref points to a private transport artifact"
        )
    _resolve_file(root, ref, "deliverable_ref")
    return ref


def _read_proof(root: Path, proof_ref: str) -> str:
    path = _resolve_file(root, proof_ref, "proof_ref")
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DirectorVerifierError(f"cannot read Prover deliverable: {proof_ref}") from exc
    if not value.strip():
        raise DirectorVerifierError("Prover deliverable is empty")
    return value


def _load_problem(root: Path, context: Mapping[str, Any]) -> tuple[str, str]:
    ref = _artifact_ref(context.get("problem_ref"), default=work_ref(root, 'input/problem.md'))
    if ref != work_ref(root, 'input/problem.md'):
        raise DirectorVerifierError(
            "problem_ref must be the canonical problem.md artifact"
        )
    _ensure_relative_ref(ref, "problem_ref")
    path = _resolve_file(root, ref, "problem_ref")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DirectorVerifierError("cannot read canonical problem statement") from exc
    if not text.strip():
        raise DirectorVerifierError("canonical problem statement is empty")
    return ref, text


def _load_optional_object(
    root: Path,
    context: Mapping[str, Any],
    *,
    key: str,
    ref_key: str,
    default_ref: str,
) -> tuple[dict[str, Any], str]:
    value = context.get(key)
    if isinstance(value, Mapping):
        inline_ref = _string(context.get(ref_key))
        if inline_ref:
            _ensure_relative_ref(inline_ref, ref_key)
        return copy.deepcopy(dict(value)), inline_ref
    ref = _artifact_ref(context.get(ref_key), default=default_ref)
    if not ref:
        return {}, ""
    path = _resolve_optional_file(root, ref, ref_key)
    if path is None:
        return {}, ""
    return _read_object(path), ref


def _existing_ref(
    root: Path,
    context: Mapping[str, Any],
    key: str,
    default: str,
) -> str:
    ref = _artifact_ref(context.get(key), default=default)
    if not ref:
        return ""
    return ref if _resolve_optional_file(root, ref, key) is not None else ""


def _state_dir(root: Path, context: Mapping[str, Any], result_ref: str, round_index: int) -> str:
    expected = f"{work_ref(root, 'stages')}/{int(round_index):04d}/verification"
    explicit = _string(context.get("verification_dir")).replace("\\", "/")
    if explicit:
        _ensure_relative_ref(explicit, "verification_dir")
        normalized = explicit.strip("/")
        if normalized != expected:
            raise DirectorVerifierError(
                "verification_dir must be the current Director round directory"
            )
        return expected
    parent = Path(result_ref.replace("\\", "/")).parent.as_posix()
    if parent not in {"", "."} and parent != ".." and ".." not in Path(parent).parts:
        if parent != str(Path(expected).parent):
            raise DirectorVerifierError(
                "result_ref must belong to the current Director round"
            )
        return expected
    return expected


def _artifact_ref(value: Any, *, default: str) -> str:
    raw = _string(value).replace("\\", "/").strip()
    return raw or default


def _ensure_relative_ref(value: str, field: str) -> None:
    path = Path(str(value or ""))
    if (
        path.is_absolute()
        or PureWindowsPath(str(value or "")).is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or is_call_path(path)
        or path.name.startswith("private_")
    ):
        raise DirectorVerifierError(f"{field} must be a public relative workspace reference")


def _resolve_file(root: Path, ref: str, field: str) -> Path:
    normalized = str(ref or "").strip().replace("\\", "/")
    _ensure_relative_ref(normalized, field)
    root_path = Path(root).resolve()
    path = _safe_output_path(root_path, normalized, field)
    try:
        relative = path.relative_to(root_path)
    except ValueError as exc:
        raise DirectorVerifierError(f"{field} escapes the run workspace") from exc
    if is_private_transport_path(relative):
        raise DirectorVerifierError(
            f"{field} points to a private or transport artifact"
        )
    if not path.is_file():
        raise DirectorVerifierError(f"{field} does not name a file: {normalized}")
    return path


def _resolve_optional_file(root: Path, ref: str, field: str) -> Path | None:
    """Resolve an optional artifact with the same real-path boundary as required files."""

    normalized = str(ref or "").strip().replace("\\", "/")
    _ensure_relative_ref(normalized, field)
    root_path = Path(root).resolve()
    path = _safe_output_path(root_path, normalized, field)
    try:
        relative = path.relative_to(root_path)
    except ValueError as exc:
        raise DirectorVerifierError(f"{field} escapes the run workspace") from exc
    if is_private_transport_path(relative):
        raise DirectorVerifierError(
            f"{field} points to a private or transport artifact"
        )
    return path if path.is_file() else None


def _safe_output_path(root: Path, ref: str, field: str) -> Path:
    """Resolve one workspace path without following a lexical symlink."""

    normalized = str(ref or "").strip().replace("\\", "/")
    _ensure_relative_ref(normalized, field)
    root_path = Path(root).resolve()
    lexical = root_path / normalized
    if path_has_symlink_component(root_path, lexical):
        raise DirectorVerifierError(
            f"{field} must not contain a symlink: {normalized}"
        )
    try:
        resolved = lexical.resolve()
        relative = resolved.relative_to(root_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DirectorVerifierError(
            f"{field} escapes the run workspace: {normalized}"
        ) from exc
    if is_private_transport_path(relative):
        raise DirectorVerifierError(
            f"{field} points to a private or transport artifact"
        )
    return resolved


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DirectorVerifierError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise DirectorVerifierError(f"JSON artifact must be an object: {path}")
    return value


def _string(value: Any) -> str:
    """Read an optional string without changing a caller-owned type."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise DirectorVerifierError("verifier handoff string fields must be strings")
    return value.strip()


__all__ = [
    "DirectorVerifierError",
    "build_default_verifier",
]
