from __future__ import annotations

import errno
import json
import logging
import os
import random
import re
import subprocess
import time
import uuid
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, Union

from pace.runtime.output.calls import (
    attempt_directory,
    begin_call,
    call_directory,
    finish_call,
)
from pace.runtime.codex_config import (
    isolated_codex_args,
    isolated_codex_environment,
    retry_codex_args,
)
from pace.runtime.output.events import (
    append_event,
    initialize_events,
    record_transport_attempt_finished,
    record_transport_attempt_started,
    read_events,
    write_call_summary,
)
from pace.runtime.output.workspace import (
    _write_json_atomic,
    _write_text_atomic,
    work_ref,
)
from pace.runtime.processes import run_owned_process
from pace.runtime.schema import validate_json_instance
from pace.runtime.json_values import load_model_json

RETRY_DETERMINISTIC_HOST = "DETERMINISTIC_HOST_CONTRACT"
RETRY_CONFIGURATION = "NON_RETRYABLE_CONFIGURATION"
RETRY_TRANSIENT = "TRANSIENT_EXECUTION"
RETRY_NON_TRANSIENT = "NON_RETRYABLE_TRANSPORT"
RETRY_MODEL_REPRESENTATION = "MODEL_REPRESENTATION"
MAX_TRANSPORT_RETRIES = 2
# Formatting is a bounded representation task. Neither the global effort nor
# user/provider configuration can override this role's policy.
_FIXED_ROLE_EFFORT = {"ARTIFACT_FORMAT_REPAIR": "medium", "FINDING_MANIFEST_REPAIR": "medium"}
RETRY_BACKOFF_BASE_SECONDS = 5.0
RETRY_BACKOFF_MAX_SECONDS = 60.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransportSuccess:
    text: str
    invocation_id: str
    call_name: str
    exit_code: int | None
    terminal_event_type: str
    accepted_output_source: str
    output_schema_valid: bool | None
    attempt_count: int
    artifact_dir: Path
    warnings: tuple[dict[str, Any], ...] = ()
    authority_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransportFailure:
    invocation_id: str
    call_name: str
    error_code: str
    retry_class: str
    retryable: bool
    exit_code: int | None
    terminal_event_type: str
    attempt_count: int
    diagnostics: tuple[dict[str, Any], ...]
    artifact_dir: Path
    timed_out: bool = False
    failure_id: str = ""
    root_failure_id: str = ""
    fatal_scope: str = ""
    suppressed: bool = False
    output_schema_valid: bool | None = None
    warnings: tuple[dict[str, Any], ...] = ()
    authority_evidence: dict[str, Any] = field(default_factory=dict)
    # Rejected current output is diagnostic input for semantic JSON correction.
    text: str = ""


TransportResult = Union[TransportSuccess, TransportFailure]


@dataclass(frozen=True)
class CodexEventTimeline:
    final_terminal_kind: str
    final_terminal_index: int | None
    final_terminal_event: dict[str, Any]
    assistant_message: str
    recoverable_error_events: tuple[dict[str, Any], ...]
    terminal_failure_events: tuple[dict[str, Any], ...]
    usage: dict[str, int]
    invalid_jsonl_line_count: int = 0

    @property
    def terminal_success(self) -> bool:
        return self.final_terminal_kind == "SUCCESS"

    @property
    def terminal_error(self) -> bool:
        return self.final_terminal_kind == "FAILURE"

    @property
    def terminal_event_type(self) -> str:
        return str(self.final_terminal_event.get("type") or "")

    @property
    def error_code(self) -> str:
        return (
            _error_from_event(self.final_terminal_event)[0]
            if self.terminal_error
            else ""
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "final_terminal_kind": self.final_terminal_kind,
            "final_terminal_index": self.final_terminal_index,
            "final_terminal_event": self.final_terminal_event,
            "assistant_message_present": bool(self.assistant_message),
            "recoverable_error_events": list(self.recoverable_error_events),
            "terminal_failure_events": list(self.terminal_failure_events),
            "usage": dict(self.usage),
            "invalid_jsonl_line_count": self.invalid_jsonl_line_count,
        }


class CodexCallError(RuntimeError):
    def __init__(
        self,
        *,
        call_name: str,
        exit_code: int | None,
        workdir: Path,
        calls_dir: Path,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        transient_classification: str = "unknown",
        error_code: str = "",
        retryable: bool = False,
        invocation_id: str = "",
        message: str = "",
        stage: str = "",
        attempt_count: int = 0,
    ) -> None:
        self.call_name = call_name
        self.exit_code = exit_code
        self.workdir = workdir
        self.calls_dir = calls_dir
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.transient_classification = transient_classification
        self.error_code = error_code
        self.retryable = bool(retryable)
        self.invocation_id = invocation_id
        detail = message or f"codex call {call_name!r} failed"
        self.detail = detail
        self.stage = stage or call_name
        self.attempt_count = attempt_count
        super().__init__(
            f"{detail}; call_name={call_name}; exit_code={exit_code}; "
            f"transient_classification={transient_classification}; calls_dir={calls_dir}"
        )

    def to_json(self) -> dict[str, str | int | None]:
        return {
            "type": type(self).__name__,
            "call_name": self.call_name,
            "exit_code": self.exit_code,
            "workdir": str(self.workdir),
            "calls_dir": str(self.calls_dir),
            "stdout_path": str(self.stdout_path) if self.stdout_path else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path else None,
            "transient_classification": self.transient_classification,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "invocation_id": self.invocation_id,
            "message": self.detail,
            "stage": self.stage,
            "attempt_count": self.attempt_count,
        }

    @classmethod
    def from_result(cls, result: TransportFailure, *, workdir: Path, stage=""):
        return cls(
            call_name=result.call_name, exit_code=result.exit_code,
            workdir=workdir, calls_dir=result.artifact_dir,
            stdout_path=result.artifact_dir / "stdout.jsonl" if result.attempt_count else None,
            stderr_path=result.artifact_dir / "stderr.txt" if result.attempt_count else None,
            transient_classification=result.retry_class, error_code=result.error_code,
            retryable=result.retryable, invocation_id=result.invocation_id,
            message=result.diagnostics[0].get("message") or json.dumps(result.diagnostics[0], ensure_ascii=False)
            if result.diagnostics else result.error_code,
            stage=stage, attempt_count=result.attempt_count,
        )


class LLM(Protocol):
    def complete(self, prompt: str, *, workdir: Path, call_name: str) -> str: ...

    def complete_unstructured_result(
        self,
        prompt: str,
        *,
        workdir: Path,
        call_name: str,
        ledger_context: dict[str, Any],
    ) -> TransportResult: ...

    def complete_structured_result(
        self,
        prompt: str,
        *,
        workdir: Path,
        call_name: str,
        output_schema: dict[str, Any],
        ledger_context: dict[str, Any],
    ) -> TransportResult: ...


