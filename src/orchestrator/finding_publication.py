"""Check committed tasks' finding drafts, then publish explicit approvals."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Iterable

from pace.agents.verification.kernel import verify_finding
from pace.runtime.output.results import (
    FINDINGS_INDEX_REF,
    finding_entries,
    publish_finding,
)
from pace.runtime.output.workspace import (
    _write_json_atomic,
    is_call_path,
    work_ref,
)
from pace.runtime.semantic import StructuredSemanticResult, SemanticInterfaceFailure

logger = logging.getLogger(__name__)


def pending_finding_checks(workdir: Path) -> bool:
    """Find unfinished finding-verifier calls before resuming publication."""
    state_path = Path(workdir) / work_ref(workdir, "state/director_state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    accepted = state.get("candidate_ref") if state.get("terminal_result") == "PASS" else ""
    completed = {str(Path(ref).parent) for ref in state.get("completed_result_refs", [])
                 if Path(ref).name == "agent_result.json"}
    for path in (Path(workdir) / work_ref(workdir, "stages")).glob("*/findings/*.verification.json"):
        if path.parent.parent.relative_to(workdir).as_posix() not in completed:
            continue
        result_path = path.parent.parent / "agent_result.json"
        if accepted and result_path.is_file():
            if json.loads(result_path.read_text(encoding="utf-8")).get("deliverable_ref") == accepted:
                continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
            return True
    return False


def publication_reuse_refs(workdir: Path, verdict: dict[str, Any]) -> list[str]:
    """Consume the verifier's publication directive, not a theorem comparison."""

    refs = verdict.get("existing_finding_refs", [])
    published = {row["ref"] for row in finding_entries(workdir)}
    if not isinstance(refs, list) or any(
        not isinstance(ref, str) or ref not in published for ref in refs
    ):
        raise ValueError("existing_finding_refs must list published finding references")
    return refs


def publish_stage_findings(
    *,
    llm: Any,
    workdir: Path,
    completed_result_refs: Iterable[str],
    accepted_candidate_ref: str = "",
    retry_errors: bool = False,
) -> bool:
    """Publish partial results; a passed whole candidate is published once.

    Only the parent publishes. Drafts accompanying the accepted whole candidate
    remain in its stage rather than receiving separate checks and result IDs.
    """

    root = Path(workdir).resolve()
    for result_ref in dict.fromkeys(completed_result_refs):
        if Path(result_ref).name != "agent_result.json":
            continue
        directory = Path(result_ref).parent
        drafts = sorted((root / directory / "findings").glob("*.md"))
        if not drafts:
            continue
        try:
            result = json.loads((root / result_ref).read_text(encoding="utf-8"))
            if result.get("status") != "SUCCEEDED":
                continue
            if accepted_candidate_ref and result["deliverable_ref"] == accepted_candidate_ref:
                continue
            assignment = json.loads(
                (root / directory / "assignment/task.json").read_text(encoding="utf-8")
            )
            round_index = int(assignment["round_index"])
            target = json.loads(
                (root / work_ref(workdir, 'input/target_contract.json')).read_text(encoding="utf-8")
            )
            target_id = target["contract_id"]
            context_refs = (
                work_ref(workdir, 'input/problem.md'),
                work_ref(workdir, 'input/target_contract.json'),
                result["deliverable_ref"],
                *assignment.get("input_refs", ()),
                *result.get("published_refs", ()),
            )
            for draft in drafts:
                if not _check_and_publish(
                    llm=llm,
                    root=root,
                    draft=draft,
                    context_refs=context_refs,
                    round_index=round_index,
                    target_id=target_id,
                    retry_errors=retry_errors,
                ):
                    return False
        except Exception:
            # A finding publication cannot change the task or final proof
            # verdict. Leave its draft and call records available to review.
            logger.warning("Finding publication failed: %s", result_ref, exc_info=True)
            return False
    return True


def _check_and_publish(
    *,
    llm: Any,
    root: Path,
    draft: Path,
    context_refs: tuple,
    round_index: int,
    target_id: str,
    retry_errors: bool = False,
) -> bool:
    draft_ref = draft.relative_to(root).as_posix()
    verification_path = draft.with_suffix(".verification.json")
    record = None
    if verification_path.is_file():
        record = json.loads(verification_path.read_text(encoding="utf-8"))
        if record["draft_ref"] != draft_ref or record["target_contract_id"] != target_id:
            raise ValueError("finding verification belongs to another manuscript or target")
        if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
            if not retry_errors:
                return False
            history = verification_path.parent / (verification_path.stem + ".attempts")
            _write_json_atomic(history / f"{uuid.uuid4().hex}.json", record)
            record = None
    if record is None:
        refs = []
        for ref in (draft_ref, *context_refs, FINDINGS_INDEX_REF):
            path = (root / ref).resolve()
            if path.is_relative_to(root) and path.is_file() and not is_call_path(path):
                relative = path.relative_to(root).as_posix()
                if relative not in refs:
                    refs.append(relative)
        body = draft.read_text(encoding="utf-8")
        if not body.strip():
            return True
        try:
            outcome = verify_finding(
                llm=llm, workdir=root, draft_ref=draft_ref, input_refs=tuple(refs),
                round_index=round_index, target_contract_id=target_id,
            )
        except Exception as exc:
            outcome = SemanticInterfaceFailure(host={}, diagnostics=({
                "reason_code": "finding_verifier_exception", "error_type": type(exc).__name__,
                "message": str(exc),
            },))
        if draft.read_text(encoding="utf-8") != body:
            raise ValueError(
                "finding manuscript changed during independent verification"
            )
        verdict = (
            outcome.payload
            if isinstance(outcome, StructuredSemanticResult)
            else {
                "stage_status": "VERIFIER_ERROR",
                "diagnostics": list(outcome.diagnostics),
                "receipt_corrections": int(outcome.host.get("host_metadata", {}).get("receipt_corrections", 0)),
            }
        )
        record = {
            "draft_ref": draft_ref,
            "target_contract_id": target_id,
            "call_id": outcome.host.get("call_id", ""),
            "verdict": verdict,
        }
        _write_json_atomic(verification_path, record)
    if record["draft_ref"] != draft_ref or record["target_contract_id"] != target_id:
        raise ValueError("finding verification belongs to another manuscript or target")
    verdict = record["verdict"]
    if verdict.get("stage_status") == "VERIFIER_ERROR":
        return False
    if (
        verdict.get("stage_status") == "PASS"
        and verdict.get("reuse_authorized") is True
    ):
        try:
            reused = publication_reuse_refs(root, verdict)
        except ValueError as exc:
            record["publication_diagnostics"] = [str(exc)]
            _write_json_atomic(verification_path, record)
            return True
        if reused:
            return True
        publish_finding(
            root,
            source_ref=draft_ref,
            verification_ref=verification_path.relative_to(root).as_posix(),
            round_index=round_index,
        )
    return True
