from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from pace.runtime.llm import (
    CodexCallError,
    RETRY_DETERMINISTIC_HOST,
    TransportFailure,
    TransportResult,
)
from pace.runtime.schema import preflight_output_schema


def complete_structured_result(
    llm: Any,
    prompt: str,
    *,
    workdir: Path,
    call_name: str,
    output_schema: dict[str, Any],
    ledger_context: dict[str, Any],
    fatal_scope: str,
) -> TransportResult:
    """Validate the schema before sending the owning semantic call to its provider."""
    scope = fatal_scope.upper()
    if scope not in {"RUN", "CALL"}:
        raise ValueError(f"unsupported fatal_scope: {fatal_scope}")
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
            artifact_dir=workdir,
            fatal_scope=scope,
            authority_evidence={
                "source": "LOCAL_PREFLIGHT",
                "diagnostic_count": len(preflight.diagnostics),
            },
        )
        if scope == "RUN":
            from pace.runtime.sessions import record_model_failure

            record_model_failure(workdir, CodexCallError.from_result(
                result, workdir=workdir, stage=ledger_context["stage"]))
        return result

    result = llm.complete_structured_result(prompt, workdir=workdir, call_name=call_name,
                                            output_schema=output_schema, ledger_context=ledger_context)
    return replace(result, fatal_scope=scope) if isinstance(result, TransportFailure) else result
