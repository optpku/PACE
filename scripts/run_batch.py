#!/usr/bin/env python3
"""Shared batch runner for pace run suites."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pace.orchestrator.recovery import (
    RecoveryError,
    load_director_checkpoint,
    load_operational_checkpoint,
)
from pace.reporting.writing import active_writing_request, writing_complete
from pace.runtime.output.workspace import RunWorkspace, work_ref
from pace.runtime.processes import terminate_process_tree
from pace.runtime.output.setup import load_setup
from pace.orchestrator.finding_publication import pending_finding_checks
from pace.runtime.output.delivery_pause import delivery_pause

RUN_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = RUN_ROOT.parent

CONFIG_SCHEMA = "pace_batch/v1"
RESULT_SCHEMA = "pace_batch_results/v1"
SUITE_PROFILE_SCHEMA = "pace_batch_suite/v1"
PROGRESS_POLL_SECONDS = 1.0
DEFAULT_PROGRESS_INTERVAL_SECONDS = 15.0
TERMINAL_RUN_STATUSES = {"completed", "completed_with_operational_error"}
_SEMANTIC_TERMINAL_EVENTS = {
    "SEMANTIC_CALL_SUCCEEDED": "completed",
    "SEMANTIC_CALL_FAILED": "failed",
    "SEMANTIC_CALL_ABORTED": "aborted",
}


@dataclass(frozen=True)
class SuiteProfile:
    """Dataset-specific syntax and CLI metadata for the shared runner."""

    file: Path
    description: str
    problem_id_pattern: re.Pattern[str]
    problem_suffixes: tuple[str, ...]
    example_ids: tuple[str, ...]

    @property
    def root(self) -> Path:
        return self.file.parent


@dataclass
class _ProblemProgress:
    run_dir: Path | None = None
    seen_event_ids: set[str] = dataclass_field(default_factory=set)
    checkpoint_signature: tuple[Any, ...] = ()
    latest_stage: str = "startup"
    semantic_started: int = 0
    semantic_finished: int = 0
    last_output_at: float = 0.0


def _load_suite_profile(path: Path) -> SuiteProfile:
    profile_file = path.expanduser().resolve()
    value = _read_object(profile_file)
    if value.get("schema_version") != SUITE_PROFILE_SCHEMA:
        raise ValueError(
            f"suite_profile.schema_version must be {SUITE_PROFILE_SCHEMA!r}"
        )
    description = str(value.get("description") or "").strip()
    if not description:
        raise ValueError("suite_profile.description must not be empty")
    pattern_text = str(value.get("problem_id_pattern") or "").strip()
    if not pattern_text:
        raise ValueError("suite_profile.problem_id_pattern must not be empty")
    try:
        pattern = re.compile(pattern_text)
    except re.error as exc:
        raise ValueError(f"suite_profile.problem_id_pattern is invalid: {exc}") from exc

    raw_suffixes = value.get("problem_suffixes")
    if not isinstance(raw_suffixes, list) or not raw_suffixes:
        raise TypeError("suite_profile.problem_suffixes must be a nonempty array")
    suffixes = tuple(str(item).strip().lower() for item in raw_suffixes)
    if any(not suffix.startswith(".") or suffix == "." for suffix in suffixes):
        raise ValueError("suite_profile.problem_suffixes entries must start with a dot")
    if len(set(suffixes)) != len(suffixes):
        raise ValueError("suite_profile.problem_suffixes must be unique")

    raw_examples = value.get("example_ids") or []
    if not isinstance(raw_examples, list):
        raise TypeError("suite_profile.example_ids must be an array")
    example_ids = tuple(str(item).strip().upper() for item in raw_examples)
    if any(not pattern.fullmatch(item) for item in example_ids):
        raise ValueError("suite_profile.example_ids must match problem_id_pattern")
    return SuiteProfile(
        file=profile_file,
        description=description,
        problem_id_pattern=pattern,
        problem_suffixes=suffixes,
        example_ids=example_ids,
    )


def _suite_profile_from_argv(argv: list[str]) -> SuiteProfile:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--suite-profile", type=Path, required=True)
    args, _ = bootstrap.parse_known_args(argv)
    return _load_suite_profile(args.suite_profile)


def _parser(profile: SuiteProfile) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=profile.description)
    parser.add_argument(
        "--suite-profile",
        type=Path,
        default=profile.file,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=(
            profile.root / "config.local.json"
            if (profile.root / "config.local.json").is_file()
            else profile.root / "config.example.json"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate inputs and configuration without starting a model call",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the selected commands without starting a model call",
    )
    parser.add_argument(
        "--ids",
        default="",
        help=(
            "comma-separated problem IDs"
            + (
                f", for example {','.join(profile.example_ids)}"
                if profile.example_ids
                else ""
            )
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="run only the first N selected problems; zero means all",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="override batch.parallel; zero uses the configured value",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="do not resume the latest nonterminal run",
    )
    parser.add_argument(
        "--progress-interval-seconds",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        help=(
            "print a heartbeat for every running problem after this many seconds "
            "without a stage transition"
        ),
    )
    return parser


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path}")
    return value


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if number < minimum:
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{field} must be {qualifier}")
    return number


def _resolve_from_config(config_file: Path, value: Any, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = config_file.parent / path
    return path.resolve()


def _validate_config(
    config_file: Path,
    config: dict[str, Any],
    profile: SuiteProfile,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    Path,
]:
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError(f"config.schema_version must be {CONFIG_SCHEMA!r}")
    dataset = _object(config.get("dataset"), "config.dataset")
    codex = _object(config.get("codex"), "config.codex")
    pipeline = _object(config.get("pipeline"), "config.pipeline")
    batch = _object(config.get("batch"), "config.batch")

    problem_dir = _resolve_from_config(
        config_file,
        dataset.get("problem_dir"),
        "config.dataset.problem_dir",
    )
    if not problem_dir.is_dir():
        raise ValueError(f"problem directory does not exist: {problem_dir}")
    expected_count = _positive_int(
        dataset.get("expected_problem_count"),
        "config.dataset.expected_problem_count",
    )
    problem_glob = str(dataset.get("problem_glob") or "").strip()
    if not problem_glob:
        raise ValueError("config.dataset.problem_glob must not be empty")

    auth_mode = str(codex.get("authentication_mode") or "").strip()
    if auth_mode not in {"api_key", "existing_codex_login"}:
        raise ValueError(
            "config.codex.authentication_mode must be api_key or existing_codex_login"
        )
    provider_name = str(codex.get("provider_name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", provider_name):
        raise ValueError(
            "config.codex.provider_name must contain only letters, digits, and underscores"
        )
    if str(codex.get("wire_api") or "") != "responses":
        raise ValueError(
            "config.codex.wire_api must be responses (the Codex CLI requires a Responses API endpoint)"
        )
    if not str(codex.get("model") or "").strip():
        raise ValueError("config.codex.model must not be empty")
    resource_variant = codex.get("resource_variant")
    if resource_variant is not None and resource_variant not in {"standard", "weak"}:
        raise ValueError("config.codex.resource_variant must be standard or weak")
    if not str(codex.get("reasoning_effort") or "").strip():
        raise ValueError("config.codex.reasoning_effort must not be empty")

    if str(pipeline.get("provider") or "") not in {"codex", "newapi", "llmmelon"}:
        raise ValueError("config.pipeline.provider is unsupported")
    if pipeline.get("mode") not in {
        None,
        "proof",
        "research",
    }:
        raise ValueError("config.pipeline.mode is unsupported")
    if str(pipeline.get("web_search_mode", "on") or "") not in {"off", "on"}:
        raise ValueError("config.pipeline.web_search_mode is unsupported")
    _positive_int(pipeline.get("timeout_seconds"), "config.pipeline.timeout_seconds")
    if pipeline.get("max_proof_states") is not None:
        if type(pipeline["max_proof_states"]) is not int:
            raise ValueError(
                "config.pipeline.max_proof_states must be a positive integer"
            )
        _positive_int(pipeline["max_proof_states"], "config.pipeline.max_proof_states")
    _positive_int(
        pipeline.get("transport_max_retries"),
        "config.pipeline.transport_max_retries",
        allow_zero=True,
    )
    _positive_int(batch.get("parallel"), "config.batch.parallel")

    problems = sorted(problem_dir.glob(problem_glob), key=lambda path: path.name)
    invalid_names = [
        path.name
        for path in problems
        if not profile.problem_id_pattern.fullmatch(path.stem)
    ]
    if invalid_names:
        raise ValueError("invalid problem filenames: " + ", ".join(invalid_names))
    invalid_suffixes = [
        path.name
        for path in problems
        if path.suffix.lower() not in profile.problem_suffixes
    ]
    if invalid_suffixes:
        raise ValueError(
            "unsupported problem filename suffixes: " + ", ".join(invalid_suffixes)
        )
    if len(problems) != expected_count:
        raise ValueError(
            f"expected {expected_count} problems, found {len(problems)} in {problem_dir}"
        )
    if len({path.stem for path in problems}) != len(problems):
        raise ValueError("problem IDs are not unique")
    return dataset, codex, pipeline, batch, problem_dir


def _configured_secret(
    config: dict[str, Any],
    *,
    value_field: str,
) -> str:
    value = str(config.get(value_field) or "").strip()
    if value:
        return value
    env_field = f"{value_field}_env"
    env_name = str(config.get(env_field) or "").strip()
    return str(os.environ.get(env_name) or "").strip() if env_name else ""


def _resolve_real_codex(codex: dict[str, Any]) -> str:
    configured = str(codex.get("cli_path") or "codex").strip()
    if "/" in configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"Codex CLI is not executable: {path}")
        return str(path)
    resolved = shutil.which(configured)
    if not resolved:
        raise ValueError(f"Codex CLI was not found on PATH: {configured}")
    return resolved


def _credential_status(codex: dict[str, Any]) -> dict[str, Any]:
    auth_mode = str(codex["authentication_mode"])
    base_url = _configured_secret(
        codex,
        value_field="base_url",
    )
    api_key = _configured_secret(
        codex,
        value_field="api_key",
    )
    base_url_valid = base_url.startswith(("https://", "http://"))
    ready = auth_mode == "existing_codex_login" or bool(base_url_valid and api_key)
    return {
        "authentication_mode": auth_mode,
        "base_url_configured": bool(base_url),
        "base_url_valid": base_url_valid,
        "api_key_configured": bool(api_key),
        "ready_for_live_run": ready,
        "base_url": base_url,
        "api_key": api_key,
    }


def _normalize_requested_ids(raw: str, profile: SuiteProfile) -> list[str]:
    ids: list[str] = []
    for item in raw.split(","):
        value = item.strip().upper()
        if not value:
            continue
        for suffix in profile.problem_suffixes:
            value = value.removesuffix(suffix.upper())
        if not profile.problem_id_pattern.fullmatch(value):
            raise ValueError(f"invalid problem ID: {item!r}")
        ids.append(value)
    return list(dict.fromkeys(ids))


def _select_problems(
    problem_dir: Path,
    dataset: dict[str, Any],
    profile: SuiteProfile,
    *,
    cli_ids: str,
    limit: int,
) -> list[Path]:
    problem_glob = str(dataset["problem_glob"])
    problems = sorted(problem_dir.glob(problem_glob), key=lambda path: path.name)
    configured_ids = dataset.get("problem_ids") or []
    if not isinstance(configured_ids, list):
        raise TypeError("config.dataset.problem_ids must be an array")
    selected_ids = (
        _normalize_requested_ids(cli_ids, profile)
        if cli_ids.strip()
        else _normalize_requested_ids(
            ",".join(str(item) for item in configured_ids),
            profile,
        )
    )
    if selected_ids:
        by_id = {path.stem: path for path in problems}
        missing = [problem_id for problem_id in selected_ids if problem_id not in by_id]
        if missing:
            raise ValueError("unknown problem IDs: " + ", ".join(missing))
        problems = [by_id[problem_id] for problem_id in selected_ids]
    if limit < 0:
        raise ValueError("--limit must be nonnegative")
    return problems[:limit] if limit else problems


def _output_paths(
    config_file: Path,
    batch: dict[str, Any],
) -> tuple[Path, Path, Path]:
    return (
        _resolve_from_config(
            config_file, batch.get("output_dir"), "config.batch.output_dir"
        ),
        _resolve_from_config(config_file, batch.get("log_dir"), "config.batch.log_dir"),
        _resolve_from_config(
            config_file, batch.get("state_dir"), "config.batch.state_dir"
        ),
    )


def _problem_workspace(output_dir: Path, problem_id: str) -> Path | None:
    problem_root = output_dir / problem_id
    return problem_root if problem_root.is_dir() else None


def _terminal_record(run_dir: Path | None) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    report = run_dir / work_ref(run_dir, "final_report.json")
    if report.is_symlink() or not report.is_file():
        return None
    try:
        value = _read_object(report)
    except (TypeError, ValueError):
        return None
    if str(value.get("run_status") or "").lower() not in TERMINAL_RUN_STATUSES:
        return None
    return value


def _is_resumable_run(run_dir: Path | None) -> bool:
    """Resume the selected core task, independently of Web presentation state."""
    if run_dir is None:
        return False
    try:
        workspace = RunWorkspace.open_existing(run_dir)
        if active_writing_request(run_dir):
            return not writing_complete(run_dir)
        setup = load_setup(run_dir)
        if setup is not None and setup["phase"] != "READY":
            return True
        terminal = _read_json_quiet(run_dir / work_ref(run_dir, "final_report.json"))
        if setup is not None and terminal.get("terminal_kind") == "SETUP_FAILURE":
            return True
        proof_path = run_dir / work_ref(run_dir, "state/proof_state.json")
        if proof_path.is_file():
            proof = _read_object(proof_path)
            if proof.get("engine") != "proof" or proof.get("turn_id") != workspace.turn_id:
                return False
            return bool(proof.get("phase") != "FINALIZE" or not terminal
                        or delivery_pause(run_dir) or pending_finding_checks(run_dir))
        state = load_director_checkpoint(workspace)
        if terminal:
            return bool(load_operational_checkpoint(run_dir, state)
                        or delivery_pause(run_dir) or pending_finding_checks(run_dir))
    except (RecoveryError, OSError, UnicodeError, ValueError):
        return False
    return True


def _read_json_quiet(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl_quiet(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _safe_progress_label(value: Any, *, fallback: str = "pipeline") -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return (text or fallback)[:80]


def _elapsed_text(started: float, *, now: float | None = None) -> str:
    elapsed = max(0, int((time.monotonic() if now is None else now) - started))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _console_print(lock: threading.Lock, message: str) -> None:
    with lock:
        print(message, flush=True)


def _progress_prefix(
    problem_id: str,
    *,
    selection_index: int,
    selection_total: int,
    started: float,
    now: float | None = None,
) -> str:
    return (
        f"[{problem_id} {selection_index}/{selection_total} "
        f"{_elapsed_text(started, now=now)}]"
    )


def _checkpoint_progress(run_dir: Path) -> tuple[tuple[Any, ...], str]:
    proof = _read_json_quiet(run_dir / work_ref(run_dir, "state/proof_state.json"))
    if proof:
        phase = _safe_progress_label(proof.get("phase"), fallback="STARTUP")
        index = int(proof.get("loop_index", 0))
        maximum = int(proof.get("max_loops", 0))
        return ("proof", phase, index, maximum), f"phase={phase} proof_loop={index}/{maximum}"
    state = _read_json_quiet(run_dir / work_ref(run_dir, "state/director_state.json"))
    if not state:
        return (), ""
    phase = _safe_progress_label(state.get("phase"), fallback="STARTUP")
    index = int(state.get("round_index") or 0)
    used = int(state.get("proof_states_used") or 0)
    maximum = int(state.get("max_proof_states") or 0)
    return (
        (phase, index, used, maximum),
        f"phase={phase} round={index} proof_states={used}/{maximum}",
    )


def _initialize_progress(run_dir: Path | None) -> _ProblemProgress:
    state = _ProblemProgress(run_dir=run_dir, last_output_at=time.monotonic())
    if run_dir is None:
        return state
    rows = _read_jsonl_quiet(run_dir / work_ref(run_dir, "events.jsonl"))
    state.seen_event_ids = {
        str(row.get("event_id") or "") for row in rows if str(row.get("event_id") or "")
    }
    state.semantic_started = sum(
        row.get("event_kind") == "SEMANTIC_CALL_STARTED" for row in rows
    )
    state.semantic_finished = sum(
        row.get("event_kind") in _SEMANTIC_TERMINAL_EVENTS for row in rows
    )
    latest_started = next(
        (
            row
            for row in reversed(rows)
            if row.get("event_kind") == "SEMANTIC_CALL_STARTED"
        ),
        {},
    )
    state.latest_stage = _safe_progress_label(
        latest_started.get("stage")
        or latest_started.get("role")
        or latest_started.get("call_name"),
        fallback="resume",
    )
    state.checkpoint_signature = _checkpoint_progress(run_dir)[0]
    return state


def _active_progress_run_dir(
    output_dir: Path,
    problem_id: str,
    *,
    action: str,
    previous_run_dir: Path | None,
) -> Path | None:
    if action == "resume":
        return previous_run_dir
    return _problem_workspace(output_dir, problem_id)


def _report_problem_progress(
    state: _ProblemProgress,
    *,
    problem_id: str,
    selection_index: int,
    selection_total: int,
    action: str,
    previous_run_dir: Path | None,
    output_dir: Path,
    started: float,
    heartbeat_seconds: float,
    console_lock: threading.Lock,
) -> None:
    now = time.monotonic()
    prefix = _progress_prefix(
        problem_id,
        selection_index=selection_index,
        selection_total=selection_total,
        started=started,
        now=now,
    )
    run_dir = _active_progress_run_dir(
        output_dir,
        problem_id,
        action=action,
        previous_run_dir=previous_run_dir,
    )
    if run_dir is not None and run_dir != state.run_dir:
        state.run_dir = run_dir
        state.seen_event_ids.clear()
        state.checkpoint_signature = ()
        _console_print(console_lock, f"{prefix} workspace={run_dir}")
        state.last_output_at = now

    emitted = False
    if state.run_dir is not None:
        rows = _read_jsonl_quiet(
            state.run_dir / work_ref(state.run_dir, "events.jsonl")
        )
        for row in rows:
            event_id = str(row.get("event_id") or "")
            if not event_id or event_id in state.seen_event_ids:
                continue
            state.seen_event_ids.add(event_id)
            kind = str(row.get("event_kind") or "")
            stage = _safe_progress_label(
                row.get("stage") or row.get("role") or row.get("call_name"),
            )
            if kind == "SEMANTIC_CALL_STARTED":
                state.semantic_started += 1
                state.latest_stage = stage
                _console_print(
                    console_lock,
                    f"{prefix} stage={stage} model_call=started "
                    f"calls={state.semantic_finished}/{state.semantic_started}",
                )
                emitted = True
            elif kind in _SEMANTIC_TERMINAL_EVENTS and not bool(row.get("suppressed")):
                state.semantic_finished += 1
                state.latest_stage = stage
                status = _SEMANTIC_TERMINAL_EVENTS[kind]
                _console_print(
                    console_lock,
                    f"{prefix} stage={stage} model_call={status} "
                    f"calls={state.semantic_finished}/{state.semantic_started}",
                )
                emitted = True
            elif (
                kind == "TRANSPORT_ATTEMPT_STARTED"
                and int(row.get("attempt_index") or 0) > 1
            ):
                _console_print(
                    console_lock,
                    f"{prefix} stage={stage} transport_retry="
                    f"{int(row.get('attempt_index') or 0)}",
                )
                emitted = True

        signature, checkpoint_text = _checkpoint_progress(state.run_dir)
        if signature and signature != state.checkpoint_signature:
            state.checkpoint_signature = signature
            _console_print(console_lock, f"{prefix} {checkpoint_text}")
            emitted = True

    if emitted:
        state.last_output_at = now
        return
    if now - state.last_output_at >= heartbeat_seconds:
        location = (
            f"stage={state.latest_stage} calls="
            f"{state.semantic_finished}/{state.semantic_started}"
            if state.run_dir is not None
            else "waiting_for_workspace"
        )
        _console_print(console_lock, f"{prefix} running {location}")
        state.last_output_at = now


def _common_cli_args(
    codex: dict[str, Any],
    pipeline: dict[str, Any],
) -> list[str]:
    args = [
        "--provider",
        str(pipeline["provider"]),
        "--model",
        str(codex["model"]),
        "--reasoning-effort",
        str(codex["reasoning_effort"]),
        "--web-search-mode",
        str(pipeline.get("web_search_mode", "on")),
        "--timeout-seconds",
        str(pipeline["timeout_seconds"]),
    ]
    if bool(pipeline.get("profile")):
        args.append("--profile")
    return args


def _problem_command(
    problem: Path,
    *,
    output_dir: Path,
    codex: dict[str, Any],
    pipeline: dict[str, Any],
    resume_incomplete: bool,
) -> tuple[str, list[str], Path | None, dict[str, Any] | None]:
    problem_id = problem.stem
    latest = _problem_workspace(output_dir, problem_id)
    terminal = _terminal_record(latest)
    resumable = resume_incomplete and _is_resumable_run(latest)
    if terminal is not None and not resumable:
        return "skip", [], latest, terminal
    if latest is not None and not resumable:
        raise ValueError(
            f"existing workspace cannot be resumed: {latest}; select another output directory"
        )

    command = [sys.executable, "-m", "pace"]
    if resumable:
        command.extend(["resume", "--run-dir", str(latest)])
        action = "resume"
    else:
        initial_mode = pipeline.get("mode")
        command.extend(
            [
                "run",
                "--problem",
                str(problem),
                "--problem-id",
                problem_id,
                "--output",
                str(output_dir),
            ]
        )
        if initial_mode is not None:
            command.extend(["--mode", initial_mode])
        if codex.get("resource_variant") is not None:
            command.extend(["--resource-variant", str(codex["resource_variant"])])
        if pipeline.get("max_proof_states") is not None:
            command.extend(["--max-proof-states", str(pipeline["max_proof_states"])])
        for option in ("max_calls", "max_seconds"):
            if pipeline.get(option) is not None:
                _positive_int(pipeline[option], f"config.pipeline.{option}")
                command.extend(["--" + option.replace("_", "-"), str(pipeline[option])])
        action = "run"
    command.extend(_common_cli_args(codex, pipeline))
    return action, command, latest, terminal


def _subprocess_environment(
    codex: dict[str, Any],
    pipeline: dict[str, Any],
    credentials: dict[str, Any],
    real_codex: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_CLI_PATH"] = str(RUN_ROOT / "codex_wrapper.sh")
    environment["NL_PROOF_REAL_CODEX"] = real_codex
    environment["NL_PROOF_CODEX_AUTH_MODE"] = str(codex["authentication_mode"])
    environment["NL_PROOF_CODEX_BASE_URL"] = str(credentials["base_url"])
    environment["NL_PROOF_CODEX_API_KEY"] = str(credentials["api_key"])
    environment["NL_PROOF_CODEX_PROVIDER_NAME"] = str(codex["provider_name"])
    environment["NL_PROOF_CODEX_WIRE_API"] = str(codex["wire_api"])
    environment["NL_PROOF_CODEX_REQUIRES_OPENAI_AUTH"] = (
        "true" if bool(codex.get("requires_openai_auth")) else "false"
    )
    environment["NL_PROOF_CLI_MAX_RETRIES"] = str(pipeline["transport_max_retries"])
    return environment


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _result_from_run(
    problem_id: str,
    *,
    action: str,
    returncode: int,
    output_dir: Path,
    log_file: Path,
    started_at: str,
    duration_seconds: float,
) -> dict[str, Any]:
    run_dir = _problem_workspace(output_dir, problem_id)
    terminal = _terminal_record(run_dir)
    run_status = str((terminal or {}).get("run_status") or "").lower()
    if run_dir is not None and not writing_complete(run_dir):
        status = "writing_incomplete"
    elif returncode != 0:
        status = "process_failed"
    elif run_status in TERMINAL_RUN_STATUSES:
        status = run_status
    else:
        status = "missing_terminal_report"
    return {
        "problem_id": problem_id,
        "action": action,
        "status": status,
        "returncode": returncode,
        "result": str((terminal or {}).get("result") or ""),
        "run_status": run_status,
        "writing_action": active_writing_request(run_dir).get("action", "") if run_dir else "",
        "writing_complete": writing_complete(run_dir) if run_dir else False,
        "run_dir": str(run_dir) if run_dir else "",
        "final_report": str(run_dir / work_ref(run_dir, "final_report.json"))
        if terminal and run_dir
        else "",
        "log_file": str(log_file),
        "started_at": started_at,
        "duration_seconds": round(duration_seconds, 3),
    }


def _terminate_processes(
    active: dict[str, subprocess.Popen[str]],
    lock: threading.Lock,
) -> None:
    with lock:
        processes = list(active.values())
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            terminate_process_tree(process)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"process cleanup failed for {process.pid}: {exc}", file=sys.stderr)


def _run_one(
    problem: Path,
    *,
    selection_index: int,
    selection_total: int,
    command: list[str],
    action: str,
    previous_run_dir: Path | None,
    output_dir: Path,
    log_dir: Path,
    environment: dict[str, str],
    active: dict[str, subprocess.Popen[str]],
    active_lock: threading.Lock,
    console_lock: threading.Lock,
    progress_interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    problem_id = problem.stem
    log_file = log_dir / f"{problem_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    started = time.monotonic()
    progress = _initialize_progress(previous_run_dir if action == "resume" else None)
    prefix = _progress_prefix(
        problem_id,
        selection_index=selection_index,
        selection_total=selection_total,
        started=started,
    )
    _console_print(
        console_lock,
        f"{prefix} started action={action} log={log_file}",
    )
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{started_at}] {shlex.join(command)}\n")
        handle.flush()
        with active_lock:
            # Cancellation and process registration share a boundary: a queued
            # worker cannot launch after the stop request's process snapshot.
            if stop_event is not None and stop_event.is_set():
                raise concurrent.futures.CancelledError()
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            active[problem_id] = process
        try:
            while process.poll() is None:
                _report_problem_progress(
                    progress,
                    problem_id=problem_id,
                    selection_index=selection_index,
                    selection_total=selection_total,
                    action=action,
                    previous_run_dir=previous_run_dir,
                    output_dir=output_dir,
                    started=started,
                    heartbeat_seconds=progress_interval_seconds,
                    console_lock=console_lock,
                )
                time.sleep(PROGRESS_POLL_SECONDS)
            returncode = int(process.returncode or 0)
            _report_problem_progress(
                progress,
                problem_id=problem_id,
                selection_index=selection_index,
                selection_total=selection_total,
                action=action,
                previous_run_dir=previous_run_dir,
                output_dir=output_dir,
                started=started,
                heartbeat_seconds=progress_interval_seconds,
                console_lock=console_lock,
            )
        finally:
            with active_lock:
                active.pop(problem_id, None)
    return _result_from_run(
        problem_id,
        action=action,
        returncode=returncode,
        output_dir=output_dir,
        log_file=log_file,
        started_at=started_at,
        duration_seconds=time.monotonic() - started,
    )


def _write_batch_state(
    path: Path,
    *,
    config_file: Path,
    selected_ids: list[str],
    started_at: str,
    records: list[dict[str, Any]],
    finished: bool,
) -> None:
    _atomic_write_json(
        path,
        {
            "schema_version": RESULT_SCHEMA,
            "config_file": str(config_file),
            "selected_problem_count": len(selected_ids),
            "selected_problem_ids": selected_ids,
            "started_at": started_at,
            "updated_at": _utc_now(),
            "finished": finished,
            "records": sorted(records, key=lambda row: str(row["problem_id"])),
        },
    )


def _run_postmortem(
    output_dir: Path,
    state_dir: Path,
    problem_ids: list[str],
    environment: dict[str, str],
) -> None:
    expected_file = state_dir / "expected_problem_ids.txt"
    expected_file.write_text("\n".join(problem_ids) + "\n", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "pace",
        "postmortem",
        "--batch-dir",
        str(output_dir),
        "--expected-problems-file",
        str(expected_file),
        "--inactive",
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    (state_dir / "postmortem.log").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"postmortem failed with exit code {completed.returncode}; "
            f"see {state_dir / 'postmortem.log'}"
        )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        profile = _suite_profile_from_argv(raw_argv)
    except (TypeError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    args = _parser(profile).parse_args(raw_argv)
    config_file = args.config.expanduser().resolve()
    try:
        config = _read_object(config_file)
        dataset, codex, pipeline, batch, problem_dir = _validate_config(
            config_file,
            config,
            profile,
        )
        problems = _select_problems(
            problem_dir,
            dataset,
            profile,
            cli_ids=args.ids,
            limit=args.limit,
        )
        if not problems:
            raise ValueError("no problems were selected")
        output_dir, log_dir, state_dir = _output_paths(config_file, batch)
        real_codex = _resolve_real_codex(codex)
        credentials = _credential_status(codex)
        parallel = args.parallel or int(batch["parallel"])
        if parallel <= 0:
            raise ValueError("parallelism must be positive")
        progress_interval_seconds = float(args.progress_interval_seconds)
        if progress_interval_seconds <= 0:
            raise ValueError("--progress-interval-seconds must be positive")
    except (TypeError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    check = {
        "schema_version": CONFIG_SCHEMA,
        "suite_profile": str(profile.file),
        "config_file": str(config_file),
        "dataset": str(dataset.get("name") or ""),
        "problem_dir": str(problem_dir),
        "discovered_problem_count": int(dataset["expected_problem_count"]),
        "selected_problem_count": len(problems),
        "first_problem_id": problems[0].stem,
        "last_problem_id": problems[-1].stem,
        "parallel": parallel,
        "progress_interval_seconds": progress_interval_seconds,
        "codex_cli": real_codex,
        "authentication_mode": credentials["authentication_mode"],
        "base_url_configured": credentials["base_url_configured"],
        "api_key_configured": credentials["api_key_configured"],
        "ready_for_live_run": credentials["ready_for_live_run"],
    }
    if args.check:
        print(json.dumps(check, ensure_ascii=False, indent=2))
        return 0

    resume_incomplete = bool(batch.get("resume_incomplete")) and not args.no_resume
    work: list[tuple[int, Path, str, list[str], Path | None]] = []
    records: list[dict[str, Any]] = []
    for selection_index, problem in enumerate(problems, start=1):
        action, command, latest, terminal = _problem_command(
            problem,
            output_dir=output_dir,
            codex=codex,
            pipeline=pipeline,
            resume_incomplete=resume_incomplete,
        )
        if action == "skip":
            records.append(
                {
                    "problem_id": problem.stem,
                    "action": "skip",
                    "status": "already_terminal",
                    "returncode": 0,
                    "result": str((terminal or {}).get("result") or ""),
                    "run_status": str((terminal or {}).get("run_status") or ""),
                    "run_dir": str(latest) if latest else "",
                    "final_report": str(latest / work_ref(latest, "final_report.json"))
                    if latest
                    else "",
                    "log_file": "",
                    "started_at": "",
                    "duration_seconds": 0.0,
                }
            )
        else:
            work.append((selection_index, problem, action, command, latest))

    if args.dry_run:
        print(json.dumps(check, ensure_ascii=False, indent=2))
        for _, problem, action, command, _ in work:
            print(f"[{problem.stem}] {action}: {shlex.join(command)}")
        for record in records:
            print(f"[{record['problem_id']}] skip: terminal result={record['result']}")
        return 0

    if not bool(credentials["ready_for_live_run"]):
        print(
            "configuration error: live execution is not ready; fill codex.base_url "
            "and codex.api_key in config.local.json, or set "
            "codex.authentication_mode to existing_codex_login",
            file=sys.stderr,
        )
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    state_file = state_dir / "batch_results.json"
    selected_ids = [problem.stem for problem in problems]
    _write_batch_state(
        state_file,
        config_file=config_file,
        selected_ids=selected_ids,
        started_at=started_at,
        records=records,
        finished=False,
    )
    environment = _subprocess_environment(
        codex,
        pipeline,
        credentials,
        real_codex,
    )
    active: dict[str, subprocess.Popen[str]] = {}
    active_lock = threading.Lock()
    console_lock = threading.Lock()
    _console_print(
        console_lock,
        f"batch start: selected={len(selected_ids)} runnable={len(work)} "
        f"skipped={len(records)} parallel={parallel} "
        f"heartbeat={progress_interval_seconds:g}s",
    )

    stop_event = threading.Event()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=parallel)
    futures = {}
    try:
        futures = {
            executor.submit(
                _run_one,
                problem,
                selection_index=selection_index,
                selection_total=len(selected_ids),
                command=command,
                action=action,
                previous_run_dir=latest,
                output_dir=output_dir,
                log_dir=log_dir,
                environment=environment,
                active=active,
                active_lock=active_lock,
                console_lock=console_lock,
                progress_interval_seconds=progress_interval_seconds,
                stop_event=stop_event,
            ): (problem.stem, selection_index)
            for selection_index, problem, action, command, latest in work
        }
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            records.append(record)
            _, selection_index = futures[future]
            _console_print(
                console_lock,
                f"[{record['problem_id']} {selection_index}/{len(selected_ids)}] "
                f"finished status={record['status']} "
                f"result={record['result'] or '-'} "
                f"elapsed={record['duration_seconds']:.1f}s "
                f"log={record['log_file']}",
            )
            _write_batch_state(
                state_file,
                config_file=config_file,
                selected_ids=selected_ids,
                started_at=started_at,
                records=records,
                finished=False,
            )
            failures = sum(
                row["status"] not in {"completed", "already_terminal"}
                for row in records
            )
            _console_print(
                console_lock,
                f"[batch] settled={len(records)}/{len(selected_ids)} "
                f"failures={failures} remaining={len(selected_ids) - len(records)}",
            )
    except BaseException as exc:
        stop_event.set()
        for future in futures:
            future.cancel()
        _terminate_processes(active, active_lock)
        if isinstance(exc, KeyboardInterrupt):
            print(
                "batch interrupted; active problem processes were terminated",
                file=sys.stderr,
            )
            return 130
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    _write_batch_state(
        state_file,
        config_file=config_file,
        selected_ids=selected_ids,
        started_at=started_at,
        records=records,
        finished=True,
    )
    try:
        _run_postmortem(output_dir, state_dir, selected_ids, environment)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    failed = [
        record
        for record in records
        if record["status"] not in {"completed", "already_terminal"}
    ]
    print(
        f"batch complete: selected={len(selected_ids)} infrastructure_failures={len(failed)} "
        f"state={state_file} summary={output_dir / '_runner_summary.json'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