@dataclass
class CodexLLM:
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "xhigh"
    cli_path: str = "codex"
    web_search: bool = False
    timeout_seconds: int | None = None
    max_retries: int = MAX_TRANSPORT_RETRIES

    def complete(self, prompt: str, *, workdir: Path, call_name: str) -> str:
        root = Path(workdir)
        initialize_events(root)
        call_id = "SC-UNSTRUCTURED-" + uuid.uuid4().hex
        call_ref = begin_call(
            root,
            call_id=call_id,
            role="unstructured_model_call",
            stage=call_name,
            prompt=prompt,
        )
        append_event(
            root,
            "SEMANTIC_CALL_STARTED",
            call_id=call_id,
            call_name=call_name,
            role="unstructured_model_call",
            stage=call_name,
        )
        result = self.complete_unstructured_result(
            prompt,
            workdir=root,
            call_name=call_name,
            ledger_context={
                "call_id": call_id,
                "call_ref": call_ref,
                "role": "unstructured_model_call",
                "stage": call_name,
                "input_refs": (),
            },
        )
        append_event(
            root,
            "SEMANTIC_CALL_FAILED"
            if isinstance(result, TransportFailure)
            else "SEMANTIC_CALL_SUCCEEDED",
            call_id=call_id,
            call_name=call_name,
            role="unstructured_model_call",
            stage=call_name,
            invocation_id=result.invocation_id,
            failure_id=str(getattr(result, "failure_id", "") or ""),
            root_failure_id=str(getattr(result, "root_failure_id", "") or ""),
            error_code=str(getattr(result, "error_code", "") or ""),
            retry_class=str(getattr(result, "retry_class", "NONE") or "NONE"),
            suppressed=bool(getattr(result, "suppressed", False)),
        )
        finish_call(
            root,
            call_ref,
            status="INTERFACE_FAILED"
            if isinstance(result, TransportFailure)
            else "SUCCEEDED",
            response_ref=(result.artifact_dir / "response.txt")
            .relative_to(root)
            .as_posix(),
            invocation_ids=(result.invocation_id,),
        )
        write_call_summary(root)
        return self._unwrap(result, workdir=workdir)

    def complete_unstructured_result(
        self,
        prompt: str,
        *,
        workdir: Path,
        call_name: str,
        ledger_context: dict[str, Any],
    ) -> TransportResult:
        root = Path(workdir)
        initialize_events(root)
        call_id = str(
            ledger_context.get("call_id") or "SC-UNSTRUCTURED-" + uuid.uuid4().hex
        )
        role = str(ledger_context.get("role") or "unstructured_model_call")
        stage = str(ledger_context.get("stage") or call_name)
        context = dict(ledger_context)
        context.update({"call_id": call_id, "role": role, "stage": stage})
        return self._complete_transport(
            prompt,
            workdir=root,
            call_name=call_name,
            output_schema=None,
            ledger_context=context,
        )

    def complete_structured_result(
        self,
        prompt: str,
        *,
        workdir: Path,
        call_name: str,
        output_schema: dict[str, Any],
        ledger_context: dict[str, Any],
    ) -> TransportResult:
        # Output shape is a prompt protocol, checked locally after completion.
        # Do not require provider support for JSON-schema response formats.
        return self._complete_transport(
            prompt,
            workdir=workdir,
            call_name=call_name,
            output_schema=output_schema,
            ledger_context=ledger_context,
        )

    def _complete_transport(
        self,
        prompt: str,
        *,
        workdir: Path,
        call_name: str,
        output_schema: dict[str, Any] | None,
        ledger_context: dict[str, Any],
    ) -> TransportResult:
        from pace.runtime.sessions import model_failure, record_model_failure

        failure = model_failure(workdir)
        if failure is not None:
            # A caller may try another role after handling a failed result.
            # Close its already-started record without starting another process.
            finish_call(workdir, ledger_context["call_ref"], status="INTERFACE_FAILED",
                        error=failure.to_json())
            append_event(
                workdir, "SEMANTIC_CALL_FAILED", call_id=ledger_context["call_id"],
                call_name=call_name, role=ledger_context.get("role", "model_call"),
                stage=ledger_context.get("stage", call_name),
                error_code=failure.error_code, suppressed=True,
            )
            write_call_summary(workdir)
            raise failure
        try:
            result = self._complete_transport_owned(
                prompt,
                workdir=workdir,
                call_name=call_name,
                output_schema=output_schema,
                ledger_context=ledger_context,
            )
        except (KeyboardInterrupt, SystemExit):
            try:
                _record_call_interruption(
                    Path(workdir),
                    ledger_context,
                    call_name=call_name,
                    model=self.model,
                    reasoning_effort=_FIXED_ROLE_EFFORT.get(
                        str(ledger_context.get("role")), self.reasoning_effort),
                )
            except Exception:
                logger.warning("Could not persist interrupted call", exc_info=True)
            raise
        if (isinstance(result, TransportFailure)
                and result.retry_class != RETRY_MODEL_REPRESENTATION
                and result.error_code != "interrupted"):
            record_model_failure(workdir, CodexCallError.from_result(
                result, workdir=workdir, stage=str(ledger_context.get("stage") or call_name),
            ))
        return result

    def _complete_transport_owned(
        self,
        prompt: str,
        *,
        workdir: Path,
        call_name: str,
        output_schema: dict[str, Any] | None,
        ledger_context: dict[str, Any],
    ) -> TransportResult:
        resolved = workdir.resolve()
        call_id = str(ledger_context["call_id"])
        role = str(ledger_context.get("role") or "model_call")
        reasoning_effort = _FIXED_ROLE_EFFORT.get(role, self.reasoning_effort)
        search_enabled = self.web_search and role == "SURVEY"
        stage = str(ledger_context.get("stage") or call_name)
        record_ref = str(ledger_context["call_ref"])
        invocation_dir = call_directory(resolved, record_ref)
        record = json.loads((resolved / record_ref).read_text(encoding="utf-8"))
        prompt_file = resolved / record["prompt_ref"]
        schema_file = None
        if output_schema is not None:
            schema_file = invocation_dir / "output_schema.json"
            _write_json_atomic(schema_file, output_schema)
        if ledger_context.get("inline_task") is True:
            instruction = _search_instruction(search_enabled) + prompt
        else:
            instruction = _build_file_driven_cli_instruction(
                web_search=search_enabled,
                call_name=call_name,
                workdir=resolved,
                prompt_file=prompt_file,
                input_refs=ledger_context.get("input_refs", ()),
                output_schema_file=schema_file,
                role=role,
            )
        instruction += (
            "\nUse only this task's saved PACE role resources and Skills. "
            "Do not discover or read personal AGENTS, Skills or memories in the user's home. "
            "Authentication files are not research inputs.\n"
        )
        from pace.reporting.diagnostics import execution_capabilities

        capabilities = execution_capabilities()
        _write_json_atomic(
            resolved / work_ref(resolved, "state/environment.json"), capabilities
        )
        instruction += (
            "\nLocal execution capabilities (do not assume optional tools):\n"
            + json.dumps(capabilities)
            + "\nFor Python code and scripts, invoke the exact absolute interpreter "
            "in the python field above, quoting its path for the shell. Do not substitute "
            "python or python3: a nested login shell may select a different environment. "
            "The optional package flags describe only that interpreter. On an import "
            "failure, check the interpreter before reporting a missing dependency; "
            "do not install packages or change environments without task authorization.\n"
        )
        _write_text_atomic(invocation_dir / "instruction.md", instruction)
        invocation_id = uuid.uuid4().hex
        environment = os.environ.copy()
        # The launching shell's last-command marker is not child configuration.
        # Exclude it from Codex's nested shell snapshot, including Unicode paths.
        environment.pop("_", None)
        from pace.runtime.input_protection import (
            protected_inputs,
            InputModifiedError,
        )

        protection = protected_inputs(
            resolved,
            (
                *ledger_context.get("input_refs", ()),
                *ledger_context.get("protected_input_refs", ()),
            ),
            invocation_dir,
        )
        effective_max_retries = _retry_count_from_env(self.max_retries)
        timeout = _timeout_from_env(self.timeout_seconds)
        for attempt in range(1, effective_max_retries + 2):
            attempt_dir = attempt_directory(resolved, record_ref, attempt)
            response_file = attempt_dir / "response.txt"
            stdout_path = attempt_dir / "stdout.jsonl"
            stderr_path = attempt_dir / "stderr.txt"
            cmd = [self.cli_path, "exec"]
            cmd.extend(isolated_codex_args(resolved))
            cmd.extend(retry_codex_args())
            # Codex enables search by default, including in full-access mode.
            # Pin both states instead of relying on the absence of --search.
            cmd.extend(
                [
                    "-c",
                    'web_search="live"' if search_enabled else 'web_search="disabled"',
                ]
            )
            if reasoning_effort == "none":
                cmd.extend(["-c", "model_supports_reasoning_summaries=false"])
            else:
                cmd.extend(
                    [
                        "-c",
                        f"model_reasoning_effort={json.dumps(reasoning_effort)}",
                    ]
                )
            cmd.extend(
                [
                    "--json",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "-C",
                    str(resolved),
                    "-m",
                    self.model,
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-o",
                    str(response_file),
                    "-",
                ]
            )
            record_transport_attempt_started(
                resolved,
                call_id=call_id,
                call_name=call_name,
                role=role,
                stage=stage,
                invocation_id=invocation_id,
                attempt_index=attempt,
                artifact_dir=attempt_dir,
            )
            try:
                protection.check(attempt_dir)
                protection.arm()
                # Child output goes directly to disk, including when it times
                # out or the Host is interrupted before communicate returns.
                with (
                    stdout_path.open("w", encoding="utf-8") as stdout,
                    stderr_path.open("w", encoding="utf-8") as stderr,
                ):
                    started = time.monotonic()
                    was_cancelled = False
                    try:
                        # Codex refuses to install its shell helpers when its
                        # configuration home is inside the system temp directory.
                        config_root = Path(
                            environment.get("CODEX_HOME") or Path.home() / ".codex"
                        )
                        config_root.mkdir(parents=True, exist_ok=True)
                        with tempfile.TemporaryDirectory(
                            prefix="pace-call-", dir=config_root
                        ) as config_home:
                            with isolated_codex_environment(
                                environment, Path(config_home)
                            ) as isolated_env:
                                process = run_owned_process(
                                    cmd,
                                    workdir=resolved,
                                    cwd=resolved,
                                    env=isolated_env,
                                    input=instruction,
                                    stdout=stdout,
                                    stderr=stderr,
                                    timeout=timeout,
                                )
                    except (KeyboardInterrupt, SystemExit):
                        was_cancelled = True
                        raise
                    finally:
                        try:
                            protection.check(attempt_dir)
                        except Exception:
                            if not was_cancelled:
                                raise
                            logger.warning(
                                "Input restoration diagnostics preserved during cancellation",
                                exc_info=True,
                            )
                        finally:
                            if (
                                getattr(self, "profile", False)
                                or os.environ.get("PACE_PROFILE") == "1"
                            ):
                                try:
                                    append_event(
                                        resolved,
                                        "PROFILE_SPAN",
                                        name=f"transport.{role}",
                                        elapsed_seconds=time.monotonic() - started,
                                        metadata={
                                            "call_id": call_id,
                                            "attempt_index": attempt,
                                        },
                                    )
                                except Exception:
                                    logger.warning(
                                        "Could not persist transport timing",
                                        exc_info=True,
                                    )
            except (OSError, subprocess.TimeoutExpired, InputModifiedError) as exc:
                missing = isinstance(exc, FileNotFoundError)
                timed_out = isinstance(exc, subprocess.TimeoutExpired)
                modified = isinstance(exc, InputModifiedError)
                environment_error = isinstance(exc, PermissionError) or getattr(exc, "errno", None) in {
                    errno.ENOSPC, errno.EROFS,
                }
                result: TransportResult = TransportFailure(
                    invocation_id=invocation_id,
                    call_name=call_name,
                    error_code="input_modified"
                    if modified
                    else "cli_not_found"
                    if missing
                    else "timeout"
                    if timed_out
                    else "process_io_error",
                    # An exhausted call returns to its owner with partial
                    # artifacts; it must not silently restart the whole task.
                    retry_class=(
                        RETRY_CONFIGURATION
                        if missing or modified or environment_error
                        else RETRY_NON_TRANSIENT
                        if timed_out
                        else RETRY_TRANSIENT
                    ),
                    retryable=not (missing or timed_out or modified or environment_error),
                    exit_code=None,
                    terminal_event_type="process.not_started"
                    if missing
                    else "process.exception",
                    attempt_count=attempt,
                    diagnostics=({"stage": "transport", "message": str(exc)},),
                    artifact_dir=attempt_dir,
                    timed_out=timed_out,
                )
            else:
                completed = subprocess.CompletedProcess(
                    cmd,
                    process.returncode,
                    stdout_path.read_text(encoding="utf-8", errors="replace"),
                    stderr_path.read_text(encoding="utf-8", errors="replace"),
                )
                timeline = reduce_codex_event_timeline(completed.stdout)
                response = (
                    response_file.read_text(encoding="utf-8").strip()
                    if response_file.is_file()
                    else ""
                )
                response_source = "response.txt" if response else ""
                if not response and timeline.assistant_message:
                    response = timeline.assistant_message.strip()
                    response_source = "stdout.jsonl:agent_message"
                    _write_text_atomic(response_file, response)
                result = decide_transport_outcome(
                    completed=completed,
                    timeline=timeline,
                    current_output_text=response,
                    current_output_source=response_source,
                    output_schema=output_schema,
                    invocation_id=invocation_id,
                    call_name=call_name,
                    attempt_count=attempt,
                    artifact_dir=attempt_dir,
                )
            warnings = []
            if stderr_path.is_file():
                for line in stderr_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if any(
                        word in line.lower()
                        for word in (
                            "stream disconnected",
                            "retrying sampling",
                            "model catalog",
                            "refresh available models",
                            "highload",
                        )
                    ):
                        warnings.append(
                            {
                                "warning_class": (
                                    "PROVIDER_CATALOG"
                                    if any(
                                        word in line.lower()
                                        for word in (
                                            "model catalog",
                                            "refresh available models",
                                        )
                                    )
                                    else "CAPACITY"
                                    if "highload" in line.lower()
                                    else "STREAM_RECOVERY"
                                ),
                                "source": "stderr",
                                "message": line,
                                "recovered": isinstance(result, TransportSuccess),
                            }
                        )
            if warnings:
                result = replace(
                    result, warnings=_merge_stream_warnings(result.warnings, warnings)
                )
            _write_transport_result(
                attempt_dir,
                result,
                model=self.model,
                reasoning_effort=reasoning_effort,
            )
            policy_path = attempt_dir / "transport_result.json"
            policy_result = json.loads(policy_path.read_text(encoding="utf-8"))
            policy_result["effective_retry_policy"] = {
                "request_max_retries": 8,
                "stream_max_retries": 20,
                "outer_max_retries": effective_max_retries,
                "web_search": search_enabled,
            }
            _write_json_atomic(policy_path, policy_result)
            record_transport_attempt_finished(
                resolved,
                result,
                call_id=call_id,
                role=role,
                stage=stage,
                attempt_index=attempt,
            )
            if (
                isinstance(result, TransportFailure)
                and result.retryable
                and attempt <= effective_max_retries
            ):
                archived = []
                for raw in ledger_context.get("output_refs", ()):
                    path = (resolved / raw).resolve()
                    if path.is_relative_to(resolved) and path.is_file():
                        destination = (
                            attempt_dir / "partial_outputs" / path.relative_to(resolved)
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        path.replace(destination)
                        archived.append(str(destination.relative_to(resolved)))
                for raw in ledger_context.get("output_dirs", ()):
                    path = (resolved / raw).resolve()
                    if path.is_relative_to(resolved) and path.is_dir():
                        destination = (
                            attempt_dir / "partial_outputs" / path.relative_to(resolved)
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        path.replace(destination)
                        path.mkdir(parents=True, exist_ok=True)
                        archived.append(str(destination.relative_to(resolved)))
                if archived and "PROBE" not in role.upper():
                    instruction += (
                        "\nPartial, unverified drafts from the preceding attempt:\n"
                        + "\n".join(archived)
                    )
                time.sleep(_retry_delay_seconds(attempt))
                continue
            return result
        raise AssertionError("transport retry loop terminated without a result")

    @staticmethod
    def _unwrap(result: TransportResult, *, workdir: Path) -> str:
        if isinstance(result, TransportSuccess):
            return result.text
        raise CodexCallError.from_result(result, workdir=workdir)


def _record_call_interruption(
    root: Path,
    context: dict[str, Any],
    *,
    call_name: str,
    model: str,
    reasoning_effort: str,
) -> None:
    call_id = str(context["call_id"])
    call_ref = str(context["call_ref"])
    record = json.loads((root / call_ref).read_text(encoding="utf-8"))
    if record["status"] != "STARTED":
        return
    rows = read_events(root)
    started = next(
        (
            row
            for row in reversed(rows)
            if row.get("call_id") == call_id
            and row["event_kind"] == "TRANSPORT_ATTEMPT_STARTED"
        ),
        {},
    )
    invocation_id = str(started.get("invocation_id") or "")
    role, stage = record["role"], record["stage"]
    if started and not any(
        row["event_kind"] == "TRANSPORT_ATTEMPT_FINISHED"
        and row.get("invocation_id") == invocation_id
        and row.get("attempt_index") == started["attempt_index"]
        for row in rows
    ):
        index = int(started["attempt_index"])
        directory = call_directory(root, call_ref) / "attempts" / f"{index:04d}"
        failure = TransportFailure(
            invocation_id=invocation_id,
            call_name=call_name,
            error_code="interrupted",
            retry_class="USER_INTERRUPTED",
            retryable=False,
            exit_code=None,
            terminal_event_type="host.interrupted",
            attempt_count=index,
            diagnostics=(
                {
                    "stage": "host",
                    "message": "Call interrupted; partial artifacts preserved",
                },
            ),
            artifact_dir=directory,
        )
        _write_transport_result(
            directory, failure, model=model, reasoning_effort=reasoning_effort
        )
        record_transport_attempt_finished(
            root, failure, call_id=call_id, role=role, stage=stage, attempt_index=index
        )
    finish_call(
        root,
        call_ref,
        status="ABORTED_BY_USER",
        invocation_ids=(invocation_id,) if invocation_id else (),
        error={"error_code": "interrupted", "retry_class": "USER_INTERRUPTED"},
    )
    append_event(
        root,
        "SEMANTIC_CALL_ABORTED",
        call_id=call_id,
        call_name=call_name,
        role=role,
        stage=stage,
        invocation_id=invocation_id,
        terminal_outcome="ABORTED_BY_USER",
        failure_class="USER_INTERRUPTED",
        retry_class="USER_INTERRUPTED",
        error_code="interrupted",
        suppressed=False,
    )
    write_call_summary(root)


def build_llm(
    *,
    provider: str,
    model: str,
    reasoning_effort: str,
    web_search: bool = False,
    timeout_seconds: int | None = None,
) -> LLM:
    if provider in {"newapi", "llmmelon"} and not (
        os.getenv("PACE_CODEX_AUTH_MODE") == "api_key"
        and os.getenv("PACE_CODEX_BASE_URL")
        and os.getenv("PACE_CODEX_API_KEY")
        and os.getenv("PACE_CODEX_CLI_PATH")
    ):
        raise ValueError(
            "Custom API provider requires the configured Codex wrapper and API credentials; "
            "use scripts/run_batch.py with examples/quickstart/suite.json and config.local.json. No login fallback is allowed."
        )
    if provider in {"codex", "newapi", "llmmelon"}:
        return CodexLLM(
            model=model,
            reasoning_effort=reasoning_effort,
            cli_path=os.getenv("PACE_CODEX_CLI_PATH", "codex"),
            web_search=web_search,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"unknown provider: {provider}")


def reduce_codex_event_timeline(text: str) -> CodexEventTimeline:
    terminal_events: list[tuple[int, str, dict[str, Any]]] = []
    generic_errors: list[dict[str, Any]] = []
    terminal_failures: list[dict[str, Any]] = []
    assistant_message = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    invalid_lines = 0
    for index, line in enumerate(str(text or "").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(row, dict):
            continue
        event_type = str(row.get("type") or "")
        if event_type in {"turn.completed", "thread.completed", "response.completed"}:
            terminal_events.append((index, "SUCCESS", row))
            raw_usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            usage["input_tokens"] += int(raw_usage.get("input_tokens") or 0)
            usage["output_tokens"] += int(raw_usage.get("output_tokens") or 0)
        elif event_type in {"turn.failed", "thread.failed", "response.failed"}:
            terminal_events.append((index, "FAILURE", row))
            terminal_failures.append({"event_index": index, **row})
        elif event_type == "error":
            generic_errors.append({"event_index": index, **row})
            continue
        message = _assistant_message_from_event(row)
        if message is not None:
            # An empty final message supersedes prior progress messages too.
            # Otherwise a stale schema-shaped commentary becomes the result.
            assistant_message = message
    if terminal_events:
        terminal_index, terminal_kind, terminal_event = terminal_events[-1]
    else:
        terminal_index, terminal_kind, terminal_event = None, "NONE", {}
    recovered = tuple(
        event
        for event in generic_errors
        if terminal_kind == "SUCCESS"
        and int(event.get("event_index") or 0) < int(terminal_index or 0)
    )
    return CodexEventTimeline(
        final_terminal_kind=terminal_kind,
        final_terminal_index=terminal_index,
        final_terminal_event=terminal_event,
        assistant_message=assistant_message,
        recoverable_error_events=recovered
        if terminal_kind == "SUCCESS"
        else tuple(generic_errors),
        terminal_failure_events=tuple(terminal_failures),
        usage=usage,
        invalid_jsonl_line_count=invalid_lines,
    )


def decide_transport_outcome(
    *,
    completed: subprocess.CompletedProcess[str],
    timeline: CodexEventTimeline,
    current_output_text: str,
    current_output_source: str,
    output_schema: dict[str, Any] | None,
    invocation_id: str,
    call_name: str,
    attempt_count: int,
    artifact_dir: Path,
) -> TransportResult:
    warnings = (
        tuple(_warning_from_event(event) for event in timeline.recoverable_error_events)
        if timeline.terminal_success
        else ()
    ) + _provider_compatibility_warnings(completed.stderr)
    terminal_authority = {
        "source": "PROVIDER_TERMINAL_EVENT",
        "authority_event_type": timeline.terminal_event_type,
        "authority_event_index": timeline.final_terminal_index,
        "final_terminal_kind": timeline.final_terminal_kind,
        "invocation_id": invocation_id,
    }

    if timeline.terminal_error:
        error_code, retry_class, retryable = _classify_transport_failure(
            completed=completed,
            events=timeline,
        )
        code, message = _error_from_event(timeline.final_terminal_event)
        authority = {
            **terminal_authority,
            "source": "PROVIDER_TERMINAL_FAILURE",
            "authority_error_code": code,
            "authority_classified_error_code": error_code,
            "authority_schema_rejection": error_code == "invalid_json_schema",
            "authority_error_message": message,
        }
        return TransportFailure(
            invocation_id=invocation_id,
            call_name=call_name,
            error_code=error_code,
            retry_class=retry_class,
            retryable=retryable,
            exit_code=completed.returncode,
            terminal_event_type=timeline.terminal_event_type,
            attempt_count=attempt_count,
            diagnostics=({"stage": "transport", "message": message or error_code},),
            artifact_dir=artifact_dir,
            warnings=warnings,
            authority_evidence=authority,
        )

    if not timeline.terminal_success:
        error_code, retry_class, retryable = _classify_transport_failure(
            completed=completed,
            events=timeline,
        )
        diagnostic_message = (
            _latest_nonterminal_error_message(timeline) or completed.stderr.strip()
        )
        return TransportFailure(
            invocation_id=invocation_id,
            call_name=call_name,
            error_code=error_code,
            retry_class=retry_class,
            retryable=retryable,
            exit_code=completed.returncode,
            terminal_event_type=timeline.terminal_event_type
            or "process.exited_without_terminal",
            attempt_count=attempt_count,
            diagnostics=(
                {
                    "stage": "transport",
                    "message": diagnostic_message
                    or "Codex emitted no terminal completion event",
                },
            ),
            artifact_dir=artifact_dir,
            warnings=warnings,
            authority_evidence={
                "source": "PROCESS_AND_NONTERMINAL_EVENTS",
                "invocation_id": invocation_id,
            },
        )

    output = str(current_output_text or "").strip()
    tool_host_unavailable = (
        "timed out negotiating with the code-mode host"
        in (completed.stderr or "").lower()
    )
    if completed.returncode != 0:
        warnings = warnings + (
            {
                "warning_class": "PROCESS_EXIT_AFTER_TERMINAL_SUCCESS",
                "exit_code": completed.returncode,
                "message": "valid terminal success is authoritative only if current output validation passes",
            },
        )
    if not output:
        return TransportFailure(
            invocation_id=invocation_id,
            call_name=call_name,
            error_code="tool_host_negotiation_timeout"
            if tool_host_unavailable
            else "TRANSPORT_EMPTY",
            retry_class=RETRY_TRANSIENT
            if tool_host_unavailable
            else RETRY_MODEL_REPRESENTATION,
            retryable=tool_host_unavailable,
            exit_code=completed.returncode,
            terminal_event_type=timeline.terminal_event_type,
            attempt_count=attempt_count,
            diagnostics=(
                {
                    "stage": "output",
                    "message": "terminal success contained no current output",
                },
            ),
            artifact_dir=artifact_dir,
            warnings=warnings,
            authority_evidence=terminal_authority,
        )

    schema_valid: bool | None = None
    if output_schema is not None:
        # A single fence and literal unsupported string escapes are encoding
        # boundaries. All structural/schema errors still fail validation.
        fenced = re.fullmatch(
            r"```(?:json)?\s*\n(.*)\n```", output, re.DOTALL | re.IGNORECASE
        )
        if fenced:
            output = fenced.group(1).strip()
        try:
            parsed, escape_positions = load_model_json(output)
        except json.JSONDecodeError as exc:
            schema_errors = [
                {"stage": "output_parse", "json_pointer": "/", "message": str(exc)}
            ]
            error_code = "output_json_invalid"
        else:
            schema_errors = validate_json_instance(parsed, output_schema)
            error_code = "output_instance_schema_invalid"
            if not schema_errors and escape_positions:
                output = json.dumps(parsed, ensure_ascii=False)
                normalized = artifact_dir / "response.normalized.json"
                _write_text_atomic(normalized, output + "\n")
                terminal_authority["json_string_escape_normalization"] = {
                    "kind": "UNSUPPORTED_ESCAPE_TO_LITERAL_BACKSLASH",
                    "offset_basis": "trimmed_unfenced_json_text",
                    "original_output_source": current_output_source,
                    "character_offsets": list(escape_positions),
                    "normalized_response_ref": str(normalized),
                    "original_response_preserved": True,
                }
        schema_valid = not schema_errors
        if schema_errors:
            return TransportFailure(
                invocation_id=invocation_id,
                call_name=call_name,
                error_code="tool_host_negotiation_timeout"
                if tool_host_unavailable
                else error_code,
                retry_class=RETRY_TRANSIENT
                if tool_host_unavailable
                else RETRY_MODEL_REPRESENTATION,
                retryable=tool_host_unavailable,
                exit_code=completed.returncode,
                terminal_event_type=timeline.terminal_event_type,
                attempt_count=attempt_count,
                diagnostics=tuple(schema_errors),
                artifact_dir=artifact_dir,
                output_schema_valid=False,
                text=str(current_output_text or ""),
                warnings=warnings,
                authority_evidence={
                    **terminal_authority,
                    "source": "HOST_OUTPUT_VALIDATION",
                    "validation_error_count": len(schema_errors),
                    "validation_error_code": error_code,
                },
            )

    if tool_host_unavailable:
        warnings += (
            {
                "warning_class": "TOOL_HOST_NEGOTIATION_WARNING",
                "message": "Tool host negotiation timed out; current output validation passed.",
            },
        )
    return TransportSuccess(
        text=output,
        invocation_id=invocation_id,
        call_name=call_name,
        exit_code=completed.returncode,
        terminal_event_type=timeline.terminal_event_type,
        accepted_output_source=current_output_source,
        output_schema_valid=schema_valid,
        attempt_count=attempt_count,
        artifact_dir=artifact_dir,
        warnings=warnings,
        authority_evidence={
            **terminal_authority,
            "source": "CURRENT_OUTPUT_ACCEPTANCE",
            "output_schema_valid": schema_valid,
        },
    )


def _assistant_message_from_event(row: dict[str, Any]) -> str | None:
    event_type = str(row.get("type") or "")
    if event_type == "item.completed":
        item = row.get("item") if isinstance(row.get("item"), dict) else {}
        if str(item.get("type") or "") in {"agent_message", "assistant_message"}:
            return str(item.get("text") or item.get("content") or "").strip()
    if event_type in {"agent_message", "assistant_message", "message.completed"}:
        role = str(row.get("role") or "assistant")
        if role in {"assistant", "agent"}:
            return str(
                row.get("text") or row.get("content") or row.get("message") or ""
            ).strip()
    return None


def _error_from_event(row: dict[str, Any]) -> tuple[str, str]:
    raw: Any = row.get("error") if row.get("error") is not None else row.get("message")
    if isinstance(raw, str):
        message = raw
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {}
        raw = decoded.get("error", decoded) if isinstance(decoded, dict) else {}
    else:
        message = (
            json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else ""
        )
    if isinstance(raw, dict) and isinstance(raw.get("error"), dict):
        raw = raw["error"]
    if isinstance(raw, dict):
        code = str(raw.get("code") or raw.get("type") or "")
        nested_message = str(raw.get("message") or message)
        if not code and nested_message:
            try:
                decoded_message = json.loads(nested_message)
            except json.JSONDecodeError:
                decoded_message = {}
            if isinstance(decoded_message, dict):
                nested = decoded_message.get("error", decoded_message)
                if isinstance(nested, dict):
                    code = str(nested.get("code") or nested.get("type") or "")
                    nested_message = str(nested.get("message") or nested_message)
        return code, nested_message
    return "", message


def _merge_stream_warnings(existing, stderr_rows):
    """Pair matching stdout/stderr events once, preserving repeated occurrences."""
    merged = [dict(row) for row in existing]
    paired = set()
    for row in stderr_rows:
        message = str(row.get("message", "")).strip().casefold()
        match = next(
            (
                index
                for index, previous in enumerate(merged[: len(existing)])
                if index not in paired
                and message
                and (
                    message == str(previous.get("message", "")).strip().casefold()
                    or (
                        len(str(previous.get("message", ""))) > 20
                        and str(previous["message"]).strip().casefold() in message
                    )
                )
            ),
            None,
        )
        if match is None:
            merged.append(row)
        else:
            paired.add(match)
            merged[match].update(
                sources=["stdout", "stderr"], stderr_message=row["message"]
            )
    return tuple(merged)


def _warning_from_event(row: dict[str, Any]) -> dict[str, Any]:
    code, message = _error_from_event(row)
    return {
        "warning_class": "RECOVERED_PROVIDER_ERROR",
        "source": "stdout",
        "event_type": str(row.get("type") or "error"),
        "event_index": int(row.get("event_index") or 0),
        "error_code": code,
        "message": message,
        "recovered": True,
    }


def _provider_compatibility_warnings(stderr: str) -> tuple[dict[str, Any], ...]:
    text = str(stderr or "")
    lowered = text.lower()
    if not (
        "failed to decode models response" in lowered
        and ("missing field `models`" in lowered or "missing field 'models'" in lowered)
    ):
        return ()
    return (
        {
            "warning_class": "PROVIDER_MODEL_CATALOG_INCOMPATIBLE",
            "error_code": "provider_models_schema_incompatible",
            "retry_class": RETRY_CONFIGURATION,
            "message": (
                "the Codex CLI could not decode the provider model catalog because its "
                "response has no top-level `models` field"
            ),
            "configuration_action": (
                "configure the custom provider model-list endpoint to return the model-catalog "
                "shape expected by the installed Codex CLI, or use a provider/CLI version with "
                "a compatible model catalog"
            ),
            "recovered": True,
        },
    )


def _latest_nonterminal_error_message(timeline: CodexEventTimeline) -> str:
    if not timeline.recoverable_error_events:
        return ""
    return _error_from_event(timeline.recoverable_error_events[-1])[1]


def _write_transport_result(
    invocation_dir: Path,
    result: TransportResult,
    *,
    model: str,
    reasoning_effort: str,
) -> None:
    _write_json_atomic(
        invocation_dir / "transport_result.json",
        {
            **_transport_payload(result),
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
    )


def _transport_payload(result: TransportResult) -> dict[str, Any]:
    common = {
        "invocation_id": result.invocation_id,
        "call_name": result.call_name,
        "attempt_count": int(result.attempt_count),
        "exit_code": result.exit_code,
        "terminal_event_type": result.terminal_event_type,
        "fallback_used": False,
        "warnings": list(result.warnings),
        "warning_count": len(result.warnings),
        "authority_evidence": dict(result.authority_evidence),
    }
    if isinstance(result, TransportSuccess):
        return {
            **common,
            "status": "SUCCESS",
            "error_code": "",
            "retry_class": "NONE",
            "retryable": False,
            "timed_out": False,
            "accepted_output_source": result.accepted_output_source,
            "output_schema_valid": result.output_schema_valid,
            "diagnostics": [],
            "recovered_transport_warning": bool(result.warnings),
        }
    return {
        **common,
        "status": "ABORTED" if result.error_code == "interrupted" else "FAILED",
        "error_code": result.error_code,
        "retry_class": result.retry_class,
        "transient_classification": result.retry_class,
        "retryable": result.retryable,
        "timed_out": result.timed_out,
        "accepted_output_source": "",
        "output_schema_valid": result.output_schema_valid,
        "diagnostics": list(result.diagnostics),
        "validation_stage": "HOST_OUTPUT_VALIDATION"
        if result.output_schema_valid is False
        else "",
        "validation_error_count": len(result.diagnostics)
        if result.output_schema_valid is False
        else 0,
        "validation_errors": list(result.diagnostics)
        if result.output_schema_valid is False
        else [],
        "failure_id": result.failure_id,
        "root_failure_id": result.root_failure_id,
        "fatal_scope": result.fatal_scope,
        "suppressed": result.suppressed,
        "recovered_transport_warning": False,
    }


def classify_model_error(
    code: str, message: str, *, http_status: int | None = None,
) -> tuple[str, str, bool]:
    """One classification for provider replies and the completed CLI timeline."""
    code = code.lower()
    text = (code + "\n" + message).lower()
    if http_status is None:
        status = re.search(r"\b(?:http(?: status)?|status)\s*[:=]?\s*(\d{3})\b", text)
        if status:
            http_status = int(status.group(1))
    if any(marker in text for marker in (
        "insufficient_quota", "insufficient quota", "insufficient account balance",
        "insufficient balance", "credit balance", "quota_exceeded", "quota exhausted",
        "exceeded your current quota", "billing_hard_limit", "余额不足", "额度不足", "配额不足",
    )):
        return "insufficient_quota", RETRY_CONFIGURATION, False
    if code == "invalid_json_schema" or any(marker in text for marker in (
        "invalid schema for response_format", "missing required schema field",
        "additionalproperties is required",
    )):
        return "invalid_json_schema", RETRY_DETERMINISTIC_HOST, False
    if code in {"authentication_error", "invalid_api_key", "permission_denied"} or any(
        marker in text for marker in (
            "invalid api key", "incorrect api key", "permission denied", "unauthorized",
        )
    ):
        return code or "authentication_error", RETRY_CONFIGURATION, False
    if code == "model_not_found" or any(marker in text for marker in (
        "model not found", "model access", "unsupported model", "unknown model",
    )):
        return "model_not_found", RETRY_CONFIGURATION, False
    if code in {"context_length_exceeded", "context_window_exceeded", "prompt_too_long"} or any(
        marker in text for marker in (
            "context length exceeded", "maximum context length", "context window exceeded",
            "prompt is too long", "too many tokens",
        )
    ):
        return "context_overflow", RETRY_NON_TRANSIENT, False
    if code in {
        "invalid_parameter", "unsupported_parameter", "unsupported_value", "invalid_request_error",
    } or any(marker in text for marker in (
        "invalidparameter", "this text.format type is unavailable", "unsupported parameter",
        "unsupported value", "not supported with this model", "does not support", "unknown variant",
    )):
        return "unsupported_request_parameter", RETRY_CONFIGURATION, False
    if any(marker in text for marker in (
        "certificate verify failed", "certificate_verify_failed", "hostname mismatch",
        "name or service not known", "nodename nor servname provided",
    )):
        return code or "connection_configuration_error", RETRY_CONFIGURATION, False
    if http_status in {401, 403, 407}:
        return code or "permission_denied", RETRY_CONFIGURATION, False
    if http_status is not None and 400 <= http_status < 500 and http_status not in {408, 429}:
        return code or f"http_{http_status}", RETRY_CONFIGURATION, False
    if http_status in {408, 429, 500, 502, 503, 504} or code in {
        "rate_limit_exceeded", "too_many_requests", "server_error", "overloaded_error",
    } or any(marker in text for marker in (
        "selected model is at capacity", "model is at capacity", "temporarily unavailable",
        "please try again", "rate limit", "rate_limit_exceeded", "too_many_requests",
        "concurrency limit exceeded", "502 bad gateway", "503 service unavailable",
        "504 gateway timeout", "timeout", "timed out", "overloaded", "stream disconnected",
        "upstream response stream was interrupted",
        "connection reset", "connection refused", "connection closed", "connection aborted",
        "connection failed", "broken pipe", "temporary failure in name resolution",
        "missing field in models response", "json stream truncation",
    )):
        return code or "transient_execution_error", RETRY_TRANSIENT, True
    return code or "transport_failed", RETRY_NON_TRANSIENT, False


def _classify_transport_failure(
    *,
    completed: subprocess.CompletedProcess[str],
    events: CodexEventTimeline,
) -> tuple[str, str, bool]:
    terminal_code, terminal_message = (
        _error_from_event(events.final_terminal_event) if events.terminal_error else ("", "")
    )
    latest = events.recoverable_error_events[-1] if events.recoverable_error_events else {}
    generic_code, generic_message = _error_from_event(latest)
    result = classify_model_error(
        terminal_code or generic_code,
        "\n".join((terminal_message, generic_message,
                   completed.stderr if not events.terminal_success else "")),
    )
    if result[0] == "transport_failed" and not events.terminal_error and completed.returncode == 0:
        return "missing_terminal_success", RETRY_NON_TRANSIENT, False
    return result


def _retry_count_from_env(default: int) -> int:
    default_count = min(MAX_TRANSPORT_RETRIES, max(0, int(default)))
    raw = os.getenv("PACE_CLI_MAX_RETRIES")
    if not raw:
        return default_count
    try:
        return min(MAX_TRANSPORT_RETRIES, max(0, int(raw)))
    except ValueError:
        return default_count


def _retry_delay_seconds(attempt: int) -> float:
    """Return a bounded exponential retry delay with small provider-friendly jitter."""
    attempt_index = max(1, int(attempt))
    upper_bound = min(
        RETRY_BACKOFF_MAX_SECONDS,
        RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt_index - 1)),
    )
    return min(
        RETRY_BACKOFF_MAX_SECONDS,
        upper_bound * random.uniform(0.8, 1.2),
    )


def _timeout_from_env(default: int | None) -> int | None:
    raw = os.getenv("PACE_CLI_TIMEOUT_SECONDS")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value or None


def _search_instruction(enabled: bool) -> str:
    if not enabled:
        return (
            "External source retrieval is disabled for this call. Use the supplied workspace "
            "materials; do not acquire external sources using web tools, shell commands, or "
            "retrieval clients. This restriction is role-local: Survey has a separate Host-controlled "
            "retrieval setting. Do not infer that Survey is unavailable from your own restriction. "
            "In research mode, request missing sources through the Director; in proof mode, use "
            "the initial Survey and identify any remaining source gap.\n\n"
        )
    return (
        "External Search is available for this call. Use it when needed to check sources "
        "relevant to your assigned role; it is not required for every task. Empty local source "
        "catalogs do not prohibit acquisition. Save acquired text or extracts only under the "
        "assigned output directory, with exact source URLs and locators. Preserve the literal "
        "user target and distinguish retrieved evidence from unverified leads.\n\n"
    )


def _build_file_driven_cli_instruction(
    *,
    web_search: bool = False,
    call_name: str,
    workdir: Path,
    prompt_file: Path,
    input_refs: Iterable[str] = (),
    output_schema_file: Path | None = None,
    role: str = "",
) -> str:
    suggested = tuple(dict.fromkeys(str(ref) for ref in input_refs if str(ref).strip()))
    turn_dir = work_ref(workdir)
    base = f"You are executing pace call `{call_name}` in the problem directory `{workdir}`.\n\n"
    # These phases must see only their independent statement view. The generic
    # workspace navigation below would invite reading reports and prior proofs,
    # even though the phase prompt explicitly excludes that evidence.
    if role.upper() in {
        "RCI_BLIND_PROVER_PROBE",
        "RCI_REFUTE_PROVER_PROBE",
        "RCI_FRONTIER_REFUTE_PROBE",
    }:
        base += (
            f"Read `{prompt_file}` for your statement-only probe task. "
            "For local workspace context, read only that prompt, the structured-output schema named below, "
            "and the explicit input files listed below, including the supplied static method guides. "
            "Do not discover additional context by listing or searching the workspace, or by "
            "following links into other research artifacts. Do not read the original request "
            "or problem, current reports, findings indexes, memory, mechanisms, coverage "
            "records, previous proofs, call traces, or sibling judgments. "
            "The supplied probe statement defines the entire mathematical task. "
            "When search is enabled, you may independently check external sources for that statement. "
            "If it is incomplete, report the defect under the phase protocol instead of "
            "recovering context from other files. Write only the artifacts requested by the "
            "phase prompt; you may reread files you create in this invocation to check them.\n\n"
        )
    else:
        base += (
            f"Read `{prompt_file}` for your task. The current exploration is `{turn_dir}/`. "
            f"The user request is `{turn_dir}/input/request.md`; the current problem is "
            f"`{turn_dir}/input/problem.md`; committed task results are under `{turn_dir}/stages/`.\n\n"
            "You may list and search this problem directory and read relevant existing files on demand, "
            "including earlier work/turn*/ explorations and results/findings/ for this same problem. "
            "Do not read global Codex memories, unrelated problem workspaces, developer test records, "
            "or private repository history. Suggested reading paths are starting points within this "
            "problem directory. Follow relevant references within it and the role-specific evidence "
            "constraints in the task prompt. Read relevant fields or line ranges instead of dumping large files. "
            "Treat historical plans, reports, budgets and verdicts as scoped context; the current "
            "literal target, current Host budget and independent verification remain authoritative. "
            "Write task artifacts to the assigned output location in the current turn and preserve "
            "earlier turns. Keep each research argument in one canonical mathematical manuscript: "
            "when a standalone finding contains the complete proof, the main handoff and memory "
            "should briefly state its scope and link to it instead of copying the proof. A task "
            "without a complete finding keeps partial reasoning in its assigned main deliverable. "
            "Self-contained verifier candidates, blind probes and terminal reports/papers retain "
            "their own completeness requirements.\n\n"
        )
    if suggested:
        base += (
            "Suggested reading paths:\n"
            + "\n".join(f"- `{ref}`" for ref in suggested)
            + "\n\n"
        )
    base += (
        "Treat target/input files, Host state, prompt and schema files as read-only. "
        "Preserve completed artifacts unless this task explicitly assigns them as revision outputs. "
        "Never use patches or other writes to inspect input files, including whitespace or no-op probes. "
        "If read or command tools fail, report the tool failure without a write-based workaround.\n\n"
    )
    base += _search_instruction(web_search)
    if output_schema_file is not None:
        return (
            base
            + f"The current structured-output schema is at `{output_schema_file}`; this exact path is also "
            "part of this task. Read it before answering. Complete exactly the task in the prompt, "
            "then make your final answer only one JSON object satisfying this schema and the complete task. "
            "The schema describes machine-owned requirements, not the complete mathematical report. "
            "Where additionalProperties is true, include all evidence and judgment fields required by "
            "the task prompt even when the schema does not list them; a routing verdict alone is incomplete. "
            "Do not return a prior schema version, Markdown fences, commentary, or an artifact-file confirmation."
        )
    return (
        base
        + "Complete exactly the task described in the prompt. If the task names a required output artifact "
        "path, create or overwrite that exact file with the requested content; putting the artifact content "
        "only in the final response does not satisfy the task. Follow the task prompt's instructions for "
        "the final response as well as its file deliverables."
    )
