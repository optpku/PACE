"""Independently check turn-local findings and applications for the outer publisher."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Iterable

from pace.agents.finding_verifier import verify_finding
from pace.artifacts.results import (
    FINDINGS_CATALOG_REF,
    FINDINGS_STATE_REF,
    committed_finding_refs,
    finding_entries,
    relocate_links,
)
from pace.artifacts.workspace import (
    _write_json_atomic,
    is_call_path,
    is_rci_critic_material,
    work_ref,
)
from pace.runtime.prompts import standard_workflow
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


def _manuscript_sources(
    root: Path, draft_ref: str, declared, *, read_links=True
) -> list[str]:
    """Collect references made by the manuscript or its author, once."""
    from pace.contracts.research.memory import public_file

    refs = []
    published = None

    def include_original(ref):
        if (
            ref == draft_ref
            or is_rci_critic_material(ref)
            or ref.startswith(("memory/", "results/"))
            or "/assignment/" in ref
            or "/state/" in ref
        ):
            return
        try:
            public_file(root, ref)
        except ValueError:
            return
        refs.append(ref)
        if Path(ref).suffix == ".md":
            checked = str(Path(ref).with_suffix(".verification.json"))
            try:
                public_file(root, checked)
            except ValueError:
                return
            refs.append(checked)

    draft = root / draft_ref
    assignment = next(
        (
            folder / "assignment"
            for folder in draft.parents
            if folder.is_relative_to(root)
            and (folder / "assignment/task.json").is_file()
        ),
        None,
    )

    def include(ref):
        nonlocal published
        stable = ref.partition("/assignment/snapshot/")[2] or ref
        if not stable.startswith("results/findings/"):
            include_original(ref)
            return
        snapshot = root / ref if stable != ref else None
        if snapshot is None and assignment:
            candidate = assignment / "snapshot" / stable
            if candidate.is_file():
                snapshot = candidate
        if snapshot:
            # The Host's reader copy links its original and receipt on the last
            # and first lines. Match both to one publication, even after UPDATE.
            lines = (
                public_file(root, snapshot.relative_to(root).as_posix())
                .read_text()
                .splitlines()
            )
            linked = set()

            def collect(path):
                if path.is_relative_to(root):
                    linked.add(path.relative_to(root).as_posix())
                return path, ""

            if lines:
                relocate_links(
                    lines[0] + "\n" + lines[-1], snapshot, snapshot, resolve=collect
                )
            state_path = root / FINDINGS_STATE_REF
            history = (
                json.loads(state_path.read_text())["publications"]
                if state_path.is_file()
                else []
            )
            entry = next(
                (
                    row
                    for row in history
                    if row["ref"] == stable
                    and {row["source_ref"], row["verification_ref"]} <= linked
                ),
                None,
            )
        else:
            if published is None:
                published = {entry["ref"]: entry for entry in finding_entries(root)}
            entry = published.get(stable)
        if entry:
            include_original(entry["source_ref"])
            include_original(entry["verification_ref"])

    for ref in declared:
        include(ref)

    def cited(path):
        if path.is_relative_to(root):
            include(path.relative_to(root).as_posix())
        return path, ""

    if read_links:
        relocate_links(draft.read_text(encoding="utf-8"), draft, draft, resolve=cited)
    return list(dict.fromkeys(refs))


def _checked_premises(root, refs, draft_ref):
    """Project a published Host-bound verdict only for a directly cited source."""
    from pace.contracts.research.memory import check_finding_record, read_json

    available = set(refs)
    checked = {}
    if not (root / FINDINGS_STATE_REF).is_file():
        return checked
    state = read_json(root, FINDINGS_STATE_REF)
    for row in state["publications"]:
        if not row["ref"]:
            continue
        source, receipt = row["source_ref"], row["verification_ref"]
        if source == draft_ref or not {source, receipt} <= available:
            continue
        record = read_json(root, receipt)
        verdict = record.get("verdict", {})
        if (
            record.get("source_binding")
            and verdict.get("stage_status") == "PASS"
            and verdict.get("reuse_authorized") is True
            and verdict.get("summary")
        ):
            check_finding_record(root, source, record["target_contract_id"], record)
            checked[source] = (receipt, verdict["summary"])
    return checked


def _publication_archive(root: Path) -> list[str]:
    return list(
        dict.fromkeys(
            ref
            for ref in (
                FINDINGS_CATALOG_REF,
                *(
                    row[key]
                    for row in finding_entries(root)
                    for key in ("ref", "source_ref", "verification_ref")
                ),
            )
            if (root / ref).is_file() and not is_rci_critic_material(ref)
        )
    )


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

    compact = standard_workflow(root)
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
            if compact:
                draft = root / draft_ref
                dependencies = (
                    _manuscript_sources(root, draft_ref, dependencies or ())
                    if draft.is_file()
                    and not draft.with_suffix(".verification.json").is_file()
                    and not draft.with_suffix(".verification.request.json").is_file()
                    else ()
                )
            elif dependencies is None:
                dependencies = [
                    result["deliverable_ref"],
                    *result.get("published_refs", ()),
                ]
            if (
                not compact
                and (root / draft_ref).is_file()
                and not (root / draft_ref).with_suffix(".verification.json").exists()
            ):
                dependencies = [
                    *dependencies,
                    *_manuscript_sources(root, draft_ref, dependencies),
                ]
            findings[draft_ref]["refs"].extend(
                ref
                for ref in (
                    work_ref(root, "input/problem.md"),
                    work_ref(root, "input/target_contract.json"),
                    *(() if compact else assignment.get("input_refs", ())),
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
    joint_request = {}
    from pace.contracts.research.memory import (
        binding,
        check_finding_record,
    )

    version = binding(root, draft_ref)
    if verification_path.is_file():
        record = json.loads(verification_path.read_text(encoding="utf-8"))
        check_finding_record(root, draft_ref, target_id, record)
        if record.get("verdict", {}).get("stage_status") == "VERIFIER_ERROR":
            if not retry_errors:
                return False
            if record.get("joint_receipt_ref"):
                # check_finding_record authenticated the original joint request and
                # its evidence. Recover its reading set before replacing the projection.
                joint_request = json.loads((root / record["request_ref"]).read_text())
            history = verification_path.parent / (verification_path.stem + ".attempts")
            _write_json_atomic(history / f"{uuid.uuid4().hex}.json", record)
            record = None
    if record is None:
        refs = []
        retry_inputs = joint_request.get(
            "publication_inputs", joint_request.get("manuscript_inputs", context_refs)
        )
        for ref in (draft_ref, *retry_inputs):
            path = (root / ref).resolve()
            if path.is_relative_to(root) and path.is_file() and not is_call_path(path):
                relative = path.relative_to(root).as_posix()
                if relative not in refs:
                    refs.append(relative)
        if not draft.read_text(encoding="utf-8").strip():
            return True

        request_path = draft.with_suffix(".verification.request.json")
        call_options = {}
        if standard_workflow(root):
            if not request_path.is_file():
                if "checked_premises" in joint_request:
                    checked = {
                        source: pair
                        for source, pair in joint_request["checked_premises"].items()
                        if source in refs and pair[0] in refs
                    }
                else:
                    checked = _checked_premises(root, refs, draft_ref)
                _write_json_atomic(
                    request_path,
                    {
                        "argument": version,
                        "checked_premises": checked,
                        "inputs": refs,
                        "bindings": [binding(root, ref) for ref in refs],
                        "archive_refs": joint_request["archive_refs"]
                        if "archive_refs" in joint_request
                        else _publication_archive(root),
                    },
                )
            request = json.loads(request_path.read_text())
            from pace.contracts.research.memory import check_binding

            if request["argument"] != version:
                raise ValueError(
                    "saved finding request binds a different manuscript version"
                )
            for row in request["bindings"]:
                check_binding(root, row)
            refs = request["inputs"]
            call_options = {
                "compact": True,
                "archive_refs": request["archive_refs"],
                "checked_premises": request.get("checked_premises", {}),
            }
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
                **call_options,
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
    from pace.contracts.research.memory import check_binding

    check_binding(root, record["request_binding"])
    for row in record["dependency_bindings"]:
        check_binding(root, row)
    if record.get("review_binding"):
        check_binding(root, record["review_binding"])
    if (
        record["request_ref"] != request_ref
        or record["request_binding"]["ref"] != request_ref
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
            prior = read_json(root, reviewed["request_ref"])["verification_ref"]
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

        compact = standard_workflow(root)
        if compact:
            request["compact_review"] = True
            request["archive_refs"] = _publication_archive(root) if publication else []
        refs = finding["refs"] if publication else []
        if compact and publication:
            request["publication_inputs"] = list(
                dict.fromkeys(r for r in refs if (root / r).is_file())
            )
        shared_refs = set(refs)
        refs = list(refs)
        if not publication:
            cited = _manuscript_sources(root, owner["argument_ref"], ())
            refs.extend(cited)
            shared_refs.update(cited)
        scope_only = set()
        for item in request["applications"]:
            sources = _manuscript_sources(
                root, owner["argument_ref"], item["premise_refs"], read_links=False
            )
            refs.extend(sources)
            scope_only.update(ref for ref in sources if ref not in shared_refs)
            # The premise itself is already frozen in this item's bindings.
            known = shared_refs.union(row["ref"] for row in item["bindings"])
            extra = [binding(root, ref) for ref in sources if ref not in known]
            if extra:
                item["premise_evidence"] = extra
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
                prior_ref = finding_receipt.relative_to(root).as_posix()
                if compact:
                    from pace.contracts.research.memory import check_finding_record

                    check_finding_record(
                        root,
                        owner["argument_ref"],
                        read_json(root, owner["target"]["ref"])["contract_id"],
                        previous,
                    )
                    verdict = previous["verdict"]
                    if (
                        verdict["stage_status"] == "PASS"
                        and verdict.get("reuse_authorized")
                        and verdict.get("summary")
                    ):
                        request["prior_review"] = binding(root, prior_ref)
                        request["checked_statement"] = verdict["summary"]
                    else:
                        request["manuscript_inputs"].append(prior_ref)
                else:
                    request["manuscript_inputs"].append(prior_ref)
        if compact:
            request["checked_premises"] = _checked_premises(
                root, request["manuscript_inputs"], owner["argument_ref"]
            )
        request["manuscript_bindings"] = [
            binding(root, r)
            for r in request["manuscript_inputs"]
            if r not in scope_only
            and not r.startswith(("results/", "memory/"))
            and "/state/" not in r
        ]
        _write_json_atomic(root / request_ref, request)
    bindings = list(
        {
            digest(row): row
            for item in request["applications"]
            for row in (*item["bindings"], *item.get("premise_evidence", ()))
        }.values()
    )
    bindings.extend(request.get("manuscript_bindings", []))
    if request.get("prior_review"):
        bindings.append(request["prior_review"])
    for row in bindings:
        check_binding(root, row)
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
                    *(() if request.get("compact_review") else (request_ref,)),
                    *(r["ref"] for r in bindings if r != request.get("prior_review")),
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
