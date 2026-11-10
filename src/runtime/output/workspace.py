from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_OWNED_LOCKS = ContextVar("pace_owned_execution_locks", default=())
_EXECUTION_FDS = {}
_EXECUTION_FDS_MUTEX = threading.Lock()


def execution_lock_fds(root: Path) -> tuple[int, ...]:
    """Keep the run locked in owned model children if their Host is killed."""
    with _EXECUTION_FDS_MUTEX:
        fd = _EXECUTION_FDS.get((os.getpid(), str(Path(root).resolve())))
    return (fd,) if fd is not None else ()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


CURRENT_TURN_REF = "work/state/current_turn.json"


def _record_unobserved_exit(root: Path, *, reason: str) -> None:
    """Under the execution lock, correct operational state without changing math."""
    path = root / CURRENT_TURN_REF
    if not path.is_file():
        return
    state = json.loads(path.read_text())
    if state.get("status") != "running":
        return
    state.update(status="interrupted", updated_at=utc_now(), interruption_reason=reason)
    _write_json_atomic(path, state)
    sessions = root / work_ref(root, "state/sessions.json")
    if sessions.is_file():
        rows = json.loads(sessions.read_text())
        for row in rows:
            if row.get("status") == "running" and not row.get("ended_at"):
                row.update(status="end_unobserved", observed_inactive_at=utc_now(),
                           measurement_gap="Host is no longer running. Exit time and OS exit code were not observed.")
        _write_json_atomic(sessions, rows)
    from pace.runtime.output.events import append_event

    append_event(root, "EXECUTION_INTERRUPTED", reason=reason, exit_code=None)


