"""Stop owned POSIX subprocess trees, including detached model and tool groups."""

from __future__ import annotations

import os
import signal
import subprocess
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import json


logger = logging.getLogger(__name__)
_calls_lock = threading.Lock()
_call_scopes: dict[Path, "ModelCallCancellation"] = {}


def _process_identity(pid: int):
    row = subprocess.run(["ps", "-p", str(pid), "-o", "pid=,pgid=,stat=,lstart="],
                         capture_output=True, text=True, timeout=5, check=False,
                         env={**os.environ, "LC_ALL": "C"})
    if row.returncode == 1 or not row.stdout.strip():
        return None
    if row.returncode:
        raise OSError("Could not inspect the saved model process")
    fields = row.stdout.strip().split(maxsplit=3)
    if len(fields) != 4:
        raise OSError("Unexpected process identity output")
    return {"pid": int(fields[0]), "process_group": int(fields[1]),
            "state": fields[2], "started_identity": fields[3]}


def assert_no_active_model_processes(workdir: Path):
    """Refuse recovery until a previous owned model has actually stopped."""
    from pace.runtime.output.events import append_event

    directory = Path(workdir) / "work/state/model_processes"
    for path in directory.glob("*.json"):
        saved = json.loads(path.read_text(encoding="utf-8"))
        actual = _process_identity(saved["pid"])
        if (actual and not actual["state"].startswith("Z")
                and actual["started_identity"] == saved["started_identity"]
                and actual["process_group"] == saved["process_group"]):
            raise ValueError(f"A previous owned process is still active (PID {saved['pid']}); stop it before recovery or continuation")
        path.unlink()
        append_event(workdir, "MODEL_PROCESS_EXIT_UNOBSERVED" if saved.get("kind", "model") == "model" else "HOST_TOOL_PROCESS_EXIT_UNOBSERVED", pid=saved["pid"],
                     process_group=saved["process_group"], exit_code=None,
                     kind=saved.get("kind", "model"),
                     saved_started_identity=saved["started_identity"],
                     reason="saved process identity is no longer running")


@dataclass
class ModelCallCancellation:
    cancelled: threading.Event = field(default_factory=threading.Event)
    processes: set[subprocess.Popen] = field(default_factory=set)

    def cancel(self) -> None:
        with _calls_lock:
            self.cancelled.set()
            processes = tuple(self.processes)
        for process in processes:
            try:
                terminate_process_tree(process)
            except (OSError, subprocess.SubprocessError):
                logger.warning("Could not fully stop a cancelled model process", exc_info=True)


def cancel_model_calls(workdir: Path) -> None:
    with _calls_lock:
        scope = _call_scopes.get(Path(workdir).resolve())
    if scope is not None:
        scope.cancel()


@contextmanager
def cancellable_model_calls(workdir: Path):
    """Cancel this run's workers before the thread pool waits for them."""
    root = Path(workdir).resolve()
    scope = ModelCallCancellation()
    with _calls_lock:
        if root in _call_scopes:
            raise ValueError("model-call cancellation scope already active for this run")
        _call_scopes[root] = scope
    try:
        yield scope
    finally:
        with _calls_lock:
            _call_scopes.pop(root, None)


@contextmanager
def tracked_model_process(workdir: Path, process: subprocess.Popen, *, kind="model"):
    from pace.runtime.output.workspace import _write_json_atomic, utc_now
    from pace.runtime.output.events import append_event

    path = Path(workdir) / f"work/state/model_processes/{process.pid}.json"
    has_workspace = (Path(workdir) / "work/state/current_turn.json").is_file()
    identity = _process_identity(process.pid) if has_workspace else None
    if identity and not identity["state"].startswith("Z"):
        saved = {**identity, "kind": kind, "owner_pid": os.getpid(), "observed_at": utc_now()}
        _write_json_atomic(path, saved)
        append_event(workdir, "MODEL_PROCESS_STARTED" if kind == "model" else "HOST_TOOL_PROCESS_STARTED", **saved)
    with _calls_lock:
        scope = _call_scopes.get(Path(workdir).resolve())
        if scope is not None:
            scope.processes.add(process)
    try:
        yield scope.cancelled if scope is not None else None
    except BaseException:
        # Reap before recording the exit, so a timeout/cancellation retains the
        # OS result instead of looking like a Host that disappeared unobserved.
        try:
            terminate_process_tree(process)
        except (OSError, subprocess.SubprocessError):
            logger.warning("Owned process cleanup failed", exc_info=True)
        raise
    finally:
        if scope is not None:
            with _calls_lock:
                scope.processes.discard(process)
        if path.is_file() and process.poll() is not None:
            try:
                path.unlink()
                append_event(workdir, "MODEL_PROCESS_FINISHED" if kind == "model" else "HOST_TOOL_PROCESS_FINISHED",
                             pid=process.pid, started_identity=identity["started_identity"], exit_code=process.returncode)
            except OSError:
                logger.warning("Could not finalize model process observation", exc_info=True)


def run_owned_process(cmd, *, workdir, cwd, env, input, stdout, stderr, timeout, kind="model"):
    """Use the same lifetime/lock boundary for models and Host build tools."""
    from pace.runtime.output.workspace import execution_lock_fds

    with subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout,
                          stderr=stderr, text=True, encoding="utf-8", errors="replace",
                          start_new_session=True, pass_fds=execution_lock_fds(workdir)) as process:
        cancelled = None
        try:
            with tracked_model_process(workdir, process, kind=kind) as cancelled:
                if cancelled is not None and cancelled.is_set():
                    raise KeyboardInterrupt()
                process.communicate(input=input, timeout=timeout)
                if cancelled is not None and cancelled.is_set():
                    raise KeyboardInterrupt()
        except BaseException:
            try:
                terminate_process_tree(process)
            except (OSError, subprocess.SubprocessError):
                logger.warning("Owned process cleanup failed", exc_info=True)
            if cancelled is not None and cancelled.is_set():
                raise KeyboardInterrupt() from None
            raise
        return subprocess.CompletedProcess(cmd, process.returncode)


def terminate_process_tree(process: subprocess.Popen) -> None:
    """Freeze, kill and reap a child launched in its own session."""
    if process.poll() is not None:
        return
    pending = [process.pid]
    groups = {process.pid}
    try:
        os.killpg(process.pid, signal.SIGSTOP)
        while pending:
            pid = pending.pop()
            try:
                # Freeze the parent before discovering its children, so a
                # detached tool cannot fork past the traversal.
                os.kill(pid, signal.SIGSTOP)
                groups.add(os.getpgid(pid))
            except ProcessLookupError:
                continue
            # ps exposes the same parent relation on macOS, Linux and WSL.
            table = subprocess.run(
                ["ps", "-axo", "pid=,ppid="], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=5,
            )
            pending.extend(
                int(child) for line in table.stdout.splitlines()
                for child, parent in (line.split(),) if int(parent) == pid
            )
    except ProcessLookupError:
        pass
    finally:
        # Cleanup must still terminate the launcher if process discovery fails.
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
