"""One resumable, non-authoritative progress receipt per mathematical handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pace.agents.director.context import task_context
from pace.artifacts.workspace import (
    _write_json_atomic,
    _write_text_atomic,
    is_rci_critic_material,
    work_ref,
)
from pace.contracts.research.memory import (
    OUTCOMES,
    application_source_error,
    binding,
    check_binding,
    digest,
    prepare_delta_payload,
    read_json,
)
from pace.runtime.json_values import as_dict
from pace.runtime.prompts import render_prompt_template
from pace.runtime.schema import closed_object
from pace.runtime.semantic import (
    SemanticCallSpec,
    SemanticRepresentationFailure,
    StructuredSemanticResult,
    semantic_call,
    validate_semantic_payload,
)


def reporter_schema(request=None):
    text = {"type": "string"}
    refs = {"type": "array", "items": text}

    def reference(allowed, *, optional=False):
        field: dict[str, Any] = {
            "type": "string",
            "description": "One exact workspace-relative file path. No section numbers, Markdown, commentary or multiple paths.",
        }
        if request is not None and (allowed or optional):
            field["enum"] = list(dict.fromkeys([*allowed, *([""] if optional else [])]))
        return field

    originals = (request or {}).get("original_refs", [])
    arguments = (
        [request["candidate_ref"]]
        if request is not None and request.get("candidate_ref")
        else (request or {}).get(
            "manuscript_refs", [r for r in originals if Path(r).suffix == ".md"]
        )
    )
    arguments = [ref for ref in arguments if not application_source_error(ref)]
    dependencies = [
        row["ref"]
        for row in (request or {}).get("bindings", [])
        if not application_source_error(row["ref"])
    ]
    application = closed_object(
        {
            "id": text,
            "obligation_id": (
                {**text, "enum": request["obligation_ids"]}
                if request and request.get("obligation_ids")
                else text
            ),
            "argument_ref": reference(arguments),
            "premise_refs": {"type": "array", "items": reference(dependencies)},
        }
    )
    delta = closed_object(
        {
            "schema_version": {"type": "string", "enum": ["research_delta/v1"]},
            "obligation_ids": (
                {**refs, "enum": [request["obligation_ids"]]}
                if request is not None and "obligation_ids" in request
                else refs
            ),
            "branch_id": (
                {**text, "enum": [request["branch_id"]]}
                if request is not None and "branch_id" in request
                else text
            ),
            "change_ref": reference(
                [request["candidate_ref"]]
                if request and request.get("candidate_ref")
                else originals
            ),
            "applications": {"type": "array", "items": application},
            "attempt": closed_object(
                {
                    "outcome": {"type": "string", "enum": sorted(OUTCOMES)},
                    "obstacle_ref": reference(originals, optional=True),
                    "reopen_condition_ref": reference(originals, optional=True),
                }
            ),
        }
    )
    if request is not None and (
        not arguments or request.get("execution_mode") == "proof"
    ):
        delta["properties"]["applications"]["maxItems"] = 0
        delta["properties"]["attempt"]["properties"]["outcome"]["enum"] = sorted(
            OUTCOMES - {"APPLICATION_PROPOSED"}
        )
    nominations = [
        ref
        for ref in (request or {}).get("manuscript_refs", originals)
        if Path(ref).suffix == ".md" and not is_rci_critic_material(ref)
    ]
    findings = {"type": "array", "items": reference(nominations)}
    if request is not None:
        if not dependencies:
            application["properties"]["premise_refs"]["maxItems"] = 0
        if (
            not nominations
            or request.get("candidate_ref")
            or request.get("execution_mode") == "proof"
        ):
            findings["maxItems"] = 0
    schema = closed_object({"summary": text, "delta": delta, "finding_refs": findings})
    # Nomination is optional when this handoff has no standalone result.
    schema["required"].remove("finding_refs")
    return schema


def _saved_response(root, folder):
    """Reuse a delivered receipt even if validation or the process stopped afterwards."""
    from pace.artifacts.events import read_events

    latest = None
    for event in reversed(read_events(root)):
        if (
            event.get("event_kind") != "SEMANTIC_CALL_STARTED"
            or event.get("role") != "RESEARCH_REPORTER"
        ):
            continue
        record_ref = event.get("call_ref", "")
        if not record_ref or Path(record_ref).parent.parent != folder / "calls":
            continue
        record = json.loads((root / record_ref).read_text())
        # A finished representation failure may have a response that isn't JSON.
        response_ref = record.get("response_ref")
        if (
            record.get("status") == "REPRESENTATION_FAILED"
            and response_ref
            and (root / response_ref).is_file()
        ):
            trace = (root / record_ref).with_name("interface_result.json")
            if trace.is_file():
                payload = json.loads(trace.read_text())["payload"]
                if latest is None:
                    latest = (response_ref, payload)
                if record.get("stage") == f"research_reporter_{folder.name}":
                    return (*latest, payload)
                continue
        # Transport records may not yet have their semantic response_ref committed.
        for response in sorted(
            (root / record_ref).parent.glob("attempts/*/response.txt"), reverse=True
        ):
            try:
                payload = json.loads(response.read_text())
            except (ValueError, OSError):
                continue
            if latest is None:
                latest = (response.relative_to(root).as_posix(), payload)
            if record.get("stage") == f"research_reporter_{folder.name}":
                return (*latest, payload)
            break
    return (*latest, {}) if latest else None


def _preserves_reported_content(
    original: dict, repaired: dict, request: dict, *, applications_forbidden: bool
) -> bool:
    summary = original.get("summary")
    if isinstance(summary, str) and summary.strip() and repaired["summary"] != summary:
        return False
    delta = original.get("delta")
    if not isinstance(delta, dict):
        return True
    repaired_delta = repaired["delta"]
    attempt = delta.get("attempt")
    outcome = as_dict(attempt).get("outcome")
    applications = delta.get("applications")
    fixed = []
    seen = set()
    if not applications_forbidden and isinstance(applications, list):
        for row in applications:
            row = as_dict(row)
            identity = row.get("id")
            obligation = row.get("obligation_id")
            anchor = {}
            if isinstance(identity, str) and identity.strip() and identity not in seen:
                anchor["id"] = identity
                seen.add(identity)
            if obligation in request["obligation_ids"]:
                anchor["obligation_id"] = obligation
            if anchor:
                fixed.append(anchor)
    if (
        isinstance(outcome, str)
        and outcome in OUTCOMES
        and not (
            outcome == "APPLICATION_PROPOSED"
            and isinstance(applications, list)
            and not fixed
        )
        and not (applications_forbidden and outcome == "APPLICATION_PROPOSED")
        and repaired_delta["attempt"]["outcome"] != outcome
    ):
        return False
    if applications_forbidden or not isinstance(applications, list):
        return True
    repaired_applications = repaired_delta["applications"]
    return len(fixed) == len(repaired_applications) and all(
        all(new[field] == value for field, value in anchor.items())
        for anchor, new in zip(fixed, repaired_applications)
    )


def _receipt_diagnostics(payload, schema, obligation_ids):
    errors = validate_semantic_payload(payload, schema)
    value = as_dict(payload)
    delta = as_dict(value.get("delta"))

    def error(pointer, message):
        if not any(row["json_pointer"] == pointer for row in errors):
            errors.append(
                {
                    "reason_code": "reporter_receipt_invalid",
                    "json_pointer": pointer,
                    "message": message,
                }
            )

    summary = value.get("summary")
    if isinstance(summary, str) and not summary.strip():
        error("/summary", "A nonblank progress summary is required.")
    applications = delta.get("applications")
    if not isinstance(applications, list):
        return errors
    if (
        not applications
        and as_dict(delta.get("attempt")).get("outcome") == "APPLICATION_PROPOSED"
    ):
        error(
            "/delta/attempt/outcome",
            "APPLICATION_PROPOSED requires an application in this receipt.",
        )
    seen = set()
    for index, row in enumerate(applications):
        row = as_dict(row)
        pointer = f"/delta/applications/{index}"
        identity = row.get("id")
        has_id = (
            isinstance(identity, str)
            and bool(identity.strip())
            and identity not in seen
        )
        if has_id:
            seen.add(identity)
        if not has_id and row.get("obligation_id") not in obligation_ids:
            error(
                pointer,
                "Omit this proposal: it has neither a usable identity nor an assigned obligation.",
            )
        elif not has_id:
            error(pointer + "/id", "Use a unique nonblank application identity.")
        premises = row.get("premise_refs")
        if isinstance(premises, list) and all(isinstance(ref, str) for ref in premises):
            if len(premises) != len(set(premises)):
                error(pointer + "/premise_refs", "List each premise once.")
    return errors


def restore_reporter_delivery(root: Path, result_ref: str) -> dict:
    """The accepted receipt owns projections, including a crash between their writes."""
    folder = Path(result_ref).parent
    saved = read_json(root, str(folder / "reporter_result.json"))
    if (
        saved["schema_version"] != "research_reporter/v1"
        or saved["result_ref"] != result_ref
    ):
        raise ValueError("Reporter receipt belongs to a different handoff")
    if saved["receipt_id"] != digest(
        {k: v for k, v in saved.items() if k != "receipt_id"}
    ):
        raise ValueError("Reporter receipt changed after acceptance")
    for row in {(r["ref"], r["sha256"]): r for r in saved["bindings"]}.values():
        check_binding(root, row)
    _project_reporter_delivery(root, folder, saved)
    return saved


def _project_reporter_delivery(root: Path, folder: Path, saved: dict) -> None:
    projections = {
        "research_delta.json": json.dumps(
            saved["payload"]["delta"], ensure_ascii=False, indent=2
        )
        + "\n",
        "progress.md": "# Research progress\n\n"
        + saved["payload"]["summary"].strip()
        + "\n",
    }
    for name, content in projections.items():
        path = root / folder / name
        if path.is_symlink():
            raise ValueError(f"unsafe Reporter projection: {path.relative_to(root)}")
        if not path.is_file() or path.read_bytes() != content.replace(
            "\n", os.linesep
        ).encode("utf-8"):
            _write_text_atomic(path, content)


def run_research_reporter(*, llm, workdir: Path, result_ref: str) -> dict | None:
    root = Path(workdir)
    result = read_json(root, result_ref)
    if result.get("status") != "SUCCEEDED" or result["agent_id"] not in {
        "research",
        "prover",
        "rci",
    }:
        return None
    folder = Path(result_ref).parent
    receipt_ref = str(folder / "reporter_result.json")
    if (root / receipt_ref).is_file():
        return restore_reporter_delivery(root, result_ref)
    context = task_context(root, result["deliverable_ref"])
    if not context:
        raise ValueError(
            "Reporter requires the saved task context; do not reconstruct it from the current route"
        )
    request_ref = str(folder / "reporter_request.json")
    if not (root / request_ref).is_file():
        # Read this handoff and its direct dependencies, not the complete historical archive.
        originals = [result["deliverable_ref"], *result.get("published_refs", ())]
        originals = list(
            dict.fromkeys(
                r
                for r in originals
                if (root / r).is_file()
                and Path(r).is_relative_to(folder)
                and not is_rci_critic_material(r)
            )
        )
        required = originals
        if result["agent_id"] == "rci":
            required = [
                r
                for r in originals
                if r == result["deliverable_ref"]
                or Path(r).name in {"change.md", "outcome.json"}
                or r in result.get("manuscript_refs", ())
            ]
        evidence = [
            ref
            for manuscript in result.get("manuscript_refs", ())
            for ref in result.get("manuscript_dependencies", {}).get(manuscript, ())
            if not is_rci_critic_material(ref)
        ]
        from pace.runtime.prompts import standard_workflow

        task_inputs = context["required_refs"]
        optional = list(context["archive_refs"])
        focused = standard_workflow(root)
        route_ref = context["route_context"]["ref"]
        if focused:
            route = read_json(root, context["route_context"]["ref"])
            # Frozen aliases preserve the exact statement/scope dispatched to the author.
            scopes = {
                r[key]
                for r in route["obligations"]
                for key in ("statement_ref", "scope_ref")
                if r.get(key)
            }
            scopes.add(work_ref(root, "input/target_contract.json"))
            task_inputs = [
                row["ref"]
                for row in context["bindings"]
                if row.get("source_ref", row["ref"]) in scopes
            ]
            if context.get("assignment_ref"):
                task_inputs.append(context["assignment_ref"])
            optional.extend((*context["required_refs"], route_ref))
            required = [
                r
                for r in originals
                if r == result["deliverable_ref"]
                or Path(r).parent == folder / "findings"
            ]
            if result["agent_id"] == "rci":
                changes = [ref for ref in originals if Path(ref).name == "change.md"]
                required = [
                    result["deliverable_ref"],
                    *changes[-1:],
                    *(
                        ref
                        for ref in result.get("manuscript_refs", ())
                        if ref in originals
                    ),
                ]
        refs = list(
            dict.fromkeys(
                [
                    result_ref,
                    *required,
                    *(r for r in task_inputs if not is_rci_critic_material(r)),
                    *(() if focused else (route_ref,)),
                ]
            )
        )
        archives = list(
            dict.fromkeys(
                [
                    *(r for r in (*originals, *evidence) if r not in refs),
                    *(
                        r
                        for r in optional
                        if r not in refs and not is_rci_critic_material(r)
                    ),
                ]
            )
        )
        _write_json_atomic(
            root / request_ref,
            {
                "schema_version": "research_reporter_request/v1",
                **(
                    {
                        "focused": True,
                        "review_obligation_ids": route.get("review_obligation_ids", []),
                    }
                    if focused
                    else {}
                ),
                "result_ref": result_ref,
                "candidate_ref": result["deliverable_ref"]
                if result["agent_id"] == "prover"
                else "",
                "execution_mode": read_json(
                    root, work_ref(root, "state/research_execution_policy.json")
                ).get("mode", "research"),
                "obligation_ids": context["obligation_ids"],
                "branch_id": context["branch_id"],
                "original_refs": originals,
                **(
                    {
                        "manuscript_refs": [
                            r for r in result.get("manuscript_refs", ()) if r in originals
                        ]
                    }
                    if result["agent_id"] == "rci" or "manuscript_refs" in result
                    else {}
                ),
                "required_refs": refs,
                "archive_refs": archives,
                "bindings": [
                    binding(root, r) for r in dict.fromkeys((*refs, *archives))
                ],
                "context": binding(root, str(folder / "assignment/context.json")),
            },
        )
    request = read_json(root, request_ref)
    frozen_bindings = {row["ref"]: row for row in request["bindings"]}
    frozen_bindings[request["context"]["ref"]] = request["context"]
    for row in frozen_bindings.values():
        check_binding(root, row)
    frozen = read_json(root, context["route_context"]["ref"])
    obligations = {row["id"]: row for row in frozen["obligations"]}
    schema = reporter_schema(request)
    applications = schema["properties"]["delta"]["properties"]["applications"]
    applications_forbidden = applications.get("maxItems") == 0
    original_payload = None
    accepted = None

    def validate(payload):
        # Report every invalid reference in one correction, rather than stopping
        # at the first failing filesystem lookup. Enums use the frozen manifest.
        errors = _receipt_diagnostics(payload, schema, request["obligation_ids"])
        if errors:
            return errors
        if isinstance(original_payload, dict) and not _preserves_reported_content(
            original_payload,
            payload,
            request,
            applications_forbidden=applications_forbidden,
        ):
            return [
                {
                    "reason_code": "reporter_repair_changed_content",
                    "json_pointer": "/",
                    "message": "Preserve the valid summary, outcome and application identities from the original receipt.",
                }
            ]
        try:
            for row in frozen_bindings.values():
                check_binding(root, row)
            delta = prepare_delta_payload(
                root,
                payload["delta"],
                obligations,
                local_result_ref=result_ref,
                checked_bindings=frozen_bindings,
            )
            if (
                delta["branch_id"] != request["branch_id"]
                or delta["obligation_ids"] != request["obligation_ids"]
            ):
                raise ValueError("retain the assigned branch and obligation identities")
            if delta["change_ref"] not in request["original_refs"]:
                raise ValueError(
                    "change_ref must identify an original mathematical handoff"
                )
            if request["candidate_ref"] and (
                delta["change_ref"] != request["candidate_ref"]
                or any(
                    app["argument_ref"] != request["candidate_ref"]
                    for app in delta["applications"]
                )
            ):
                raise ValueError(
                    "Prover change_ref and application arguments must reference the single complete candidate"
                )
            if any(
                ref not in request.get("manuscript_refs", request["original_refs"])
                or Path(ref).suffix != ".md"
                or is_rci_critic_material(ref)
                for ref in payload.get("finding_refs", ())
            ):
                raise ValueError(
                    "finding_refs must nominate complete original mathematical manuscripts"
                )
            # Reporter must not supply an application proof in its own summary or a control file.
            for app in delta["applications"]:
                if (
                    app["argument_ref"]
                    not in request.get("manuscript_refs", request["original_refs"])
                    or Path(app["argument_ref"]).suffix != ".md"
                ):
                    raise ValueError(
                        "application argument must already exist in the mathematical handoff"
                    )
            return []
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return [
                {
                    "reason_code": "reporter_receipt_invalid",
                    "json_pointer": "/delta",
                    "message": str(exc),
                }
            ]

    def accept(payload):
        # Commit before event publication. A replay only restores these exact projections.
        nonlocal accepted
        delta = payload["delta"]
        if (
            request.get("focused")
            and delta["attempt"]["outcome"] in {"NOT_ESTABLISHED", "CONTEXT_GAP"}
            and not delta["applications"]
            and delta["change_ref"] in payload.get("finding_refs", ())
        ):
            # Keep the incomplete argument as an original. Other standalone
            # manuscripts in the same handoff can still be nominated.
            payload = {
                **payload,
                "finding_refs": [
                    ref for ref in payload["finding_refs"] if ref != delta["change_ref"]
                ],
            }
        saved = {
            "schema_version": "research_reporter/v1",
            "result_ref": result_ref,
            "authority": "SEARCH_CONTEXT_ONLY",
            "payload": payload,
            "bindings": [binding(root, request_ref), *frozen_bindings.values()],
        }
        saved["receipt_id"] = digest(saved)
        _write_json_atomic(root / receipt_ref, saved)
        accepted = saved

    def project_accepted():
        _project_reporter_delivery(root, folder, accepted)
        return accepted

    prompt = build_prompt(workdir=root, request_ref=request_ref, request=request)
    spec = SemanticCallSpec(
        role="RESEARCH_REPORTER",
        semantic_schema=schema,
        host_schema_version="research_reporter/v1",
        authority="SEARCH_CONTEXT_ONLY",
        fatal_scope="CALL",
        validator=validate,
        on_accept=accept,
        retain_schema_rejected_payload=True,
    )
    metadata = {
        "task_id": folder.name + "-reporter",
        "stage_ref": str(folder),
        "archive_refs": [
            *request["archive_refs"],
            request_ref,
            request["context"]["ref"],
        ]
        if request.get("focused")
        else request["archive_refs"],
        "output_refs": [],
        "output_dirs": [],
        "receipt_correction_owner": True,
    }
    saved = _saved_response(root, folder)
    if saved:
        raw_ref, payload, original_payload = saved
        diagnostics = validate(payload)
        if not diagnostics:
            accept(payload)
            return project_accepted()
    else:
        reply = semantic_call(
            llm,
            prompt,
            workdir=root,
            call_name=f"research_reporter_{folder.name}",
            spec=spec,
            input_refs=tuple(request["required_refs"])
            if request.get("focused")
            else (request_ref, request["context"]["ref"], *request["required_refs"]),
            host_metadata=metadata,
        )
        if isinstance(reply, StructuredSemanticResult):
            return project_accepted()
        if not isinstance(reply, SemanticRepresentationFailure) or any(
            row.get("stage") == "acceptance" for row in reply.diagnostics
        ):
            raise RuntimeError(
                "Reporter interrupted; resume without repeating the mathematical author: "
                + json.dumps(list(reply.diagnostics), ensure_ascii=False)
            )
        raw_ref, payload, diagnostics = (
            reply.host["response_ref"],
            reply.payload,
            list(reply.diagnostics),
        )
        original_payload = payload
        for row in _receipt_diagnostics(payload, schema, request["obligation_ids"]):
            if row not in diagnostics:
                diagnostics.append(row)

    # A path correction is a control task. Supply the previous receipt, all
    # diagnostics and exact allowed paths in the schema, not the entire library.
    correction = render_prompt_template(
        "reporter/receipt_correction.md",
        workdir=root,
        receipt_ref=raw_ref,
        branch_id=repr(request["branch_id"]),
        obligation_ids=repr(request["obligation_ids"]),
        proof_constraint=(
            "This handoff requires applications=[]. Remove any inadmissible applications "
            "and replace APPLICATION_PROPOSED with the outcome supported by the handoff. "
        )
        if applications_forbidden
        else "",
        diagnostics=json.dumps(diagnostics, ensure_ascii=False),
        original_payload=json.dumps(original_payload, ensure_ascii=False),
    )
    reply = semantic_call(
        llm,
        correction,
        workdir=root,
        call_name=f"research_reporter_{folder.name}_receipt_correction",
        spec=spec,
        input_refs=(
            raw_ref,
            request_ref,
            *(
                (result["deliverable_ref"],)
                if any(
                    row.get("json_pointer")
                    in {
                        "/",
                        "/summary",
                        "/delta",
                        "/delta/attempt",
                        "/delta/attempt/outcome",
                    }
                    for row in diagnostics
                )
                else ()
            ),
        ),
        host_metadata={
            **metadata,
            "archive_refs": [],
            "read_scope": "declared",
            "receipt_corrections": 1,
        },
    )
    if not isinstance(reply, StructuredSemanticResult):
        raise RuntimeError(
            "Reporter interrupted; resume this handoff without repeating the mathematical author: "
            + json.dumps(list(reply.diagnostics), ensure_ascii=False)
        )
    return project_accepted()


def build_instructions(
    *, workdir: Path, resources: Path, mode: str | None = None
) -> str:
    from pace.agents.prompt_composer import compose_instructions

    return compose_instructions(
        workdir=workdir,
        role="RESEARCH_REPORTER",
        resources=resources,
        refs=("reporter/role.md",),
    )


def build_prompt(*, workdir: Path, request_ref: str, request: dict) -> str:
    if request.get("focused"):
        return render_prompt_template(
            "reporter/focused_task.md",
            workdir=workdir,
            mode=request["execution_mode"],
            branch=request["branch_id"],
            obligations=", ".join(request["obligation_ids"]),
            eligible=", ".join(request["review_obligation_ids"]) or "(none)",
            required_refs="\n".join(f"- `{ref}`" for ref in request["required_refs"]),
            request_ref=request_ref,
        )
    return render_prompt_template(
        "reporter/task.md",
        workdir=workdir,
        request_ref=request_ref,
        required_refs="\n".join(f"- `{ref}`" for ref in request["required_refs"]),
        archive_refs="\n".join(f"- `{ref}`" for ref in request["archive_refs"]),
    )
