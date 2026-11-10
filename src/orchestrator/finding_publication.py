"""Check committed tasks' finding drafts, then publish explicit approvals."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Iterable

from pace.agents.kernel import verify_finding
from pace.runtime.output.results import (
    FINDINGS_INDEX_REF,
    FINDINGS_CATALOG_REF,
    publish_finding,
)
from pace.runtime.output.workspace import (
    _write_json_atomic,
    is_call_path,
    path_has_symlink_component,
    work_ref,
)
from pace.runtime.semantic import StructuredSemanticResult, SemanticInterfaceFailure
from pace.runtime.math_format import artifact_format_issues
from pace.reporting.editorial import review_draft

from pace.runtime.output.finding_manifest import current_finding_drafts

logger = logging.getLogger(__name__)


def pending_finding_checks(workdir: Path) -> bool:
    """Find unfinished format repairs or verifier calls before publication."""
    state_path = Path(workdir) / work_ref(workdir, "state/director_state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    accepted = state.get("candidate_ref") if state.get("terminal_result") == "PASS" else ""
    completed = {str(Path(ref).parent) for ref in state.get("completed_result_refs", [])
                 if Path(ref).name == "agent_result.json"}
    for directory in completed:
        result_path = Path(workdir) / directory / "agent_result.json"
        if accepted and result_path.is_file():
            if json.loads(result_path.read_text(encoding="utf-8")).get("deliverable_ref") == accepted:
                continue
        active = current_finding_drafts(Path(workdir), Path(workdir) / directory)
        for path in (draft.with_suffix(".format.json") for draft in active):
            if not path.is_file():
                continue
            if not json.loads(path.read_text())["editorial"].get("approved"):
                return True
        for path in (draft.with_suffix(".verification.json") for draft in active):
            if not path.is_file():
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
                return True
    return False


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
        try:
            result = json.loads((root / result_ref).read_text(encoding="utf-8"))
            if result.get("status") != "SUCCEEDED":
                continue
            if accepted_candidate_ref and result["deliverable_ref"] == accepted_candidate_ref:
                continue
            drafts = current_finding_drafts(root, root / directory)
            if not drafts:
                continue
            assignment = json.loads(
                (root / directory / "task.json").read_text(encoding="utf-8")
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


def _finding_format_state(root: Path, draft: Path) -> dict | None:
    """Bind saved revisions to this finding's directory and original manuscript."""
    source_ref = draft.relative_to(root).as_posix()
    state_path = draft.with_suffix(".format.json")
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state["source_ref"] != source_ref:
        raise ValueError("finding format repair belongs to another manuscript")
    editorial = state["editorial"]
    refs = (editorial["current_ref"], *editorial.get("revision_refs", ()),
            *((editorial["revision"]["output_ref"],) if editorial.get("revision") else ()),
            *((editorial["revision"]["patch_ref"],)
              if (editorial.get("revision") or {}).get("patch_ref") else ()))
    for ref in refs:
        path = root / ref
        if (path.parent != draft.parent or path.relative_to(root).as_posix() != ref
                or path_has_symlink_component(root, path)):
            raise ValueError("finding format revision must stay beside its original manuscript")
    return state


def _formatted_finding(*, llm: Any, root: Path, draft: Path) -> Path:
    """Repair a new finding's format without recalling its completed researcher."""
    if path_has_symlink_component(root, draft):
        raise ValueError("finding manuscript must not contain a symlink")
    source_ref = draft.relative_to(root).as_posix()
    state_path = draft.with_suffix(".format.json")
    state = _finding_format_state(root, draft)
    if state is None:
        if not artifact_format_issues(root, source_ref):
            return draft
        state = {"source_ref": source_ref}
    # This is format-only approval. Mathematical acceptance still requires a
    # fresh finding verifier on the corrected copy, with the original retained.
    corrected = review_draft(
        llm=llm, root=root, state=state,
        state_ref=state_path.relative_to(root).as_posix(),
        source_ref=source_ref, input_refs=(), markdown=True, independent=False,
    )
    if artifact_format_issues(root, corrected):
        raise ValueError("Finding still has formatting or local-link defects")
    return root / corrected


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
    source_ref = draft.relative_to(root).as_posix()
    verification_path = draft.with_suffix(".verification.json")
    state = _finding_format_state(root, draft)
    if state is not None:
        draft = root / state["editorial"]["current_ref"]
    draft_ref = draft.relative_to(root).as_posix()
    record = None
    if verification_path.is_file():
        record = json.loads(verification_path.read_text(encoding="utf-8"))
        if record["target_contract_id"] != target_id:
            raise ValueError("finding verification belongs to another manuscript or target")
        if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
            if not retry_errors:
                return False
            history = verification_path.parent / (verification_path.stem + ".attempts")
            _write_json_atomic(history / f"{uuid.uuid4().hex}.json", record)
            record = None
        elif record["draft_ref"] != draft_ref:
            raise ValueError("finding verification belongs to another manuscript or target")
    if record is None:
        original = root / source_ref
        draft = _formatted_finding(llm=llm, root=root, draft=original)
        draft_ref = draft.relative_to(root).as_posix()
        refs = []
        for ref in (draft_ref, *context_refs, FINDINGS_INDEX_REF, FINDINGS_CATALOG_REF):
            if ref == source_ref:
                ref = draft_ref
            path = (root / ref).resolve()
            if path.is_relative_to(root) and path.is_file() and not is_call_path(path):
                relative = path.relative_to(root).as_posix()
                if relative not in refs:
                    refs.append(relative)
        body = draft.read_bytes()
        if not body.decode("utf-8").strip():
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
        if draft.read_bytes() != body:
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
            "source_draft_ref": source_ref,
            "draft_sha256": hashlib.sha256(body).hexdigest(),
            "target_contract_id": target_id,
            "call_id": outcome.host.get("call_id", ""),
            "verdict": verdict,
        }
        _write_json_atomic(verification_path, record)
    if record["draft_ref"] != draft_ref or record["target_contract_id"] != target_id:
        raise ValueError("finding verification belongs to another manuscript or target")
    if record.get("draft_sha256") and record["draft_sha256"] != hashlib.sha256(
        draft.read_bytes()
    ).hexdigest():
        raise ValueError("finding manuscript changed after independent verification")
    verdict = record["verdict"]
    if verdict.get("stage_status") == "VERIFIER_ERROR":
        return False
    if verdict.get("stage_status") == "PASS":
        publish_finding(root, source_ref=draft_ref,
                        verification_ref=verification_path.relative_to(root).as_posix(),
                        round_index=round_index, publication=verdict["publication"])
    return True