def reconcile_execution_state(root: Path) -> dict:
    """Inspect liveness on list/open/resume; never infer death from elapsed time."""
    root = Path(root).resolve()
    path = root / CURRENT_TURN_REF
    if any(owner[0] == os.getpid() and owner[2] == str(root) for owner in _OWNED_LOCKS.get()):
        return json.loads(path.read_text())
    with (root / "work/state/execution.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return json.loads(path.read_text())
        try:
            from pace.runtime.processes import assert_no_active_model_processes

            try:
                assert_no_active_model_processes(root)
            except ValueError:
                # Legacy workers may not inherit the lock. Their PID/start
                # identity must be gone before a run can be resumed.
                return json.loads(path.read_text())
            _record_unobserved_exit(root, reason="execution lock is free and no owned worker is alive")
            execution = root / "work/state/execution.json"
            if execution.is_file():
                row = json.loads(execution.read_text())
                if row.get("status") == "running":
                    row.update(status="end_unobserved", observed_inactive_at=utc_now())
                    _write_json_atomic(execution, row)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    return json.loads(path.read_text())


def current_turn(root: Path) -> str:
    return json.loads((Path(root) / CURRENT_TURN_REF).read_text(encoding="utf-8"))["turn_id"]


def work_ref(root: Path, relative: str = "", *, turn_id: str | None = None) -> str:
    prefix = f"work/{turn_id or current_turn(root)}"
    return f"{prefix}/{relative}" if relative else prefix


def stage_ref(round_index: int = 0, *, workdir: Path, final: bool = False) -> str:
    """Name the Host-selected stage without inferring ownership from a path."""
    name = (
        "finalization"
        if final
        else f"{round_index:04d}"
        if round_index
        else "orientation"
    )
    return f"{work_ref(workdir, 'stages')}/{name}"


def path_has_symlink_component(root: Path, path: Path) -> bool:
    """Return whether ``path`` uses a symbolic-link component below ``root``."""

    root_path = Path(root)
    candidate = Path(path)
    try:
        relative = candidate.relative_to(root_path)
    except ValueError:
        return True
    current = root_path
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def is_call_path(path: Path) -> bool:
    """Call traces are provenance, never mathematical artifact inputs."""
    return "calls" in path.parts and "stages" in path.parts


def is_private_transport_path(path: Path) -> bool:
    """Exclude call traces and private transport files from mathematical artifacts."""
    return is_call_path(path) or path.name.startswith("private_")


def sanitize_id(raw: str) -> str:
    text = raw.strip().replace("\\", "/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("ids must not contain '..'")
        cleaned = re.sub(r"\s+", "_", part)
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
        cleaned = re.sub(r"_+", "_", cleaned).strip("._")
        if cleaned:
            parts.append(cleaned)
    return "_".join(parts) or "problem"


@dataclass
class RunWorkspace:
    root: Path
    problem_id: str

    @classmethod
    def create(
        cls,
        output_root: Path,
        problem_id: str,
    ) -> "RunWorkspace":
        safe_problem_id = sanitize_id(problem_id)
        root = Path(output_root).expanduser().resolve() / safe_problem_id
        root.mkdir(parents=True, exist_ok=False)
        workspace = cls(root=root, problem_id=safe_problem_id)
        for relative in ("memory", "results/findings", "work/state"):
            workspace.path(relative).mkdir(parents=True)
        workspace.start_turn()
        return workspace

    @property
    def turn_id(self) -> str:
        return current_turn(self.root)

    def work_ref(self, relative: str = "") -> str:
        return work_ref(self.root, relative)

    def start_turn(self) -> str:
        record = self.path(CURRENT_TURN_REF)
        index = int(current_turn(self.root)[4:]) + 1 if record.is_file() else 1
        turn_id = f"turn{index:03d}"
        self.path("work", turn_id).mkdir(exist_ok=False)
        for relative in ("input", "state", "stages"):
            self.path("work", turn_id, relative).mkdir()
        self.write_json(CURRENT_TURN_REF, {"turn_id": turn_id, "status": "running", "updated_at": utc_now()})

        from pace.runtime.output.events import initialize_events

        initialize_events(self.root)

        return turn_id

    def finish_turn(self, status: str) -> None:
        self.write_json(CURRENT_TURN_REF, {"turn_id": self.turn_id, "status": status, "updated_at": utc_now()})

    @contextmanager
    def execution_lock(self):
        owner = (os.getpid(), threading.get_ident(), str(self.root.resolve()))
        if owner in _OWNED_LOCKS.get():
            yield
            return
        with self.path("work/state/execution.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("This problem already has an active exploration") from exc
            token = None
            try:
                from pace.runtime.processes import assert_no_active_model_processes, _process_identity

                assert_no_active_model_processes(self.root)
                # A previous session may have died before its finally block.
                if self.path(self.work_ref("state/sessions.json")).is_file():
                    _record_unobserved_exit(self.root, reason="previous execution ended without closing the turn")
                token = _OWNED_LOCKS.set((*_OWNED_LOCKS.get(), owner))
                execution = {"status": "running", "started_at": utc_now(),
                             "host": _process_identity(os.getpid())}
                self.write_json("work/state/execution.json", execution)
                with _EXECUTION_FDS_MUTEX:
                    _EXECUTION_FDS[(owner[0], owner[2])] = stream.fileno()
                yield
            finally:
                if token is not None:
                    _record_unobserved_exit(self.root, reason="execution ended with unfinished turn work")
                    execution.update(status="ended", ended_at=utc_now())
                    self.write_json("work/state/execution.json", execution)
                    _OWNED_LOCKS.reset(token)
                    with _EXECUTION_FDS_MUTEX:
                        _EXECUTION_FDS.pop((owner[0], owner[2]), None)
                fcntl.flock(stream, fcntl.LOCK_UN)

    @classmethod
    def open_existing(cls, root: Path) -> "RunWorkspace":
        """Open a run and reconcile stale operational state, preserving math."""

        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"run directory does not exist: {resolved}")
        if not (resolved / work_ref(resolved, "input/problem.md")).is_file():
            raise ValueError(f"run directory is missing problem.md: {resolved}")
        reconcile_execution_state(resolved)
        return cls(
            root=resolved,
            problem_id=resolved.name,
        )

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def write_text(self, relative: str, content: str) -> Path:
        target = self.path(*relative.replace("\\", "/").split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_text_atomic(self, relative: str, content: str) -> Path:
        target = self.path(*relative.replace("\\", "/").split("/"))
        _write_text_atomic(target, content)
        return target

    def write_json(self, relative: str, payload: Any) -> Path:
        target = self.path(*relative.replace("\\", "/").split("/"))
        _write_json_atomic(target, payload)
        return target


def problem_title(root: Path) -> str:
    """Read the lasting topic from the first turn, including before Orientation."""

    first_input = Path(root) / "work/turn001/input"
    brief = first_input / "problem_brief.md"
    if brief.is_file():
        heading = next(
            (
                line[2:].strip()
                for line in brief.read_text(encoding="utf-8").splitlines()
                if line.startswith("# ")
            ),
            "",
        )
        if heading:
            return heading[:60]
    request = first_input / "request.md"
    if request.is_file():
        first_line = next(
            (
                line.strip().removeprefix("# ").strip()
                for line in request.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ),
            "新研究题目",
        )
        return first_line[:60]
    return "新研究题目"


def list_problems(output_root: Path) -> list[dict]:
    rows = []
    for record in sorted(Path(output_root).glob(f"*/{CURRENT_TURN_REF}")):
        root = record.parents[2]
        state = reconcile_execution_state(root)
        rows.append({"problem_id": root.name, "run_dir": str(root.resolve()), **state,
                     "title": problem_title(root),
                     "report": str(root / work_ref(root, "outcome.md"))})
    return rows


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(content or ""))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
