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
    is_rci_critic_material,
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
        if (
            json.loads((Path(workdir) / result_ref).read_text()).get("status")
            != "SUCCEEDED"
        ):
            continue
        for ref in committed_finding_refs(Path(workdir), result_ref):
            path = (Path(workdir) / ref).with_suffix(".verification.json")
            if not path.is_file():
                return True
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
    # A manuscript may be nominated by a different handoff from the one
    # proposing its application. Resolve publication work before either review.
    findings = {}
    for result_ref in dict.fromkeys(completed_result_refs):
        if Path(result_ref).name != "agent_result.json":
            continue
        drafts = committed_finding_refs(root, result_ref)
        if not drafts:
            continue
        result = json.loads((root / result_ref).read_text())
        if result.get("status") != "SUCCEEDED":
            continue
        assignment = json.loads(
            (root / Path(result_ref).parent / "assignment/task.json").read_text()
        )
        for draft_ref in drafts:
            findings.setdefault(
                draft_ref, {"round_index": int(assignment["round_index"]), "refs": []}
            )
            dependencies = result.get("manuscript_dependencies", {}).get(draft_ref)
            if dependencies is None:
                dependencies = [result["deliverable_ref"], *result.get("published_refs", ())]
            findings[draft_ref]["refs"].extend(
                ref
                for ref in (
                    work_ref(root, "input/problem.md"),
                    work_ref(root, "input/target_contract.json"),
                    *assignment.get("input_refs", ()),
                    *dependencies,
                )
                if not is_rci_critic_material(ref)
            )
    if not check_applications(
        llm=llm, root=root, retry_errors=retry_errors, findings=findings
    ):
        return False
    for draft_ref, finding in findings.items():
        try:
            target = json.loads(
                (root / work_ref(root, "input/target_contract.json")).read_text()
            )
            if not _check_finding(
                llm=llm,
                root=root,
                draft=root / draft_ref,
                context_refs=tuple(dict.fromkeys(finding["refs"])),
                round_index=finding["round_index"],
                target_id=target["contract_id"],
                retry_errors=retry_errors,
            ):
                return False
        except Exception:
            logger.warning("Finding verification failed: %s", draft_ref, exc_info=True)
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
    from pace.contracts.research.memory import (
        binding,
        check_finding_record,
        enabled,
    )

    version = binding(root, draft_ref)
    if verification_path.is_file():
        record = json.loads(verification_path.read_text(encoding="utf-8"))
        check_finding_record(root, draft_ref, target_id, record)
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
        if not draft.read_text(encoding="utf-8").strip():
            return True
        dependencies = [
            binding(root, ref)
            for ref in refs
            if ref != draft_ref
            and not ref.startswith(("memory/", "results/"))
            and "/state/" not in ref
        ]

        def save(verdict, call_id=""):
            nonlocal record
            candidate = {
                "draft_ref": draft_ref,
                "target_contract_id": target_id,
                "call_id": call_id,
                "verdict": verdict,
                "source_binding": version,
                "dependency_bindings": dependencies,
            }
            check_finding_record(root, draft_ref, target_id, candidate)
            _write_json_atomic(verification_path, candidate)
            record = candidate

        try:
            outcome = verify_finding(
                llm=llm,
                workdir=root,
                draft_ref=draft_ref,
                input_refs=tuple(refs),
                round_index=round_index,
                target_contract_id=target_id,
                on_accept=save,
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
        if isinstance(outcome, StructuredSemanticResult):
            call_id = outcome.host.get("call_id", "")
            if record is None or call_id:
                save(outcome.payload, call_id)
        elif record is None:
            save(
                {
                    "stage_status": "VERIFIER_ERROR",
                    "diagnostics": list(outcome.diagnostics),
                    "receipt_corrections": int(
                        outcome.host.get("host_metadata", {}).get(
                            "receipt_corrections", 0
                        )
                    ),
                },
                outcome.host.get("call_id", ""),
            )
    verdict = record["verdict"]
    if verdict.get("stage_status") == "VERIFIER_ERROR":
        return False
    return True


def _commit_application_reviews(root, request, verification_ref):
    """Each scope references the same independently authored manuscript receipt."""
    from pace.artifacts.events import append_event
    from pace.contracts.research.memory import binding, digest

    version = binding(root, verification_ref)
    publication_ref = request.get("publication_ref")
    if publication_ref:
        # Scope pointers bind the immutable joint receipt. A later standalone
        # retry may replace only the manuscript projection.
        path = root / publication_ref
        prior = json.loads(path.read_text()) if path.is_file() else None
        if prior is None or (
            prior.get("joint_receipt_ref") == verification_ref
            and prior["verdict"]["stage_status"] == "VERIFIER_ERROR"
        ):
            receipt = json.loads((root / verification_ref).read_text())
            _write_json_atomic(path, {**receipt, "joint_receipt_ref": verification_ref})
    for item in request["applications"]:
        ref = item["application_ref"]
        link = json.loads((root / ref).read_text()) if (root / ref).is_file() else {}
        current = link.get("verification_binding")
        if current and current["ref"] not in (
            verification_ref,
            request.get("retry_of"),
        ):
            continue  # A more recent scoped repair already owns this pointer.
        if current != version or "pending_request_binding" in link:
            _write_json_atomic(root / ref, {"verification_binding": version})
        append_event(
            root,
            "APPLICATION_REVIEW_COMMITTED",
            idempotency_key="application:" + digest([item["application_key"], version]),
            application_key=item["application_key"],
            review_ref=verification_ref,
            obligation_id=item["obligation_id"],
        )


def _check_group_record(root, record, request_ref, argument):
    from pace.contracts.research.memory import binding, check_binding

    check_binding(root, record["request_binding"])
    for row in record["dependency_bindings"]:
        check_binding(root, row)
    if record.get("review_binding"):
        check_binding(root, record["review_binding"])
    if (
        record["request_ref"] != request_ref
        or record["request_binding"] != binding(root, request_ref)
        or record["source_binding"] != argument
    ):
        raise ValueError("saved review belongs to a different request or manuscript")


def check_applications(*, llm, root, retry_errors=False, findings=None):
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
    restored = set()
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
        # Group membership can change after acceptance but before the Host has
        # projected every scope. Locate the original receipt by the frozen request.
        link_ref = (
            Path(app["result_ref"]).parent / "verification" / f"application-{key}.json"
        )
        if (root / link_ref).is_file():
            pending = read_json(root, str(link_ref)).get("pending_request_binding")
            if pending and pending["ref"] not in restored:
                check_binding(root, pending)
                request = read_json(root, pending["ref"])
                if not any(
                    item["application_key"] == key
                    and item["application_ref"] == str(link_ref)
                    and item["bindings"] == application_review_bindings(app)
                    for item in request["applications"]
                ):
                    raise ValueError("pending review belongs to another application")
                if (root / request["verification_ref"]).is_file():
                    record = read_json(root, request["verification_ref"])
                    _check_group_record(root, record, pending["ref"], app["argument"])
                    _commit_application_reviews(
                        root, request, request["verification_ref"]
                    )
                    restored.add(pending["ref"])
        group = digest([app["target"], app["argument"]])
        groups.setdefault(group, []).append(app)

    return all(
        _check_application_group(
            llm=llm,
            root=root,
            apps=apps,
            retry_errors=retry_errors,
            finding=(findings or {}).get(apps[0]["argument_ref"]),
        )
        for apps in groups.values()
    )


def _check_application_group(
    *, llm, root, apps, retry_errors, finding=None, retry_of=None
):
    from pace.contracts.research.memory import (
        application_review_bindings,
        application_review_incomplete,
        binding,
        check_binding,
        digest,
        load_application_review,
        read_json,
    )

    owner = apps[0]
    directory = Path(owner["result_ref"]).parent / "verification"
    batch_key = digest([app["application_key"] for app in apps])
    if retry_of:
        batch_key = digest([batch_key, retry_of])
    stem = directory / f"finding-{batch_key}"
    request_ref = str(stem.with_suffix(".request.json"))
    request = read_json(root, request_ref) if (root / request_ref).is_file() else None
    record = None
    if request and (root / request["verification_ref"]).is_file():
        record = read_json(root, request["verification_ref"])
        _check_group_record(root, record, request_ref, owner["argument"])
        if record["verdict"].get("stage_status") != "VERIFIER_ERROR" or record[
            "verdict"
        ].get("application_checks"):
            _commit_application_reviews(root, request, request["verification_ref"])
        elif not retry_errors:
            return False
        else:
            history = root / directory / f"finding-{batch_key}.attempts"
            _write_json_atomic(history / f"{uuid.uuid4().hex}.json", record)
            record = None

    pending = []
    repairs = []
    for app in apps:
        reviewed = load_application_review(root, app)
        if reviewed is None:
            pending.append(app)
        elif application_review_incomplete(app, reviewed):
            prior = read_json(root, reviewed["request_ref"]).get(
                "verification_ref", str(Path(reviewed["request_ref"]).with_suffix("").with_suffix(".json"))
            )
            if retry_of or (
                request
                and request["verification_ref"] == prior
                and not reviewed["verdict"].get("application_checks")
            ):
                pending.append(app)
            else:
                repairs.append((app, prior))
    if repairs:
        if not retry_errors:
            return False
        for app, previous in repairs:
            if not _check_application_group(
                llm=llm, root=root, apps=[app], retry_errors=True, retry_of=previous
            ):
                return False
    if not pending:
        return True
    if request is None:
        draft = root / owner["argument_ref"]
        finding_receipt = draft.with_suffix(".verification.json")
        publication = bool(
            finding is not None and not finding_receipt.exists() and not retry_of
        )
        request = {
            "argument_ref": owner["argument_ref"],
            "separate_scope_decisions": True,
            "retry_of": retry_of,
            "review_ref": str(stem.with_suffix(".md")),
            "verification_ref": str(stem.with_suffix(".json")),
            "publication_ref": finding_receipt.relative_to(root).as_posix()
            if publication
            else None,
            "publication_requested": publication,
            "applications": [
                {
                    "application_key": app["application_key"],
                    "obligation_id": app["obligation_id"],
                    "obligation_version": app["obligation_version"],
                    "bindings": application_review_bindings(app),
                    "premise_refs": app["premise_refs"],
                    "application_ref": str(
                        Path(app["result_ref"]).parent
                        / "verification"
                        / f"application-{app['application_key']}.json"
                    ),
                }
                for app in pending
            ],
        }
        refs = finding["refs"] if publication else []
        request["manuscript_inputs"] = list(
            dict.fromkeys(
                r
                for r in refs
                if (root / r).is_file()
                and (root / r).resolve().is_relative_to(root)
                and not is_call_path(root / r)
            )
        )
        if not publication and finding_receipt.is_file():
            previous = json.loads(finding_receipt.read_text())
            if (
                previous.get("source_binding") == owner["argument"]
                and previous["verdict"]["stage_status"] != "VERIFIER_ERROR"
            ):
                request["manuscript_inputs"].append(
                    finding_receipt.relative_to(root).as_posix()
                )
        request["manuscript_bindings"] = [
            binding(root, r)
            for r in request["manuscript_inputs"]
            if not r.startswith(("results/", "memory/")) and "/state/" not in r
        ]
        _write_json_atomic(root / request_ref, request)
    bindings = list(
        {
            digest(row): row
            for item in request["applications"]
            for row in item["bindings"]
        }.values()
    )
    bindings.extend(request.get("manuscript_bindings", []))
    verification_ref = request["verification_ref"]

    frozen_request = binding(root, request_ref)
    for item in request["applications"]:
        ref = item["application_ref"]
        link = read_json(root, ref) if (root / ref).is_file() else {}
        _write_json_atomic(
            root / ref, {**link, "pending_request_binding": frozen_request}
        )
    saved_record = None

    def save_review(verdict):
        nonlocal saved_record
        check_binding(root, frozen_request)
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
            "request_binding": frozen_request,
            "review_binding": binding(root, request["review_ref"])
            if (root / request["review_ref"]).is_file()
            else None,
            "verdict": verdict,
            "separate_scope_decisions": request.get("separate_scope_decisions", False),
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
        input_refs=tuple(
            dict.fromkeys(
                [
                    request_ref,
                    *(r["ref"] for r in bindings),
                    *request.get("manuscript_inputs", []),
                ]
            )
        ),
        round_index=assignment["round_index"],
        target_contract_id=read_json(root, owner["target"]["ref"])["contract_id"],
        application_request=request,
        on_accept=save_review,
    )
    if saved_record is not None:
        record = saved_record
    elif isinstance(outcome, StructuredSemanticResult):
        record = save_review(outcome.payload)
    else:
        record = save_review(
            {"stage_status": "VERIFIER_ERROR", "diagnostics": list(outcome.diagnostics)}
        )
    _commit_application_reviews(root, request, verification_ref)
    return not any(application_review_incomplete(app, record) for app in pending)
