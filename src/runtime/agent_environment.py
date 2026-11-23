"""Turn-owned role homes and explicitly owned native conversations."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pace.artifacts.workspace import (
    _write_json_atomic,
    _write_text_atomic,
    current_turn,
)

_ROLE_PATHS = {
    "GLOBAL_RESEARCH_DIRECTOR": "director",
    "DIRECTOR_LED_FRAMER": "framer",
    "SURVEY": "survey",
    "PLANNER": "planner",
    "RESEARCH": "research",
    "FINDING_VERIFIER": "finding_verifier",
    "PROBLEM_ORIENTATION_EXTRACTOR": "orientation/extractor",
    "PROBLEM_RESEARCH_DECISION": "orientation/assessment",
    "WHOLE_PROVER": "prover/prover",
    "PROOF_CONTRACT_AUDITOR": "prover/auditor",
    "MATH_VERIFIER_KERNEL": "prover/kernel",
    "RESEARCH_REPORT_OUTLINE": "writing/report_outline",
    "RESEARCH_REPORT_WRITER": "writing/report_writer",
    "THEORY_PAPER_OUTLINE": "writing/paper_outline",
    "THEORY_PAPER_WRITER": "writing/paper_writer",
    "RESEARCH_REPORTER": "reporter",
    "WEB_REPORTER": "writing/web_reporter",
    "RCI_SEED_SCOUT": "rci/seed_scout",
    "RCI_RAW_SEED_CRITIC_MATH": "rci/raw_seed_critic_math",
    "RCI_RAW_SEED_CRITIC_RESEARCH": "rci/raw_seed_critic_research",
    "RCI_FRONTIER_EXPLORER": "rci/frontier_explorer",
    "RCI_FRONTIER_REFUTE_PROBE": "rci/frontier_refute_probe",
    "RCI_MECHANISM_CRITIC": "rci/mechanism_critic",
    "RCI_COVERAGE_JUDGE": "rci/coverage_judge",
    "RCI_FINAL_CHALLENGER": "rci/final_challenger",
    "RCI_FRONTIER_HARDENER": "rci/frontier_hardener",
    "RCI_BLIND_PROVER_PROBE": "rci/blind_prover_probe",
    "RCI_REFUTE_PROVER_PROBE": "rci/refute_prover_probe",
    "RCI_STATEMENT_NORMALIZER": "rci/statement_normalizer",
    "RCI_SEMANTIC_DEDUP": "rci/semantic_dedup",
}


def role_home(
    workdir: Path, role: str, *, environment_root: Path | None = None
) -> Path | None:
    """Resolve a role's fixed environment; utility calls have no role home."""
    definition = _ROLE_PATHS.get(role.upper())
    if definition is None:
        return None
    root = Path(workdir).resolve()
    environment_root = environment_root or Path(
        os.environ.get("PACE_CODEX_ENV_ROOT", root.parent / ".nl-proof-acp")
    )
    return environment_root / "runs" / root.name / current_turn(root) / definition


def prepare_role_environment(workdir: Path, role: str, *, instructions: str) -> None:
    """Persist caller-composed instructions; runtime does not select role policy."""
    path = role_home(workdir, role) / "AGENTS.md"
    if not path.is_file() or path.read_text(encoding="utf-8") != instructions:
        _write_text_atomic(path, instructions)


def director_thread(home: Path) -> str:
    path = home / "session.json"
    return (
        json.loads(path.read_text(encoding="utf-8"))["thread_id"]
        if path.is_file()
        else ""
    )


def resumed_call(workdir: Path, record: dict) -> dict | None:
    """Read the caller-selected conversation, bound to the same task and role."""
    ref = record.get("resume_from_call_ref")
    if not ref:
        return None
    previous = json.loads((workdir / ref).read_text(encoding="utf-8"))
    for key in ("role", "task_id", "stage_ref", "agent_home"):
        if previous[key] != record[key]:
            raise ValueError(f"session continuation changed {key}")
    return previous


def native_thread(workdir: Path, record_path: Path) -> str:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record["role"].upper() == "GLOBAL_RESEARCH_DIRECTOR":
        return director_thread(Path(record["agent_home"]))
    # An internal retry continues this logical call, never the role's last call.
    while not record["thread_id"]:
        record = resumed_call(workdir, record)
        if record is None:
            return ""
    return record["thread_id"]


def record_native_session(
    *,
    home: Path,
    role: str,
    stdout_path: Path,
    record_path: Path,
    expected_thread_id: str = "",
) -> None:
    """Persist the native ID even when an invocation times out or is interrupted."""
    for line in stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "thread.started":
            continue
        thread_id = event["thread_id"]
        if expected_thread_id and thread_id != expected_thread_id:
            raise ValueError("resumed a different native session")
        if role.upper() == "GLOBAL_RESEARCH_DIRECTOR":
            _write_json_atomic(home / "session.json", {"thread_id": thread_id})
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.update(thread_id=thread_id)
        _write_json_atomic(record_path, record)
        return


def call_environment(workdir: Path, role: str, metadata: dict | None = None) -> dict:
    """Bind the native home, retaining an explicit caller-owned display environment."""
    values = dict(metadata or {})
    if not values.get("agent_home"):
        home = role_home(workdir, role)
        values["agent_home"] = str(home) if home else ""
    return values
