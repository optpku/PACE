from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pace.runtime.output.workspace import sanitize_id


@dataclass
class Problem:
    problem_id: str
    text: str
    source: str


class ProblemInputError(ValueError):
    error_code = "invalid_problem_input"


def load_problem(path: Path, problem_id: str | None = None) -> Problem:
    """Load exactly one self-contained UTF-8 text problem."""

    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise ProblemInputError(f"problem input must be one regular file: {source_path}")
    payload = source_path.read_bytes()
    if payload.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
        raise ProblemInputError("problem input must be text; PDF input is not supported")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProblemInputError("problem input must be a UTF-8 text file") from exc
    return Problem(
        problem_id=sanitize_id(problem_id or source_path.stem),
        text=text,
        source=str(source_path),
    )


def canonical_problem_text(value: object) -> str:
    """Use the run-wide LF representation for the stored literal problem."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
