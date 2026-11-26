from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.artifacts.setup import load_setup
from pace.artifacts.workspace import (
    RunWorkspace,
    is_call_path,
    path_has_symlink_component,
    work_ref,
)
from pace.contracts.proof.binding import (
    ProofBindingError,
    load_active_target,
    resolve_verification_binding,
)
from pace.orchestrator.writing.tasks import writing_artifacts
from pace.pipeline_types import PipelineResult
from pace.reporting.terminal_report import (
    FinalReportEnvelope,
    FinalReportEnvelopeError,
)

_FINAL_PROOF_ARTIFACT_FIELDS = {
    "proof",
    "proof_manifest",
    "verification_binding",
    "verification",
    "auditor",
    "kernel",
    "source_authority",
}


from pace.orchestrator.recovery import RecoveryError, RecoveryRequest


def load_director_checkpoint(workspace: RunWorkspace) -> dict[str, Any]:
    """Read the current turn's committed continuation state."""

    from pace.agents.director.state import DirectorLedEngineError, DirectorLedState

    ref = work_ref(workspace.root, "state/director_state.json")
    path = workspace.path(ref)
    if path_has_symlink_component(workspace.root, path):
        raise RecoveryError(
            "invalid_director_checkpoint", f"{ref} must not contain a symlink"
        )
    if not path.is_file():
        raise RecoveryError("missing_director_checkpoint", f"no committed {ref}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        DirectorLedState.from_json(state)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        DirectorLedEngineError,
    ) as exc:
        raise RecoveryError(
            "invalid_director_checkpoint", f"invalid committed Director state: {exc}"
        ) from exc
    return state


def resume_turn(
    workspace: RunWorkspace,
    *,
    continue_pipeline: Callable[[RecoveryRequest], PipelineResult | Path] | None = None,
) -> PipelineResult | Path:
    """Return a terminal report or dispatch its checkpoint's exact next action.

    Recovery does not inspect later artifacts to infer progress, reconstruct an
    empty state, replay a transaction, or substitute a different action.
    """

    terminal = workspace.path(work_ref(workspace.root, "final_report.json"))
    try:
        setup = load_setup(workspace.root)
    except (OSError, ValueError, TypeError) as exc:
        raise RecoveryError("invalid_setup_checkpoint", str(exc)) from exc
    setup_failure = (
        terminal.is_file()
        and _read_json_object(terminal).get("terminal_kind") == "SETUP_FAILURE"
    )
    if setup is not None and (setup["phase"] != "READY" or setup_failure):
        if continue_pipeline is None:
            raise RecoveryError(
                "continuation_hook_unavailable",
                "initialization recovery requires a pipeline continuation",
            )
        return continue_pipeline(
            RecoveryRequest(
                workspace=workspace, checkpoint=setup, continuation_action="SETUP"
            )
        )

    proof_state = workspace.path(work_ref(workspace.root, "state/proof_state.json"))
    if proof_state.is_file():
        state = _read_json_object(proof_state)
        if state.get("engine") != "proof":
            raise RecoveryError(
                "invalid_proof_checkpoint", "invalid saved proof engine"
            )
        terminal = workspace.path(work_ref(workspace.root, "final_report.json"))
        if state.get("phase") == "FINALIZE" and terminal.is_file():
            report = _read_json_object(terminal)
            if report.get("publication_status") != "INCOMPLETE":
                return _project_terminal_result(workspace, report)
            _project_terminal_result(workspace, report)
        if continue_pipeline is None:
            raise RecoveryError(
                "continuation_hook_unavailable", "proof recovery requires a pipeline"
            )
        return continue_pipeline(RecoveryRequest(workspace, state, "PROOF"))

    from pace.agents.director.workflow import resume_research_checkpoint

    return resume_research_checkpoint(workspace, continue_pipeline)


