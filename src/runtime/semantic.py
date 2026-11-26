from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from pace.artifacts.calls import begin_call, call_directory, finish_call
from pace.artifacts.events import (
    append_event,
    initialize_events,
    read_events,
    write_call_summary,
)
from pace.artifacts.workspace import (
    _write_json_atomic,
    path_has_symlink_component,
    stage_ref,
)
from pace.runtime.agent_environment import call_environment
from pace.runtime.json_values import as_dict as _as_dict
from pace.runtime.json_values import (
    decode_structured_fields,
    load_model_json,
    unwrap_json_response,
)
from pace.runtime.prompts import render_prompt_template
from pace.runtime.schema import (
    validate_json_instance as _validate_json_instance,
)

# Structured semantic calls


@dataclass(frozen=True)
class SemanticCallSpec:
    role: str
    semantic_schema: dict[str, Any]
    host_schema_version: str
    authority: str
    fatal_scope: str = "RUN"
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    validator: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    on_accept: Callable[[dict[str, Any]], object] | None = None
    retain_schema_rejected_payload: bool = False


@dataclass(frozen=True)
class StructuredSemanticResult:
    payload: dict[str, Any]
    host: dict[str, Any]


@dataclass(frozen=True)
class SemanticRepresentationFailure:
    payload: dict[str, Any]
    host: dict[str, Any]
    diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SemanticInterfaceFailure:
    host: dict[str, Any]
    diagnostics: tuple[dict[str, Any], ...] = ()


SemanticCallFailure = Union[SemanticRepresentationFailure, SemanticInterfaceFailure]
SemanticCallResult = Union[StructuredSemanticResult, SemanticCallFailure]


@dataclass(frozen=True)
class ArtifactCallHandle:
    """Transport result awaiting file-authoritative role acceptance."""

    call_id: str
    call_name: str
    role: str
    stage: str
    call_ref: str
    response_ref: str
    invocation_id: str
    transport_status: str
    error_code: str = ""
    retry_class: str = "NONE"
    suppressed: bool = False


def begin_artifact_call(
    llm: Any,
    prompt: str,
    *,
    workdir: Path,
    call_name: str,
    role: str,
    stage: str,
    input_refs: Iterable[str | Path],
    metadata: dict[str, Any] | None = None,
    on_started: Callable[[str], None] | None = None,
    on_transport_success: Callable[[ArtifactCallHandle], None] | None = None,
) -> ArtifactCallHandle:
    """Run transport only; the artifact owner must commit one terminal outcome."""

    from pace.runtime.llm import TransportFailure

    root = Path(workdir)
    input_refs = tuple(str(item) for item in input_refs)
    initialize_events(root)
    call_id = "SC-ARTIFACT-" + uuid.uuid4().hex
    call_ref = begin_call(
        root,
        call_id=call_id,
        role=role,
        stage=stage,
        prompt=prompt,
        input_refs=input_refs,
        metadata=call_environment(root, role, metadata),
    )
    if on_started is not None:
        on_started(call_ref)
    append_event(
        root,
        "SEMANTIC_CALL_STARTED",
        call_id=call_id,
        call_name=call_name,
        role=role,
        stage=stage,
        call_ref=call_ref,
        prompt_bytes=len(prompt.encode("utf-8")),
        suggested_input_bytes=sum(
            (root / ref).stat().st_size for ref in input_refs if (root / ref).is_file()
        ),
        task_id=str((metadata or {}).get("task_id") or ""),
        parent_task_id=str((metadata or {}).get("parent_task_id") or ""),
    )
    result = llm.complete_unstructured_result(
        prompt,
        workdir=root,
        call_name=call_name,
        ledger_context={
            "call_id": call_id,
            "call_ref": call_ref,
            "role": role,
            "stage": stage,
            "input_refs": input_refs,
            "archive_refs": (metadata or {}).get("archive_refs", ()),
            "read_scope": (metadata or {}).get("read_scope", ""),
            "protected_input_refs": (metadata or {}).get("protected_input_refs", ()),
            "inline_task": (metadata or {}).get("inline_task") is True,
            "output_refs": (metadata or {}).get(
                "output_refs", [(metadata or {}).get("output_ref", "")]
            ),
            "output_dirs": (metadata or {}).get("output_dirs", ()),
        },
    )
    failed = isinstance(result, TransportFailure)
    handle = ArtifactCallHandle(
        call_id=call_id,
        call_name=call_name,
        role=role,
        stage=stage,
        call_ref=call_ref,
        response_ref=(Path(result.artifact_dir) / "response.txt")
        .relative_to(root)
        .as_posix(),
        invocation_id=str(getattr(result, "invocation_id", "") or ""),
        transport_status="FAILED" if failed else "SUCCESS",
        error_code=str(getattr(result, "error_code", "") or ""),
        retry_class=str(getattr(result, "retry_class", "NONE") or "NONE"),
        suppressed=bool(getattr(result, "suppressed", False)),
    )
    if on_transport_success is not None and not failed:
        on_transport_success(handle)
    return handle


