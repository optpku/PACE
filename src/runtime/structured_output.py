from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from pace.runtime.llm import (
    CodexCallError,
    RETRY_DETERMINISTIC_HOST,
    TransportFailure,
    TransportResult,
)
from pace.runtime.output.calls import begin_call, finish_call
from pace.runtime.output.events import (
    append_event,
    initialize_events,
    write_call_summary,
)
from pace.runtime.schema import preflight_output_schema


def complete_structured_result(
    llm: Any,
    prompt: str,
    *,
    workdir: Path,
    call_name: str,
    output_schema: dict[str, Any],
    input_refs: Iterable[str | Path],
    ledger_context: dict[str, Any] | None = None,
    fatal_scope: str = "RUN",
) -> TransportResult:
    """Return typed transport truth for one structured semantic call."""

    root = Path(workdir)
    initialize_events(root)
    context = dict(ledger_context or {})
    context["input_refs"] = tuple(input_refs)
    call_id = str(context.get("call_id") or "SC-DIRECT-" + uuid.uuid4().hex)
    role = str(context.get("role") or "direct_structured_output")
    stage = str(context.get("stage") or call_name)
    direct_call = not bool(ledger_context)
    if direct_call:
        context["call_ref"] = begin_call(
            root, call_id=call_id, role=role, stage=stage, prompt=prompt,
            input_refs=context["input_refs"],
        )
        append_event(
            root,
            "SEMANTIC_CALL_STARTED",
            call_id=call_id,
            call_name=call_name,
            role=role,
            stage=stage,
            call_ref=context["call_ref"],
        )
    preflight = preflight_output_schema(output_schema)
    if not preflight.ok:
        result = TransportFailure(
            invocation_id="",
            call_name=call_name,
            error_code="HOST_SCHEMA_CONTRACT_ERROR",
            retry_class=RETRY_DETERMINISTIC_HOST,
            retryable=False,
            exit_code=None,
            terminal_event_type="schema.preflight.failed",
            attempt_count=0,
            diagnostics=preflight.diagnostics,
            artifact_dir=root,
            authority_evidence={
                "source": "LOCAL_PREFLIGHT",
                "diagnostic_count": len(preflight.diagnostics),
            },
        )
        return _finalize_result(
            root,
            result,
            call_id=call_id,
            call_name=call_name,
            role=role,
            stage=stage,
            direct_call=direct_call,
            call_ref=context["call_ref"],
            fatal_scope=fatal_scope,
        )

    result = llm.complete_structured_result(
        prompt,
        workdir=root,
        call_name=call_name,
        output_schema=output_schema,
        ledger_context={
            **context,
            "call_id": call_id,
            "role": role,
            "stage": stage,
        },
    )
    return _finalize_result(
        root,
        result,
        call_id=call_id,
        call_name=call_name,
        role=role,
        stage=stage,
        direct_call=direct_call,
        call_ref=context["call_ref"],
        fatal_scope=fatal_scope,
    )


def _finalize_result(
    root: Path,
    result: TransportResult,
    *,
    call_id: str,
    call_name: str,
    role: str,
    stage: str,
    direct_call: bool,
    call_ref: str,
    fatal_scope: str,
) -> TransportResult:
    scope = str(fatal_scope or "RUN").upper()
    if scope not in {"RUN", "CALL"}:
        raise ValueError(f"unsupported fatal_scope: {fatal_scope}")
    finalized = replace(result, fatal_scope=scope) if isinstance(result, TransportFailure) else result
    if (isinstance(finalized, TransportFailure) and scope == "RUN"
            and finalized.terminal_event_type == "schema.preflight.failed"):
        from pace.runtime.sessions import record_model_failure

        record_model_failure(root, CodexCallError.from_result(finalized, workdir=root, stage=stage))
    if direct_call:
        finish_call(
            root, call_ref,
            status="INTERFACE_FAILED" if isinstance(finalized, TransportFailure) else "SUCCEEDED",
            response_ref=(finalized.artifact_dir / "response.txt").relative_to(root).as_posix() if finalized.attempt_count else "",
            invocation_ids=(finalized.invocation_id,),
        )
        event_kind = "SEMANTIC_CALL_FAILED" if isinstance(finalized, TransportFailure) else "SEMANTIC_CALL_SUCCEEDED"
        append_event(
            root,
            event_kind,
            call_id=call_id,
            call_name=call_name,
            role=role,
            stage=stage,
            invocation_id=str(getattr(finalized, "invocation_id", "") or ""),
            failure_id=str(getattr(finalized, "failure_id", "") or ""),
            root_failure_id=str(getattr(finalized, "root_failure_id", "") or ""),
            error_code=str(getattr(finalized, "error_code", "") or ""),
            retry_class=str(getattr(finalized, "retry_class", "NONE") or "NONE"),
            suppressed=bool(getattr(finalized, "suppressed", False)),
        )
    write_call_summary(root)
    return finalized
