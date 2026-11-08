"""Terminal publication for the Director-led execution engine.

Publish the verified winner, or the last committed candidate on failure.
Only independent Auditor/Kernel bindings authorize PASS. Model response text
and incomplete output files are never promoted to candidate proofs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Any

from pace.contracts.problem.loader import Problem
from pace.contracts.proof.binding import (
    ProofBindingError,
    load_active_target,
    resolve_verification,
)
from pace.orchestrator.director_led import (
    DirectorLedEngineError,
    DirectorLedRunResult,
    DirectorLedState,
    _validate_verification_checkpoint,
)
from pace.pipeline_types import PipelineResult
from pace.reporting.terminal_report import (
    FINAL_REPORT_SCHEMA_VERSION,
    FinalReportEnvelope,
)
from pace.runtime.output.events import write_call_summary
from pace.runtime.output.profiling import ProfileRecorder
from pace.runtime.output.results import finding_entries, publish_finding
from pace.runtime.output.workspace import RunWorkspace, is_call_path, work_ref, path_has_symlink_component
from pace.runtime.output.delivery_pause import delivery_pause

logger = logging.getLogger(__name__)




class DirectorLedFinalizationError(ValueError):
    """A Director-led terminal artifact chain is not publishable."""


def finalize_director_led(
    workspace: RunWorkspace,
    problem: Problem,
    run_result: DirectorLedRunResult | DirectorLedState | Mapping[str, Any] | Any,
    profiler: ProfileRecorder | None = None,
    reason_code: str = "",
    defer_delivery: bool = False,
) -> PipelineResult:
    """Persist and project one Director-led terminal result.

    ``run_result`` is normally :class:`DirectorLedRunResult`.  A state object
    or a JSON-like mapping is accepted as a narrow convenience for recovery
    callers and focused tests.  The function is intentionally usable with
    positional arguments as well as keyword arguments.

    A PASS is possible only when the committed state says ``FINALIZE/PASS``
    and the verifier's explicit references resolve through the canonical proof
    manifest, verification binding, Auditor, and Kernel.  Every other state
    is reported as FAILED.  In particular, an incomplete candidate chain is
    never copied into the reserved proof artifact fields.
    """

    ws = _coerce_workspace(workspace)
    root = ws.root
    root.mkdir(parents=True, exist_ok=True)

    problem_error = _ensure_problem_file(ws, problem)
    state, state_ref, state_error = _coerce_state(ws, run_result)
    target, target_error = _load_target(root)
    target_available = bool(target)
    canonical_target_id = str(target.get("contract_id") or "").strip()
    state_target_id = str(state.target_contract_id or "").strip() if state is not None else ""
    # The report may still expose the canonical ID (or the committed state ID
    # when the canonical target could not be loaded), but only an explicit,
    # non-empty state ID equal to the canonical ID can authorize PASS.
    target_id = canonical_target_id or state_target_id
    target_identity_valid = bool(
        state_target_id
        and target_available
        and canonical_target_id
        and state_target_id == canonical_target_id
    )
    if state is not None and not state_target_id:
        target_error = target_error or (
            "Director-led state target_contract_id is required"
        )
    elif target_available and state_target_id != canonical_target_id:
        target_error = target_error or (
            "Director-led state target_contract_id differs from canonical target"
        )

    artifact_refs = _context_refs(ws, state, state_ref, target_available)
    candidate_state = _last_candidate_state(ws, state)
    if candidate_state is not None and _existing_safe_ref(root, candidate_state.candidate_ref):
        artifact_refs["candidate"] = candidate_state.candidate_ref
        if _existing_safe_ref(root, candidate_state.last_verification_ref):
            artifact_refs["candidate_verification"] = candidate_state.last_verification_ref
    chain: dict[str, Any] = {}
    chain_error = ""
    if (
        state is not None
        and not state_error
        and not target_error
        and target_identity_valid
        and candidate_state is not None
    ):
        try:
            chain = _resolve_chain(
                ws,
                candidate_state,
                canonical_target=target,
                target_id=target_id,
            )
        except (DirectorLedFinalizationError, ProofBindingError, OSError, ValueError) as exc:
            chain_error = str(exc) or type(exc).__name__

    verification_failure = chain.get("verification_failure", {})
    requested_result = (
        str(state.terminal_result or "").upper() if state is not None else ""
    )
    state_phase = str(state.phase or "") if state is not None else ""
    if state is not None and state_phase not in {"FINALIZE", "STOPPED", "FAILED"}:
        raise DirectorLedFinalizationError(
            f"Director-led state is not terminal: {state_phase}"
        )
    chain_is_pass = bool(chain.get("pass_chain"))
    chain_is_complete = bool(chain.get("proof_binding"))
    terminal_pass = (
        requested_result == "PASS"
        and state_phase == "FINALIZE"
        and target_identity_valid
        and chain_is_pass
        and not problem_error
        and not state_error
    )
    terminal_result = "PASS" if terminal_pass else "FAILED"

    # A malformed target/state or a broken PASS chain is an operational
    # failure.  Ordinary Director STOP and exhausted mathematical search are
    # completed mathematical failures and remain distinguishable in the
    # report.
    operational_error = bool(
        problem_error
        or state_error
        or chain_error
        or verification_failure
        or (state_phase == "FAILED")
        or (requested_result == "PASS" and not terminal_pass)
        or (target_error and requested_result == "PASS")
    )
    if terminal_pass:
        owner = "PROOF"
    elif operational_error:
        owner = "INTERFACE"
    elif state_phase == "STOPPED":
        owner = "DIRECTOR"
    elif chain_is_complete:
        owner = "PROOF"
    else:
        owner = "DIRECTOR"

    final_reason = str(
        reason_code or verification_failure.get("reason_code") or ""
    ).strip() or _default_reason(
        state=state,
        terminal_result=terminal_result,
        chain_error=chain_error,
        operational_error=operational_error,
    )

    # Reserved proof-chain fields are added only when every resolver check
    # succeeds.  A failed or interrupted handoff is represented by ordinary
    # Director context refs instead of a misleading half-binding.
    if chain_is_complete:
        artifact_refs.update(chain["artifact_refs"])
        proof_binding = dict(chain["proof_binding"])
    else:
        proof_binding = {}

    diagnostic_ref = ""
    diagnostic_persistence_error = ""
    diagnostics = [
        value
        for value in (problem_error, state_error, target_error, chain_error)
        if str(value or "").strip()
    ]
    if state_phase == "FAILED" and _existing_safe_ref(root, state.diagnostics_ref):
        failure = json.loads((root / state.diagnostics_ref).read_text(encoding="utf-8"))
        if failure.get("message"):
            diagnostics.append(str(failure["message"]))
    if diagnostics:
        diagnostic_ref = work_ref(workspace.root, 'state/diagnostics/director_led_finalization.json')
        try:
            _safe_write_json(
                ws,
                diagnostic_ref,
                {
                    "schema_version": "operational_diagnostic/v1",
                    "stage": "DIRECTOR_LED_FINALIZATION",
                    "requested_result": requested_result,
                    "phase": state_phase,
                    "messages": diagnostics,
                    "state_ref": state_ref,
                },
                "operational diagnostic",
            )
        except Exception as exc:
            # Diagnostics are observability only.  Keep the in-memory reasons
            # in the terminal report even when their optional file cannot be
            # safely persisted.
            diagnostic_ref = ""
            diagnostic_persistence_error = str(exc) or type(exc).__name__
        else:
            artifact_refs["operational_diagnostic"] = diagnostic_ref

    execution_policy = json.loads((root / work_ref(root, "state/research_execution_policy.json")).read_text())
    report: dict[str, Any] = {
        "schema_version": FINAL_REPORT_SCHEMA_VERSION,
        "problem_id": str(problem.problem_id or ws.problem_id),
        "run_status": (
            "completed_with_operational_error" if operational_error else "completed"
        ),
        "result": terminal_result,
        # Preserve the verifier's target-relative answer polarity.  The
        # Director never chooses this value; it is projected only from the
        # resolved canonical final_result after the Auditor/Kernel chain has
        # been checked.
        "answer_mode": str(chain.get("answer_mode") or ""),
        "reason_code": final_reason,
        "termination_owner": owner,
        "mathematical_status": "VERIFIED" if terminal_pass else "OPEN",
        # Outline delivery never changes literal-target acceptance.
        "operational_failure_owner": owner if operational_error else "",
        "operational_reason_code": (
            final_reason
            if verification_failure
            else ("director_led_finalization_failed" if operational_error else "")
        ),
        "publication_status": "NO_PUBLICATION",
        "terminal_kind": "DIRECTOR_LED",
        "target_contract_id": target_id,
        "target_contract_unavailable": not target_available,
        "proof_binding": proof_binding,
        "research_binding": {},
        "literal_resolution_ref": "",
        "routing_decision_ref": "",
        "orientation_routing_requested": False,
        "execution_mode": "research",
        "mode_source": execution_policy["mode_source"],
        "mode_reason": execution_policy["mode_reason"],
        "artifact_refs": artifact_refs,
        "final_provenance_valid": bool(chain_is_complete),
        "director_state": {
            "phase": state_phase,
            "round_index": state.round_index if state is not None else 0,
            "max_proof_states": state.max_proof_states if state is not None else 0,
            "proof_states_used": state.proof_states_used if state is not None else 0,
            "stop_reason": state.stop_reason if state is not None else "",
        },
    }
    if diagnostics:
        report["operational_diagnostics"] = {
            **({"diagnostic_ref": diagnostic_ref} if diagnostic_ref else {}),
            "messages": diagnostics,
            **(
                {"persistence_error": diagnostic_persistence_error}
                if diagnostic_persistence_error
                else {}
            ),
        }
    if verification_failure:
        from pace.runtime.semantic import diagnostic_messages

        report["operational_diagnostics"] = {
            **report.get("operational_diagnostics", {}),
            "messages": list(dict.fromkeys([
                *report.get("operational_diagnostics", {}).get("messages", []),
                *diagnostic_messages(verification_failure["verification_interface_failures"]),
            ])),
            "verification_ref": candidate_state.last_verification_ref,
            "diagnostics": verification_failure.get("diagnostics", []),
            "verification_interface_failures": verification_failure["verification_interface_failures"],
        }

    if profiler is not None:
        try:
            profiler.summary()
        except Exception:
            # Profiling is observability, not terminal authority.
            pass
    try:
        write_call_summary(root)
    except Exception:
        # A missing ledger must not prevent a terminal mathematical report.
        pass

    report = _publish_report(ws, report)
    if defer_delivery and delivery_pause(root):
        report["artifact_refs"]["publication_checkpoint"] = work_ref(root, "state/delivery_pause.json")
        report["run_status"] = "completed_with_operational_error"
        report["operational_failure_owner"] = "INTERFACE"
        report["operational_reason_code"] = "finding_verification_incomplete"
    _safe_write_json(ws, work_ref(root, "final_report.json"), report, "final report")
    from pace.runtime.output.events import publish_progress
    envelope = FinalReportEnvelope.validate(report)

    publish_progress(root, task_id=f"final-{report['director_state']['round_index']}-{envelope.result}", stage='run', status='finished', final_report=report)

    def resolve_artifact(name: str) -> Path | None:
        ref = str(envelope.artifact_refs.get(name) or "")
        if not ref or not _existing_safe_ref(root, ref):
            return None
        return _safe_existing_path(root, ref)

    profile_file = None
    if profiler is not None and profiler.path is not None:
        profile_path = Path(profiler.path)
        if profile_path.is_file():
            profile_file = profile_path
    return envelope.project(
        run_root=root,
        default_problem_id=ws.problem_id,
        resolve_artifact=resolve_artifact,
        profile_file=profile_file,
        final_report_file=root / work_ref(workspace.root, 'final_report.json'),
    )


def _coerce_workspace(value: RunWorkspace) -> RunWorkspace:
    if not isinstance(value, RunWorkspace):
        raise TypeError("workspace must be a RunWorkspace")
    return value


def _ensure_problem_file(ws: RunWorkspace, problem: Problem) -> str:
    path = _safe_workspace_path(ws.root, work_ref(ws.root, 'input/problem.md'))
    if path is None:
        return "canonical problem.md is not a safe workspace file"
    # The pipeline commits the literal problem as UTF-8 with LF line endings
    # and exactly one terminal newline.  Normalize the supplied value the same
    # way before comparing it with an existing checkpoint.
    expected = _canonical_problem_text(problem.text)
    if not path.is_file():
        _safe_write_text(ws, work_ref(ws.root, 'input/problem.md'), expected, "canonical problem.md")
        return ""
    try:
        actual = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        return f"cannot read canonical problem.md: {exc}"
    if actual != expected:
        return "canonical problem.md differs from the supplied Problem"
    return ""


def _coerce_state(
    ws: RunWorkspace,
    run_result: Any,
) -> tuple[DirectorLedState | None, str, str]:
    raw_state: Any = None
    state_ref = work_ref(ws.root, 'state/director_state.json')
    if isinstance(run_result, DirectorLedRunResult):
        raw_state = run_result.state
        state_ref = str(run_result.state_ref or state_ref)
    elif isinstance(run_result, DirectorLedState):
        raw_state = run_result
    elif isinstance(run_result, Mapping):
        state_ref = str(run_result.get("state_ref") or state_ref)
        raw_state = run_result.get("state", run_result)
    elif run_result is not None and hasattr(run_result, "state"):
        raw_state = getattr(run_result, "state")
        state_ref = str(getattr(run_result, "state_ref", state_ref) or state_ref)

    state_path_error = ""
    if not _is_safe_ref(state_ref):
        state_path_error = "Director-led state_ref is not a safe workspace path"
        state_ref = work_ref(ws.root, 'state/director_state.json')

    state: DirectorLedState | None = None
    if isinstance(raw_state, DirectorLedState):
        state = raw_state
    elif isinstance(raw_state, Mapping):
        try:
            state = DirectorLedState.from_json(raw_state)
        except (DirectorLedEngineError, TypeError, ValueError) as exc:
            state_path_error = state_path_error or f"invalid Director-led state: {exc}"

    state_path = _safe_workspace_path(ws.root, state_ref)
    if state_path is None:
        state_path_error = state_path_error or "Director-led state_ref is not a safe workspace path"
        state_ref = work_ref(ws.root, 'state/director_state.json')
        state_path = _safe_workspace_path(ws.root, state_ref)
    if state_path is None:
        return None, state_ref, state_path_error or "Director-led state path is invalid"
    if state is None and state_path.is_file():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            state = DirectorLedState.from_json(payload)
        except (
            DirectorLedEngineError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            state_path_error = state_path_error or f"cannot load Director-led state: {exc}"
    elif state is not None:
        # The engine normally persisted this already.  Tests and recovery
        # adapters may provide the state object directly, in which case write
        # the same explicit bytes so the manifest has a resolvable state file.
        if state_path.is_file():
            try:
                committed = json.loads(state_path.read_text(encoding="utf-8"))
                if committed != state.to_json():
                    state_path_error = state_path_error or "committed Director-led state differs from run result"
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                state_path_error = state_path_error or f"cannot read committed Director-led state: {exc}"
        elif not state_path_error:
            try:
                _safe_write_json(
                    ws,
                    state_ref,
                    state.to_json(),
                    "Director-led state",
                )
            except Exception as exc:
                state_path_error = (
                    state_path_error
                    or f"cannot persist Director-led state: {exc}"
                )
    if state is None and not state_path_error:
        state_path_error = "Director-led finalization has no committed state"
    return state, state_ref, state_path_error


def _load_target(root: Path) -> tuple[dict[str, Any], str]:
    target_path = Path(root).resolve() / work_ref(root, 'input/target_contract.json')
    if path_has_symlink_component(Path(root).resolve(), target_path):
        return {}, "canonical target_contract.json must not contain a symlink"
    try:
        return load_active_target(root), ""
    except (ProofBindingError, OSError, RuntimeError, ValueError) as exc:
        return {}, str(exc) or type(exc).__name__


def _context_refs(
    ws: RunWorkspace,
    state: DirectorLedState | None,
    state_ref: str,
    target_available: bool,
) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for name, ref in (("problem", work_ref(ws.root, 'input/problem.md')), ("target_contract", work_ref(ws.root, 'input/target_contract.json'))):
        if name == "target_contract" and not target_available:
            continue
        if _existing_safe_ref(ws.root, ref):
            refs[name] = ref
    if _existing_safe_ref(ws.root, state_ref):
        refs["director_state"] = state_ref
    if state is not None:
        for name, ref in (
            ("director_decision", state.last_directive_ref),
            ("director_assignment", state.last_assignment_ref),
            ("specialist_result", state.last_specialist_result_ref),
            ("director_verification", state.last_verification_ref),
            ("director_diagnostics", state.diagnostics_ref),
        ):
            if _existing_safe_ref(ws.root, ref):
                refs[name] = ref
        completed = [
            ref
            for ref in state.completed_result_refs
            if _existing_safe_ref(ws.root, ref)
        ]
        if completed:
            refs["director_completed_results"] = completed
    for name, ref in (
        ("execution_policy", work_ref(ws.root, 'state/research_execution_policy.json')),
        ("memory", "memory/README.md"),
        ("verified_progress", "memory/verified_progress.json"),
    ):
        if _existing_safe_ref(ws.root, ref):
            refs[name] = ref
    return refs


def _resolve_chain(
    ws: RunWorkspace,
    state: DirectorLedState,
    *,
    canonical_target: Mapping[str, Any],
    target_id: str,
) -> dict[str, Any]:
    canonical_target_id = str(canonical_target.get("contract_id") or "").strip()
    state_target_id = str(state.target_contract_id or "").strip()
    resolved_target_id = str(target_id or "").strip()
    if not state_target_id:
        raise DirectorLedFinalizationError(
            "Director-led state has no target_contract_id"
        )
    if not canonical_target_id:
        raise DirectorLedFinalizationError(
            "proof chain has no canonical target identity"
        )
    if state_target_id != canonical_target_id:
        raise DirectorLedFinalizationError(
            "Director-led state target_contract_id differs from canonical target"
        )
    if resolved_target_id != canonical_target_id:
        raise DirectorLedFinalizationError(
            "proof chain target identity is not canonical"
        )
    target_id = canonical_target_id
    verification_observation_ref = str(state.last_verification_ref or "").strip()
    if not _is_safe_ref(verification_observation_ref):
        raise DirectorLedFinalizationError("Director-led verification reference is invalid")
    expected_verification_ref = (
        f"{work_ref(ws.root)}/round{int(state.round_index)}/verification/verification.json"
    )
    if verification_observation_ref != expected_verification_ref:
        raise DirectorLedFinalizationError(
            "Director-led verification reference is not the committed round envelope"
        )
    verification_path = _safe_round_existing_path(
        ws.root,
        verification_observation_ref,
        round_index=state.round_index,
    )
    if verification_path is None:
        raise DirectorLedFinalizationError(
            "Director-led verification reference is not a safe current-round workspace file"
        )
    observation = _read_object(verification_path)
    try:
        value = _validate_verification_checkpoint(
            observation,
            expected_round_index=state.round_index,
            expected_specialist_result_ref=state.last_specialist_result_ref,
        )
    except DirectorLedEngineError as exc:
        raise DirectorLedFinalizationError(str(exc)) from exc
    specialist_result_ref = str(state.last_specialist_result_ref or "").strip()
    if _safe_round_existing_path(
        ws.root,
        specialist_result_ref,
        round_index=state.round_index,
    ) is None:
        raise DirectorLedFinalizationError(
            "Director-led specialist result reference is not a safe current-round workspace file"
        )
    resolved = resolve_verification(
        ws.root, value, target_contract_id=target_id, candidate_ref=state.candidate_ref,
    )
    if resolved["status"] == "VERIFIER_ERROR":
        return {"verification_failure": {
            **resolved["verification_report"]["interface_failure"],
            "verification_interface_failures": resolved["final_result"].get("verification_interface_failures", []),
        }}
    return {
        "pass_chain": resolved["status"] == "PASS",
        "answer_mode": resolved["final_result"]["answer_mode"],
        "proof_binding": resolved["proof_binding"],
        "artifact_refs": resolved["artifact_refs"],
    }


def _last_candidate_state(ws: RunWorkspace, state: DirectorLedState | None) -> DirectorLedState | None:
    if state is None:
        return None
    # PASS must retain its exact winning binding, never choose a later sibling.
    if state.terminal_result == "PASS":
        return state
    candidates = []
    for ref in state.completed_result_refs:
        if Path(ref).name != "agent_result.json" or not _existing_safe_ref(ws.root, ref):
            continue
        result = _read_object(ws.root / ref)
        round_dir = Path(ref).parent
        assignment_ref = (round_dir / "assignment/task.json").as_posix()
        assignment = _read_object(ws.root / assignment_ref)
        if assignment.get("agent_id") == "prover" and result.get("status") == "SUCCEEDED":
            candidates.append((int(assignment["round_index"]), ref, result, assignment_ref))
    if not candidates:
        return state if state.candidate_ref else None
    index, ref, result, assignment_ref = max(candidates, key=lambda item: item[0])
    prefix = Path(ref).parent.as_posix()
    return replace(state, round_index=index, candidate_ref=result["deliverable_ref"],
        last_specialist_result_ref=ref, last_assignment_ref=assignment_ref,
        last_directive_ref=f"{prefix}/director_decision.json",
        last_verification_ref=f"{prefix}/verification/verification.json")


def _publish_report(ws: RunWorkspace, report: dict[str, Any]) -> dict[str, Any]:
    """Publish terminal authority and retain cumulative verified findings."""
    refs = report["artifact_refs"]
    if report["result"] == "PASS":
        try:
            kernel = json.loads(ws.path(refs["kernel"]).read_text(encoding="utf-8"))
            publish_finding(
                ws.root, source_ref=refs["proof"], verification_ref=refs["verification"],
                round_index=report["director_state"]["round_index"],
                publication=kernel["publication"],
            )
        except Exception as exc:
            report["finding_publication_diagnostics"] = [str(exc)]
            logger.warning("Verified final finding publication failed", exc_info=True)
    refs["report"] = work_ref(ws.root, "outcome.md")
    ws.write_text_atomic(refs["report"], "# Mathematical outcome\n\n" + json.dumps({key: report.get(key) for key in ("result", "mathematical_status", "reason_code", "execution_mode", "mode_source", "mode_reason")}, ensure_ascii=False, indent=2) + "\n")
    refs["findings"] = [row["ref"] for row in finding_entries(ws.root)]
    report["publication_status"] = "PUBLISHED" if report["result"] == "PASS" else "REPORT_ONLY"
    FinalReportEnvelope.validate(report)

    _safe_write_json(ws, work_ref(ws.root, 'final_report.json'), report, work_ref(ws.root, 'final_report.json'))
    return report


def _default_reason(
    *,
    state: DirectorLedState | None,
    terminal_result: str,
    chain_error: str,
    operational_error: bool,
) -> str:
    if terminal_result == "PASS":
        return "director_led_verified"
    if operational_error:
        return "director_led_finalization_failed"
    phase = str(state.phase or "") if state is not None else ""
    stop_reason = str(state.stop_reason or "") if state is not None else ""
    if phase == "STOPPED":
        return "director_stopped"
    if stop_reason == "PROOF_STATE_BUDGET_EXHAUSTED":
        return "proof_state_budget_exhausted"
    if stop_reason == "USER_REQUESTED_FINALIZATION":
        return "user_requested_finalization"
    if chain_error:
        return "director_led_verification_failed"
    if phase == "FAILED":
        return "director_led_host_failure"
    return "director_led_search_exhausted"


def _existing_safe_ref(root: Path, ref: str) -> bool:
    return _safe_existing_path(root, ref) is not None


def _safe_existing_path(root: Path, ref: str) -> Path | None:
    """Resolve one public file without following a symlink out of the run."""

    return _safe_workspace_path(root, ref, require_file=True)


def _safe_round_existing_path(
    root: Path,
    ref: str,
    *,
    round_index: int,
) -> Path | None:
    """Resolve a chain artifact and require it to belong to this round."""

    if isinstance(round_index, bool) or int(round_index or 0) < 1:
        return None
    resolved = _safe_existing_path(root, ref)
    if resolved is None:
        return None
    try:
        expected_root = (
            Path(root).resolve()
            / work_ref(root)
            / f"round{int(round_index)}"
        ).resolve()
        resolved.relative_to(expected_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _safe_workspace_path(
    root: Path,
    ref: str,
    *,
    require_file: bool = False,
) -> Path | None:
    """Resolve a public reference inside the real run workspace."""

    normalized = str(ref or "").strip().replace("\\", "/")
    if not _is_safe_ref(normalized):
        return None
    root_path = Path(root).resolve()
    try:
        lexical = root_path / normalized
        if path_has_symlink_component(root_path, lexical):
            return None
        resolved = lexical.resolve()
        relative = resolved.relative_to(root_path)
    except (OSError, RuntimeError, ValueError):
        return None
    if _is_private_transport_path(relative):
        return None
    if require_file and not resolved.is_file():
        return None
    return resolved


def _safe_write_json(
    ws: RunWorkspace,
    ref: str,
    payload: Any,
    label: str,
) -> Path:
    normalized = str(ref or "").strip().replace("\\", "/")
    _safe_write_path(ws.root, normalized, label)
    return ws.write_json(normalized, payload)


def _safe_write_text(
    ws: RunWorkspace,
    ref: str,
    content: str,
    label: str,
) -> Path:
    normalized = str(ref or "").strip().replace("\\", "/")
    _safe_write_path(ws.root, normalized, label)
    return ws.write_text_atomic(normalized, content)


def _safe_write_path(root: Path, ref: str, label: str) -> Path:
    """Validate a canonical destination before creating or replacing it."""

    normalized = str(ref or "").strip().replace("\\", "/")
    if not _is_safe_ref(normalized):
        raise DirectorLedFinalizationError(
            f"{label} is not a safe workspace path: {normalized}"
        )
    root_path = Path(root).resolve()
    lexical = root_path / normalized
    if path_has_symlink_component(root_path, lexical):
        raise DirectorLedFinalizationError(
            f"{label} must not contain a symlink: {normalized}"
        )
    try:
        resolved = lexical.resolve()
        relative = resolved.relative_to(root_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DirectorLedFinalizationError(
            f"{label} escapes the run workspace: {normalized}"
        ) from exc
    if _is_private_transport_path(relative):
        raise DirectorLedFinalizationError(
            f"{label} points to a private or transport artifact"
        )
    return resolved


def _is_safe_ref(ref: str) -> bool:
    value = str(ref or "").strip().replace("\\", "/")
    if not value:
        return False
    path = Path(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        return False
    if is_call_path(path):
        return False
    if path.name.startswith("private_"):
        return False
    return True




def _is_private_transport_path(path: Path) -> bool:
    return is_call_path(path) or path.name.startswith("private_")


def _canonical_problem_text(value: Any) -> str:
    return (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .rstrip("\n")
        + "\n"
    )


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DirectorLedFinalizationError(f"JSON artifact must be an object: {path}")
    return value


__all__ = [
    "DirectorLedFinalizationError",
    "finalize_director_led",
]
