"""Independently check turn-local findings and applications for the outer publisher."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Iterable

from pace.agents.finding_verifier import verify_finding
from pace.artifacts.results import (
    FINDINGS_ARCHIVE_REF,
    FINDINGS_CATALOG_REF,
    FINDINGS_INDEX_REF,
    committed_finding_refs,
)
from pace.artifacts.workspace import (
    _write_json_atomic,
    is_call_path,
    work_ref,
)
from pace.runtime.semantic import SemanticInterfaceFailure, StructuredSemanticResult

logger = logging.getLogger(__name__)


def pending_finding_checks(workdir: Path) -> bool:
    """Find unfinished finding-verifier calls before resuming publication."""
    state_path = Path(workdir) / work_ref(workdir, "state/director_state.json")
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.is_file()
        else {}
    )
    accepted = (
        state.get("candidate_ref") if state.get("terminal_result") == "PASS" else ""
    )
    if accepted:
        return False
    from pace.contracts.research.memory import enabled

    if enabled(workdir):
        index_path = Path(workdir) / "memory/research_index.json"
        if index_path.is_file():
            from pace.orchestrator.memory import whole_proof_application_routes

            index = json.loads(index_path.read_text())
            whole_routes = whole_proof_application_routes(workdir, index, state=state)
            for key, app in index["applications"].items():
                obligation = index["obligations"][app["obligation_id"]]
                if (
                    key not in whole_routes
                    and app["status"] == "PROPOSED"
                    and obligation.get("disposition", "OPEN") == "OPEN"
                    and obligation["version"] == app["obligation_version"]
                ):
                    return True
    for result_ref in state.get("completed_result_refs", []):
        if Path(result_ref).name != "agent_result.json":
            continue
        for ref in committed_finding_refs(Path(workdir), result_ref):
            path = (Path(workdir) / ref).with_suffix(".verification.json")
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
                    return True
    return False


def check_stage_findings(
    *,
    llm: Any,
    workdir: Path,
    completed_result_refs: Iterable[str],
    retry_errors: bool = False,
) -> bool:
    """Check local findings; the outer orchestrator publishes their bound receipts.

    The parent requests these checks while research is open. Whole-target
    acceptance proceeds directly to the Kernel's terminal publication.
    """

    root = Path(workdir).resolve()
    if not check_applications(llm=llm, root=root, retry_errors=retry_errors):
        return False
    for result_ref in dict.fromkeys(completed_result_refs):
        if Path(result_ref).name != "agent_result.json":
            continue
        directory = Path(result_ref).parent
        drafts = [root / ref for ref in committed_finding_refs(root, result_ref)]
        if not drafts:
            continue
        try:
            result = json.loads((root / result_ref).read_text(encoding="utf-8"))
            if result.get("status") != "SUCCEEDED":
                continue
            assignment = json.loads(
                (root / directory / "assignment/task.json").read_text(encoding="utf-8")
            )
            round_index = int(assignment["round_index"])
            target = json.loads(
                (root / work_ref(workdir, "input/target_contract.json")).read_text(
                    encoding="utf-8"
                )
            )
            target_id = target["contract_id"]
            context_refs = (
                work_ref(workdir, "input/problem.md"),
                work_ref(workdir, "input/target_contract.json"),
                result["deliverable_ref"],
                *assignment.get("input_refs", ()),
                *result.get("published_refs", ()),
            )
            for draft in drafts:
                if not _check_finding(
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


def _check_finding(
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
            for row in record.get("dependency_bindings", []):
                check_binding(root, row)
        if (
            record["draft_ref"] != draft_ref
            or record["target_contract_id"] != target_id
        ):
            raise ValueError(
                "finding verification belongs to another manuscript or target"
            )
        if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
            if not retry_errors:
                return False
            history = verification_path.parent / (verification_path.stem + ".attempts")
            _write_json_atomic(history / f"{uuid.uuid4().hex}.json", record)
            record = None
    if record is None:
        refs = []
        archives = (
            ()
            if enabled(root)
            else (FINDINGS_INDEX_REF, FINDINGS_CATALOG_REF, FINDINGS_ARCHIVE_REF)
        )
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
                llm=llm,
                workdir=root,
                draft_ref=draft_ref,
                input_refs=tuple(refs),
                round_index=round_index,
                target_contract_id=target_id,
            )
        except Exception as exc:
            outcome = SemanticInterfaceFailure(
                host={},
                diagnostics=(
                    {
                        "reason_code": "finding_verifier_exception",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
            )
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
                "receipt_corrections": int(
                    outcome.host.get("host_metadata", {}).get("receipt_corrections", 0)
                ),
            }
        )
        record = {
            "draft_ref": draft_ref,
            "target_contract_id": target_id,
            "call_id": outcome.host.get("call_id", ""),
            "verdict": verdict,
            **(
                {
                    "source_binding": version,
                    "dependency_bindings": [
                        binding(root, r)
                        for r in context_refs
                        if (root / r).is_file()
                        and not r.startswith(("memory/", "results/"))
                        and "/state/" not in r
                    ],
                }
                if version
                else {}
            ),
        }
        _write_json_atomic(verification_path, record)
    if record["draft_ref"] != draft_ref or record["target_contract_id"] != target_id:
        raise ValueError("finding verification belongs to another manuscript or target")
    verdict = record["verdict"]
    if verdict.get("stage_status") == "VERIFIER_ERROR":
        return False
    return True


def _commit_application_reviews(root, request, verification_ref):
    """Each scope references the same independently authored manuscript receipt."""
    from pace.artifacts.events import append_event
    from pace.contracts.research.memory import binding, digest

    version = binding(root, verification_ref)
    for item in request["applications"]:
        ref = item["application_ref"]
        _write_json_atomic(root / ref, {"verification_binding": version})
        append_event(
            root,
            "APPLICATION_REVIEW_COMMITTED",
            idempotency_key="application:" + digest([item["application_key"], version]),
            application_key=item["application_key"],
            review_ref=verification_ref,
            obligation_id=item["obligation_id"],
        )


def check_applications(*, llm, root, retry_errors=False):
    """Review each manuscript once, retaining a decision for every requested scope."""
    from pace.contracts.research.memory import (
        application_review_bindings,
        check_binding,
        digest,
        enabled,
        read_json,
    )
    from pace.orchestrator.memory import (
        RESEARCH_INDEX_REF,
        whole_proof_application_routes,
    )

    root = Path(root)
    if not enabled(root):
        return True
    index = read_json(root, RESEARCH_INDEX_REF)
    whole_routes = whole_proof_application_routes(root, index)
    groups = {}
    for key, app in sorted(index["applications"].items()):
        obligation = index["obligations"][app["obligation_id"]]
        if (
            obligation.get("disposition", "OPEN") != "OPEN"
            or obligation["version"] != app["obligation_version"]
            or key in whole_routes
        ):
            continue
        for row in application_review_bindings(app):
            check_binding(root, row)
        group = digest([app["target"], app["argument"]])
        groups.setdefault(group, []).append(app)

    return all(
        _check_application_group(
            llm=llm, root=root, apps=apps, retry_errors=retry_errors
        )
        for apps in groups.values()
    )


def _check_application_group(*, llm, root, apps, retry_errors):
    from pace.contracts.research.memory import (
        application_review_bindings,
        binding,
        check_binding,
        digest,
        load_application_review,
        read_json,
    )

    owner = apps[0]
    directory = Path(owner["result_ref"]).parent / "verification"
    batch_key = digest([app["application_key"] for app in apps])
    stem = directory / f"finding-{batch_key}"
    request_ref = str(stem.with_suffix(".request.json"))
    request = read_json(root, request_ref) if (root / request_ref).is_file() else None
    record = None
    if request and (root / request["verification_ref"]).is_file():
        record = read_json(root, request["verification_ref"])
        check_binding(root, record["request_binding"])
        for row in record["dependency_bindings"]:
            check_binding(root, row)
        if record.get("review_binding"):
            check_binding(root, record["review_binding"])
        if record["verdict"].get("stage_status") != "VERIFIER_ERROR":
            _commit_application_reviews(root, request, request["verification_ref"])
        elif not retry_errors:
            return False
        else:
            history = root / directory / f"finding-{batch_key}.attempts"
            _write_json_atomic(history / f"{uuid.uuid4().hex}.json", record)
            record = None

    pending = []
    for app in apps:
        reviewed = load_application_review(root, app)
        if (
            reviewed is None
            or reviewed["verdict"].get("stage_status") == "VERIFIER_ERROR"
        ):
            pending.append(app)
    if not pending:
        return True
    if request is None:
        draft = root / owner["argument_ref"]
        finding_receipt = draft.with_suffix(".verification.json")
        publication = (
            owner["argument_ref"] in committed_finding_refs(root, owner["result_ref"])
            and not finding_receipt.exists()
        )
        verification_ref = (
            finding_receipt.relative_to(root).as_posix()
            if publication
            else str(stem.with_suffix(".json"))
        )
        request = {
            "argument_ref": owner["argument_ref"],
            "review_ref": str(stem.with_suffix(".md")),
            "verification_ref": verification_ref,
            "publication_requested": publication,
            "applications": [
                {
                    "application_key": app["application_key"],
                    "obligation_id": app["obligation_id"],
                    "obligation_version": app["obligation_version"],
                    "bindings": application_review_bindings(app),
                    "application_ref": str(
                        Path(app["result_ref"]).parent
                        / "verification"
                        / f"application-{app['application_key']}.json"
                    ),
                }
                for app in pending
            ],
        }
        _write_json_atomic(root / request_ref, request)
    bindings = list(
        {
            digest(row): row
            for item in request["applications"]
            for row in item["bindings"]
        }.values()
    )
    verification_ref = request["verification_ref"]

    saved_record = None

    def save_review(verdict):
        nonlocal saved_record
        for row in bindings:
            check_binding(root, row)
        saved = {
            "draft_ref": owner["argument_ref"],
            "target_contract_id": read_json(root, owner["target"]["ref"])[
                "contract_id"
            ],
            "source_binding": owner["argument"],
            "dependency_bindings": bindings,
            "request_ref": request_ref,
            "request_binding": binding(root, request_ref),
            "review_binding": binding(root, request["review_ref"])
            if (root / request["review_ref"]).is_file()
            else None,
            "verdict": verdict,
        }
        _write_json_atomic(root / verification_ref, saved)
        saved_record = saved
        return saved

    assignment = read_json(
        root, str(Path(owner["result_ref"]).parent / "assignment/task.json")
    )
    outcome = verify_finding(
        llm=llm,
        workdir=root,
        draft_ref=owner["argument_ref"],
        input_refs=tuple(dict.fromkeys([request_ref, *(r["ref"] for r in bindings)])),
        round_index=assignment["round_index"],
        target_contract_id=read_json(root, owner["target"]["ref"])["contract_id"],
        application_request=request,
        on_accept=save_review,
    )
    if isinstance(outcome, StructuredSemanticResult):
        # Acceptance persists the receipt before semantic completion. Replaying this
        # same frozen group also repairs any interrupted per-scope references.
        record = saved_record or save_review(outcome.payload)
    else:
        record = save_review(
            {"stage_status": "VERIFIER_ERROR", "diagnostics": list(outcome.diagnostics)}
        )
    _commit_application_reviews(root, request, verification_ref)
    if record["verdict"].get("stage_status") == "VERIFIER_ERROR":
        return False
    return True
