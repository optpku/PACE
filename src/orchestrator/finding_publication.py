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
    FINDINGS_CATALOG_REF,
    FINDINGS_ARCHIVE_REF,
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
    from pace.contracts.research.memory import enabled
    if enabled(workdir):
        index_path = Path(workdir) / 'memory/research_index.json'
        if index_path.is_file():
            from pace.runtime.output.memory import whole_proof_application_routes

            index = json.loads(index_path.read_text())
            whole_routes = whole_proof_application_routes(workdir, index, state=state)
            for key, app in index['applications'].items():
                obligation = index['obligations'][app['obligation_id']]
                if (key not in whole_routes and app['status'] == 'PROPOSED' and obligation.get('disposition', 'OPEN') == 'OPEN'
                    and obligation['version'] == app['obligation_version']):
                    return True
    for path in (Path(workdir) / work_ref(workdir)).glob("*/findings/*.verification.json"):
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
    if not publish_applications(llm=llm, root=root, retry_errors=retry_errors):
        return False
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
    from pace.contracts.research.memory import binding, check_binding, enabled
    version = binding(root, draft_ref) if enabled(root) else None
    if verification_path.is_file():
        record = json.loads(verification_path.read_text(encoding="utf-8"))
        if record.get("source_binding"):
            check_binding(root, record["source_binding"])
            for row in record.get("dependency_bindings", []): check_binding(root, row)
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
        archives = () if enabled(root) else (FINDINGS_INDEX_REF, FINDINGS_CATALOG_REF, FINDINGS_ARCHIVE_REF)
        for ref in (draft_ref, *context_refs, *archives):
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
            **({"source_binding": version, "dependency_bindings": [binding(root, r) for r in context_refs
                 if (root / r).is_file() and not r.startswith(("memory/", "results/")) and "/state/" not in r]} if version else {}),
        }
        _write_json_atomic(verification_path, record)
    if record["draft_ref"] != draft_ref or record["target_contract_id"] != target_id:
        raise ValueError("finding verification belongs to another manuscript or target")
    verdict = record["verdict"]
    if verdict.get("stage_status") == "VERIFIER_ERROR":
        return False
    if verdict.get("stage_status") == "PASS":
        publish_finding(
            root,
            source_ref=draft_ref,
            verification_ref=verification_path.relative_to(root).as_posix(),
            round_index=round_index,
            publication=verdict["publication"],
        )
    return True


def publish_applications(*, llm, root, retry_errors=False):
    """Independently check committed applications, including publication NONE."""
    from pace.contracts.research.memory import (
        application_review_bindings, binding, check_binding, digest, enabled, read_json,
    )
    from pace.runtime.output.memory import RESEARCH_INDEX_REF, refresh_application_memory, whole_proof_application_routes
    from pace.runtime.output.events import append_event
    if not enabled(root): return True
    index = read_json(root, RESEARCH_INDEX_REF)
    whole_routes = whole_proof_application_routes(root, index)
    for key, app in index['applications'].items():
        if index['obligations'][app['obligation_id']].get('disposition', 'OPEN') != 'OPEN': continue
        if index['obligations'][app['obligation_id']]['version'] != app['obligation_version']: continue
        if key in whole_routes: continue
        bindings = application_review_bindings(app)
        for row in bindings: check_binding(root, row)
        directory = Path(app['result_ref']).parent / 'verification'
        ref = str(directory / f'application-{key}.json')
        review_ref = str(directory / f'application-{key}.md')
        request_ref = str(directory / f'application-{key}.request.json')
        request = {'schema_version': 'application_request/v1', 'application_key': key,
                   'obligation_id': app['obligation_id'], 'obligation_version': app['obligation_version'],
                   'argument_ref': app['argument_ref'], 'bindings': bindings,
                   'review_ref': review_ref, 'purpose': 'APPLICATION'}
        if (root / request_ref).is_file():
            if read_json(root, request_ref) != request: raise ValueError('application request binding changed')
        else: _write_json_atomic(root / request_ref, request)
        record = read_json(root, ref) if (root / ref).is_file() else None
        if record and record['application_key'] != key: raise ValueError('wrong application review')
        if record:
            if record['bindings'] != bindings or record['request_binding'] != binding(root, request_ref):
                raise ValueError('application review dependencies changed')
            if record.get('review_binding'): check_binding(root, record['review_binding'])
        if record and record['verdict'].get('stage_status') == 'VERIFIER_ERROR':
            if not retry_errors: return False
            _write_json_atomic(root / directory / f'application-{key}.{uuid.uuid4().hex}.attempt.json', record)
            record = None
        if record is None:
            assignment = read_json(root, str(Path(app['result_ref']).parent / 'assignment/task.json'))
            def save_review(verdict, call_id=''):
                for row in bindings: check_binding(root, row)
                saved = {'application_key': key, 'verdict': verdict, 'bindings': bindings,
                         'request_ref': request_ref, 'request_binding': binding(root, request_ref),
                         'review_binding': binding(root, review_ref) if (root / review_ref).is_file() else None,
                         'call_id': call_id}
                _write_json_atomic(root / ref, saved)
                return saved
            outcome = verify_finding(llm=llm, workdir=root, draft_ref=app['argument_ref'],
                input_refs=tuple(dict.fromkeys([request_ref, *(r['ref'] for r in bindings)])),
                round_index=assignment['round_index'], target_contract_id=read_json(root, app['target']['ref'])['contract_id'],
                application_request=request, on_accept=save_review)
            for row in bindings: check_binding(root, row)
            verdict = outcome.payload if isinstance(outcome, StructuredSemanticResult) else {
                'stage_status': 'VERIFIER_ERROR', 'diagnostics': list(outcome.diagnostics)}
            record = save_review(verdict, outcome.host.get('call_id', ''))
        if record['verdict'].get('stage_status') == 'VERIFIER_ERROR': return False
        append_event(root, 'APPLICATION_REVIEW_COMMITTED', idempotency_key='application:' + digest(record),
                     application_key=key, review_ref=ref, obligation_id=app['obligation_id'])
    refresh_application_memory(root)
    return True