def load_operational_checkpoint(
    workdir: Path,
    failed: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the exact active checkpoint saved by a Host-owned state."""

    from pace.agents.director.state import DirectorLedEngineError, DirectorLedState

    if failed["phase"] != "FAILED" or not failed["diagnostics_ref"]:
        return None
    diagnostic_path = Path(workdir) / failed["diagnostics_ref"]
    diagnostic = _read_json_object(diagnostic_path)
    checkpoint = diagnostic.get("resume_checkpoint")
    if checkpoint is None:
        return None
    try:
        saved = DirectorLedState.from_json(checkpoint)
        if saved.phase not in {
            "DIRECTOR",
            "PENDING_SPECIALIST",
            "PENDING_BATCH",
            "VERIFY",
        }:
            raise ValueError("operational checkpoint must be active")
        for field in (
            "target_contract_id",
            "max_proof_states",
            "proof_states_used",
            "completed_result_refs",
        ):
            if checkpoint[field] != failed[field]:
                raise ValueError(f"operational checkpoint changes {field}")
    except (DirectorLedEngineError, ValueError, TypeError, KeyError) as exc:
        raise RecoveryError("invalid_operational_checkpoint", str(exc)) from exc

    return checkpoint


def _restore_operational_checkpoint(
    workspace: RunWorkspace,
    failed: dict[str, Any],
) -> dict[str, Any] | None:
    """Reopen an explicit Host failure checkpoint, retaining its history."""

    checkpoint = load_operational_checkpoint(workspace.root, failed)
    if checkpoint is None:
        return None
    diagnostic_path = workspace.path(failed["diagnostics_ref"])
    archive_root = workspace.path(work_ref(workspace.root, "state/recovery"))
    archive_root.mkdir(parents=True, exist_ok=True)
    attempt = archive_root / f"attempt-{len(list(archive_root.iterdir())) + 1:04d}"
    attempt.mkdir()
    # Preserve the failed diagnostic before the retry can overwrite its stage.
    shutil.copyfile(diagnostic_path, attempt / "diagnostics.json")
    workspace.write_json(
        (attempt / "failed_state.json").relative_to(workspace.root).as_posix(),
        failed,
    )
    terminal = workspace.path(work_ref(workspace.root, "final_report.json"))
    if terminal.is_file():
        terminal.replace(attempt / terminal.name)
    # Restore last: an interrupted archive still leaves the failed checkpoint
    # and diagnostic available to the next explicit resume invocation.
    workspace.write_json(
        work_ref(workspace.root, "state/director_state.json"), checkpoint
    )
    return checkpoint


def _project_terminal_result(
    workspace: RunWorkspace,
    report: dict[str, Any],
) -> PipelineResult:
    try:
        envelope = FinalReportEnvelope.validate(report)
    except FinalReportEnvelopeError as exc:
        raise RecoveryError("invalid_final_report", str(exc)) from exc
    refs = envelope.artifact_refs
    _validate_terminal_report_binding(workspace, report, refs)
    visible_refs = {**refs, **writing_artifacts(workspace.root)}

    def resolve_artifact(name: str) -> Path | None:
        return _bound_artifact(workspace, visible_refs, name)

    from pace.artifacts.events import read_events

    profile = workspace.path(work_ref(workspace.root, "events.jsonl"))
    profiled = any(
        row["event_kind"] == "PROFILE_SPAN" for row in read_events(workspace.root)
    )
    return envelope.project(
        run_root=workspace.root,
        default_problem_id=workspace.problem_id,
        resolve_artifact=resolve_artifact,
        profile_file=profile if profiled else None,
        final_report_file=workspace.path(work_ref(workspace.root, "final_report.json")),
    )


def _validate_terminal_report_binding(
    workspace: RunWorkspace,
    report: dict[str, Any],
    refs: dict[str, Any],
) -> None:
    proof_binding = report["proof_binding"]
    if not isinstance(proof_binding, dict):
        raise RecoveryError(
            "invalid_final_report", "terminal proof_binding must be an object"
        )
    if not proof_binding:
        if any(refs.get(name) for name in _FINAL_PROOF_ARTIFACT_FIELDS):
            raise RecoveryError(
                "invalid_final_report", "proof evidence requires a proof_binding"
            )
        if report["result"] == "PASS":
            raise RecoveryError(
                "invalid_final_report", "PASS requires a verified proof_binding"
            )
        if not report.get("target_contract_unavailable"):
            try:
                target = load_active_target(workspace.root)
            except ProofBindingError as exc:
                raise RecoveryError("invalid_final_report", str(exc)) from exc
            if target["contract_id"] != report["target_contract_id"] or refs.get(
                "target_contract"
            ) != work_ref(workspace.root, "input/target_contract.json"):
                raise RecoveryError(
                    "invalid_final_report", "terminal target authority is inconsistent"
                )
        return
    try:
        resolved = resolve_verification_binding(
            workspace.root,
            verification_binding_ref=proof_binding["verification_binding_ref"],
            proof_id=proof_binding["proof_id"],
            proof_ref=proof_binding["proof_ref"],
            target_contract_id=report["target_contract_id"],
            verification_ref=proof_binding["verification_ref"],
        )
        if proof_binding != resolved["proof_binding"]:
            raise ProofBindingError(
                "terminal proof_binding differs from the persisted binding"
            )
        for name, reference in resolved["artifact_refs"].items():
            if refs.get(name) != reference:
                raise ProofBindingError(
                    f"terminal artifact_refs.{name} is inconsistent"
                )
        if refs.get("target_contract") != proof_binding["target_contract_ref"]:
            raise ProofBindingError("terminal target reference is inconsistent")
        if report["result"] == "PASS" and resolved["status"] != "PASS":
            raise ProofBindingError("PASS terminal report has no verified proof chain")
    except (ProofBindingError, KeyError) as exc:
        raise RecoveryError("invalid_final_report", str(exc)) from exc


def _bound_artifact(
    workspace: RunWorkspace,
    refs: dict[str, Any],
    name: str,
) -> Path | None:
    raw_ref = refs.get(name)
    if raw_ref is None or raw_ref == "":
        return None
    if not isinstance(raw_ref, str):
        raise RecoveryError(
            "invalid_final_report",
            f"terminal {name} must be a relative workspace file reference",
        )
    ref = raw_ref.strip().replace("\\", "/")
    path = Path(ref)
    raw_parts = tuple(ref.split("/"))
    if (
        not ref
        or path.is_absolute()
        or PureWindowsPath(ref).is_absolute()
        or any(part in {".", ".."} for part in raw_parts)
        or _is_private_transport_path(path)
    ):
        raise RecoveryError(
            "invalid_final_report",
            f"terminal {name} must be a public relative workspace reference: {raw_ref}",
        )
    root = workspace.root.resolve()
    lexical = root.joinpath(*path.parts)
    if path_has_symlink_component(root, lexical):
        raise RecoveryError(
            "invalid_final_report",
            f"terminal {name} must not contain a symlink: {ref}",
        )
    try:
        resolved = lexical.resolve()
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RecoveryError(
            "invalid_final_report",
            f"terminal {name} escapes the run workspace: {ref}",
        ) from exc
    if _is_private_transport_path(relative):
        raise RecoveryError(
            "invalid_final_report",
            f"terminal {name} points to a private or transport artifact: {ref}",
        )
    if not resolved.is_file():
        raise RecoveryError(
            "missing_terminal_artifact", f"terminal {name} is missing: {ref}"
        )
    return resolved


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RecoveryError(
            "invalid_final_report",
            f"JSON artifact must be a regular file, not a symlink: {path.name}",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecoveryError("missing_terminal_artifact", str(exc)) from exc
    except UnicodeError as exc:
        raise RecoveryError("invalid_final_report", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise RecoveryError("invalid_final_report", str(exc)) from exc
    if not isinstance(value, dict):
        raise RecoveryError("invalid_final_report", f"JSON object required: {path}")
    return value


def _is_private_transport_path(path: Path) -> bool:
    """Return whether a path names hidden model transport data."""

    return is_call_path(path) or path.name.startswith("private_")