def commit_artifact_call_success(
    handle: ArtifactCallHandle,
    *,
    workdir: Path,
    artifact_ref: str,
    artifact_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if handle.transport_status != "SUCCESS":
        raise ValueError(
            "transport-failed artifact call cannot be committed as success"
        )
    return _commit_artifact_terminal(
        workdir,
        handle,
        event_kind="SEMANTIC_CALL_SUCCEEDED",
        terminal_outcome="SUCCEEDED",
        artifact_ref=artifact_ref,
        artifact_id=artifact_id,
        metadata=metadata,
    )


def commit_artifact_call_failure(
    handle: ArtifactCallHandle,
    *,
    workdir: Path,
    failure_class: str,
    retry_class: str,
    diagnostics_ref: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failure = str(failure_class or "INTERFACE").upper()
    outcome = {
        "REPRESENTATION": "REPRESENTATION_FAILED",
        "DOMAIN": "DOMAIN_REJECTED",
        "INTERFACE": "INTERFACE_FAILED",
    }.get(failure)
    if outcome is None:
        raise ValueError(f"unsupported artifact failure class: {failure_class}")
    return _commit_artifact_terminal(
        workdir,
        handle,
        event_kind="SEMANTIC_CALL_FAILED",
        terminal_outcome=outcome,
        failure_class=failure,
        retry_class=str(retry_class or handle.retry_class or "NONE"),
        diagnostics_ref=diagnostics_ref,
        metadata=metadata,
    )


def _commit_artifact_terminal(
    workdir: Path,
    handle: ArtifactCallHandle,
    *,
    event_kind: str,
    terminal_outcome: str,
    artifact_ref: str = "",
    artifact_id: str = "",
    failure_class: str = "",
    retry_class: str = "NONE",
    diagnostics_ref: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(workdir)
    finish_call(
        root,
        handle.call_ref,
        status=terminal_outcome,
        response_ref=handle.response_ref,
        deliverable_ref=artifact_ref,
        invocation_ids=(handle.invocation_id,),
        error={"failure_class": failure_class, "diagnostics_ref": diagnostics_ref}
        if failure_class
        else {},
        terminal_metadata=metadata,
    )
    # The durable owner commit is authoritative. A display/event projection
    # failure after this point must not turn accepted writing into a new call.
    event = {}
    try:
        event = append_event(
            root,
            event_kind,
            call_id=handle.call_id,
            call_name=handle.call_name,
            role=handle.role,
            stage=handle.stage,
            invocation_id=handle.invocation_id,
            terminal_outcome=terminal_outcome,
            failure_class=failure_class,
            retry_class=retry_class,
            error_code=handle.error_code,
            suppressed=handle.suppressed,
            artifact_refs={
                "authority_artifact": str(artifact_ref or ""),
                "authority_artifact_id": str(artifact_id or ""),
                "diagnostics": str(diagnostics_ref or ""),
            },
            metadata=dict(metadata or {}),
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "Committed call event projection failed", exc_info=True
        )
    try:
        write_call_summary(root)
    except Exception:
        logging.getLogger(__name__).warning(
            "Committed call summary projection failed", exc_info=True
        )
    return event


def semantic_call(
    llm: Any,
    prompt: str,
    *,
    workdir: Path,
    call_name: str,
    spec: SemanticCallSpec,
    input_refs: Iterable[str | Path],
    host_metadata: dict[str, Any] | None = None,
) -> SemanticCallResult:
    """Correct one incomplete control response without restarting its task."""
    inputs = tuple(input_refs)
    result = _semantic_call_once(
        llm,
        prompt,
        workdir=workdir,
        call_name=call_name,
        spec=spec,
        input_refs=inputs,
        host_metadata=host_metadata,
    )
    if (host_metadata or {}).get("receipt_correction_owner"):
        return result
    if not isinstance(result, SemanticRepresentationFailure) or any(
        row.get("stage") == "acceptance" for row in result.diagnostics
    ):
        return result
    from pace.runtime.sessions import budget_exhausted

    if budget_exhausted(Path(workdir)):
        return result
    raw_ref = str(result.host["response_ref"])
    correction = render_prompt_template(
        "shared/receipt_correction.md",
        workdir=workdir,
        receipt_ref=raw_ref,
        diagnostics=json.dumps(list(result.diagnostics), ensure_ascii=False),
    )
    return _semantic_call_once(
        llm,
        correction,
        workdir=workdir,
        call_name=call_name + "_receipt_correction",
        spec=spec,
        input_refs=(*inputs, raw_ref),
        host_metadata={
            **dict(host_metadata or {}),
            "parent_call_id": result.host["call_id"],
            "representation_correction_of": result.host["call_id"],
            "receipt_corrections": 1,
            "output_refs": [],
            "output_dirs": [],
        },
    )


def _semantic_call_once(
    llm: Any,
    prompt: str,
    *,
    workdir: Path,
    call_name: str,
    spec: SemanticCallSpec,
    input_refs: Iterable[str | Path],
    host_metadata: dict[str, Any] | None = None,
) -> SemanticCallResult:
    """Execute exactly one model call for one semantic task."""

    from pace.runtime.llm import (
        RETRY_DETERMINISTIC_HOST,
        RETRY_MODEL_REPRESENTATION,
        TransportFailure,
    )
    from pace.runtime.structured_output import complete_structured_result
    from pace.runtime.llm import recover_pending_completion

    root = Path(workdir)
    allowed_inputs = tuple(str(item) for item in input_refs)
    recovered = recover_pending_completion(
        root,
        prompt=prompt,
        call_name=call_name,
        role=spec.role,
        input_refs=allowed_inputs,
        metadata=host_metadata,
        output_schema=spec.semantic_schema,
    )
    call_id = recovered[0] if recovered else uuid.uuid4().hex
    call_ref = (
        recovered[1]
        if recovered
        else begin_call(
            root,
            call_id=call_id,
            role=spec.role,
            stage=call_name,
            prompt=prompt,
            input_refs=allowed_inputs,
            metadata=call_environment(root, spec.role, host_metadata),
        )
    )
    initialize_events(root)
    if not recovered:
        append_event(
            root,
            "SEMANTIC_CALL_STARTED",
            call_id=call_id,
            call_name=call_name,
            role=spec.role,
            stage=call_name,
            call_ref=call_ref,
            prompt_bytes=len(prompt.encode("utf-8")),
            suggested_input_bytes=sum(
                (root / ref).stat().st_size
                for ref in allowed_inputs
                if (root / ref).is_file()
            ),
            task_id=str((host_metadata or {}).get("task_id") or ""),
            parent_task_id=str((host_metadata or {}).get("parent_task_id") or ""),
        )
    trace_dir = call_directory(root, call_ref)
    diagnostics: list[dict[str, Any]] = []
    invocation_ids: list[str] = []
    payload: dict[str, Any] = {}
    acceptance_failed = False
    transport = (
        recovered[2]
        if recovered
        else complete_structured_result(
            llm,
            str(prompt or ""),
            workdir=root,
            call_name=call_name,
            output_schema=spec.semantic_schema,
            ledger_context={
                "input_refs": allowed_inputs,
                "call_id": call_id,
                "call_ref": call_ref,
                "role": spec.role,
                "stage": call_name,
                "archive_refs": (host_metadata or {}).get("archive_refs", ()),
                "read_scope": (host_metadata or {}).get("read_scope", ""),
                "protected_input_refs": (host_metadata or {}).get(
                    "protected_input_refs", ()
                ),
                "output_refs": (host_metadata or {}).get(
                    "output_refs", [(host_metadata or {}).get("output_ref", "")]
                ),
                "output_dirs": (host_metadata or {}).get("output_dirs", ()),
            },
            fatal_scope=spec.fatal_scope,
        )
    )
    if transport.invocation_id:
        invocation_ids.append(transport.invocation_id)
    transport_metadata = {
        "invocation_id": transport.invocation_id,
        "status": "FAILED" if isinstance(transport, TransportFailure) else "SUCCESS",
        "terminal_event_type": transport.terminal_event_type,
        "error_code": str(getattr(transport, "error_code", "") or ""),
        "retry_class": str(getattr(transport, "retry_class", "NONE") or "NONE"),
        "retryable": bool(getattr(transport, "retryable", False)),
        "fatal_scope": str(getattr(transport, "fatal_scope", "") or ""),
        "output_schema_valid": transport.output_schema_valid,
        "attempt_count": transport.attempt_count,
        "warnings": list(transport.warnings),
        "authority_evidence": dict(transport.authority_evidence),
    }
    raw = transport.text
    if isinstance(transport, TransportFailure):
        diagnostics.append(
            {
                "stage": "transport",
                "attempt": 1,
                "error_code": transport.error_code,
                "retry_class": transport.retry_class,
                "retryable": transport.retryable,
                "terminal_event_type": transport.terminal_event_type,
                "diagnostics": list(transport.diagnostics),
            }
        )
        representation_failed = transport.retry_class == RETRY_MODEL_REPRESENTATION
        if (
            spec.retain_schema_rejected_payload
            and representation_failed
            and transport.error_code == "output_instance_schema_invalid"
        ):
            # Keep parseable fields for the role's correction. A transport
            # rejection still cannot enter the ordinary acceptance path.
            try:
                decoded, _ = load_model_json(unwrap_json_response(raw))
                payload, _ = decode_structured_fields(decoded, spec.semantic_schema)
            except ValueError:
                pass
    else:
        try:
            payload, repairs = decode_structured_fields(
                json.loads(raw), spec.semantic_schema
            )
            if repairs:
                _write_json_atomic(
                    trace_dir / "receipt_normalization.json",
                    {
                        "json_pointers": repairs,
                        "response_ref": (Path(transport.artifact_dir) / "response.txt")
                        .relative_to(root)
                        .as_posix(),
                    },
                )
            diagnostics.extend(validate_semantic_payload(payload, spec.semantic_schema))
            if not diagnostics and spec.normalizer is not None:
                payload = spec.normalizer(payload)
                diagnostics.extend(
                    validate_semantic_payload(payload, spec.semantic_schema)
                )
            if not diagnostics and spec.validator is not None:
                diagnostics.extend(spec.validator(payload))
        except Exception as exc:
            payload = {}
            diagnostics.append(
                {
                    "stage": "parse",
                    "attempt": 1,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        representation_failed = bool(diagnostics)
        if not diagnostics and spec.on_accept is not None:
            try:
                spec.on_accept(payload)
            except Exception as exc:
                # A failed Host commit cannot be repaired by another model call.
                acceptance_failed = True
                diagnostics.append(
                    {
                        "stage": "acceptance",
                        "reason_code": "host_acceptance_failed",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )

    response_ref = (
        (Path(transport.artifact_dir) / "response.txt").relative_to(root).as_posix()
    )
    if not isinstance(transport, TransportFailure) and not diagnostics:
        host = {
            "schema_version": spec.host_schema_version,
            "authority": spec.authority,
            "call_id": call_id,
            "call_ref": call_ref,
            "response_ref": response_ref,
            "invocation_ids": invocation_ids,
            "interface_status": "OK",
            "semantic_status": "PASS",
            "retry_owner": "NONE",
            "stage": call_name,
            "host_metadata": {
                "transport": transport_metadata,
                **dict(host_metadata or {}),
            },
        }
        _write_json_atomic(trace_dir / "interface_result.json", {"host": host, "diagnostics": []})
        finish_call(
            root,
            call_ref,
            status="SUCCEEDED",
            response_ref=response_ref,
            deliverable_ref=str((host_metadata or {}).get("output_ref") or ""),
            invocation_ids=invocation_ids,
        )
        append_event(
            root,
            "SEMANTIC_CALL_SUCCEEDED",
            call_id=call_id,
            call_name=call_name,
            role=spec.role,
            stage=call_name,
            invocation_id=transport.invocation_id,
            suppressed=False,
        )
        write_call_summary(root)
        return StructuredSemanticResult(payload=payload, host=host)

    host = {
        "schema_version": spec.host_schema_version,
        "authority": spec.authority,
        "call_id": call_id,
        "call_ref": call_ref,
        "response_ref": response_ref,
        "invocation_ids": invocation_ids,
        "interface_status": "OK" if representation_failed else "FAILED",
        "semantic_status": "FAILED" if representation_failed else "NOT_EVALUATED",
        "retry_owner": (
            "HOST"
            if acceptance_failed
            else "HOST_SCHEMA"
            if transport_metadata.get("status") == "FAILED"
            and any(
                row.get("retry_class") == RETRY_DETERMINISTIC_HOST
                for row in diagnostics
            )
            else "TRANSPORT"
            if not representation_failed
            else "NONE"
        ),
        "stage": call_name,
        "host_metadata": {
            "attempt_count": 1,
            "transport": transport_metadata,
            "failure_id": str(getattr(transport, "failure_id", "") or ""),
            "root_failure_id": str(getattr(transport, "root_failure_id", "") or ""),
            "fatal_scope": str(getattr(transport, "fatal_scope", "") or ""),
            "suppressed": bool(getattr(transport, "suppressed", False)),
            "transport_attempt_count": int(getattr(transport, "attempt_count", 0) or 0),
            **dict(host_metadata or {}),
        },
    }
    # A failed handoff may need its decoded fields to resume a local correction.
    # Successful calls already committed the accepted receipt in their owning stage.
    trace = {"host": host, "diagnostics": diagnostics}
    if representation_failed or acceptance_failed:
        trace["payload"] = payload
    _write_json_atomic(trace_dir / "interface_result.json", trace)
    finish_call(
        root,
        call_ref,
        status="REPRESENTATION_FAILED" if representation_failed else "INTERFACE_FAILED",
        response_ref=response_ref,
        invocation_ids=invocation_ids,
        error={"diagnostics": diagnostics},
    )
    append_event(
        root,
        "SEMANTIC_CALL_FAILED",
        call_id=call_id,
        call_name=call_name,
        role=spec.role,
        stage=call_name,
        invocation_id=str(getattr(transport, "invocation_id", "") or ""),
        failure_id=str(_as_dict(host.get("host_metadata")).get("failure_id") or ""),
        root_failure_id=str(
            _as_dict(host.get("host_metadata")).get("root_failure_id") or ""
        ),
        error_code=(
            "HOST_ACCEPTANCE_FAILED"
            if acceptance_failed
            else str(
                getattr(transport, "error_code", "") or "SEMANTIC_VALIDATION_FAILED"
            )
            if representation_failed
            else str(getattr(transport, "error_code", "") or "")
        ),
        retry_class=(
            RETRY_DETERMINISTIC_HOST
            if acceptance_failed
            else RETRY_MODEL_REPRESENTATION
            if representation_failed
            else str(getattr(transport, "retry_class", "") or "")
        ),
        suppressed=bool(_as_dict(host.get("host_metadata")).get("suppressed")),
    )
    write_call_summary(root)
    if representation_failed:
        return SemanticRepresentationFailure(
            payload=_as_dict(payload),
            host=host,
            diagnostics=tuple(diagnostics),
        )
    return SemanticInterfaceFailure(host=host, diagnostics=tuple(diagnostics))


def recover_rejected_receipt(
    root: Path,
    *,
    call_name: str,
    role: str,
    metadata: dict[str, Any],
    binding_fields: tuple[str, ...],
) -> SemanticRepresentationFailure | None:
    """Resume correction from the latest rejected receipt bound to this task."""
    stage = metadata.get("stage_ref") or stage_ref(
        int(metadata.get("round_index") or 0), workdir=root
    )
    calls = Path(stage) / "calls"
    if not (root / calls).is_dir():
        return None
    for event in reversed(read_events(root)):
        if (
            event.get("event_kind") != "SEMANTIC_CALL_STARTED"
            or event.get("role") != role
            or event.get("call_name") != call_name
        ):
            continue
        record_ref = Path(event.get("call_ref") or "")
        if record_ref.parent.parent != calls:
            continue
        call_id = event.get("call_id")
        record_path = root / record_ref
        trace_path = record_path.with_name("interface_result.json")
        if (
            record_ref.name != "record.json"
            or record_ref.parent.name != call_id
            or ".." in record_ref.parts
            or any(
                path_has_symlink_component(root, path)
                for path in (record_path, trace_path)
            )
            or not record_path.is_file()
            or not trace_path.is_file()
        ):
            return None
        record = json.loads(record_path.read_text(encoding="utf-8"))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        host = trace["host"]
        previous = host["host_metadata"]
        response = Path(host["response_ref"])
        if (
            record.get("status") not in {"STARTED", "REPRESENTATION_FAILED"}
            or host.get("interface_status") != "OK"
            or host.get("semantic_status") != "FAILED"
            or host.get("retry_owner") == "HOST"
            or host.get("call_id") != call_id
            or host.get("call_ref") != record_ref.as_posix()
            or any(previous.get(key) != metadata[key] for key in binding_fields)
            or not response.is_relative_to(record_ref.parent / "attempts")
            or ".." in response.parts
            or path_has_symlink_component(root, root / response)
            or not (root / response).is_file()
        ):
            return None  # An older attempt cannot replace the latest one.
        if record["status"] == "STARTED":
            finish_call(
                root,
                record_ref.as_posix(),
                status="REPRESENTATION_FAILED",
                response_ref=response.as_posix(),
                invocation_ids=host["invocation_ids"],
                error={"diagnostics": trace["diagnostics"]},
            )
        elif record.get("response_ref") != response.as_posix():
            return None
        return SemanticRepresentationFailure(
            payload=_as_dict(trace["payload"]),
            host=host,
            diagnostics=tuple(trace["diagnostics"]),
        )
    return None


def validate_semantic_payload(
    value: Any, schema: dict[str, Any], *, pointer: str = ""
) -> list[dict[str, Any]]:
    """Validate the JSON-Schema subset used by model semantic contracts."""
    return _validate_json_instance(value, schema, pointer=pointer)


def interface_failure_payload(
    result: SemanticInterfaceFailure, *, stage: str
) -> dict[str, Any]:
    return {
        "host": result.host,
        "stage": stage,
        "diagnostics": list(result.diagnostics),
        "schema_valid": False,
        "interface_status": "FAILED",
    }


def representation_failure_payload(
    result: SemanticRepresentationFailure,
    *,
    stage: str,
) -> dict[str, Any]:
    return {
        "host": result.host,
        "stage": stage,
        "diagnostics": list(result.diagnostics),
        "schema_valid": False,
        "interface_status": "OK",
        "semantic_status": "FAILED",
        "failure_owner": "MODEL_REPRESENTATION",
        "receipt_corrections": int(
            result.host.get("host_metadata", {}).get("receipt_corrections", 0)
        ),
    }


def diagnostic_messages(value: Any) -> list[str]:
    """Render persisted failure fields without exposing prompts or host internals."""
    if isinstance(value, (list, tuple)):
        return list(
            dict.fromkeys(
                message for row in value for message in diagnostic_messages(row)
            )
        )
    if not isinstance(value, dict):
        return []
    messages = []
    detail = (
        value.get("message")
        or value.get("reason")
        or value.get("expected")
        or value.get("reason_code")
    )
    if value.get("missing"):
        detail = "missing: " + ", ".join(value["missing"])
    if detail:
        label = " ".join(
            str(value.get(key) or "") for key in ("phase", "stage", "json_pointer")
        ).strip()
        messages.append((label + ": " if label else "") + str(detail))
    for key in (
        "diagnostics",
        "interface_failure",
        "semantic_failure",
        "verification_interface_failures",
    ):
        messages.extend(diagnostic_messages(value.get(key)))
    if value.get("receipt_corrections"):
        messages.append(f"局部回执纠正已用 {value['receipt_corrections']}/1")
    return list(dict.fromkeys(messages))
