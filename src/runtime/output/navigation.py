"""Regenerable factual navigation; never an input to acceptance or recovery."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from datetime import datetime

from .workspace import _write_text_atomic, _write_json_atomic, current_turn


_NAVIGATION_LOCK = threading.RLock()

def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text()) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _link(root: Path, page: Path, ref: str, label: str = "") -> str:
    target = root / ref
    if not target.is_file() and not target.is_dir():
        return ""
    relative = Path(os.path.relpath(target, page.parent)).as_posix()
    label = (label or target.name).replace("|", "\\|").replace("\n", " ")
    return f"[{label}](<{relative}>)"


def _write(path: Path, lines: list[str]) -> None:
    body = "\n".join(lines).rstrip() + "\n"
    if not path.is_file() or path.read_text() != body:
        _write_text_atomic(path, body)


def refresh_navigation(root: Path, *, stage: str = "", rebuild: bool = False) -> None:
    """Update the affected stage, current turn and run; errors never block science."""
    try:
        with _NAVIGATION_LOCK:
            _refresh(Path(root), stage=stage, rebuild=rebuild)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logging.getLogger(__name__).warning("Navigation update failed; use pace index to rebuild", exc_info=True)


def _refresh(root: Path, *, stage: str, rebuild: bool) -> None:
    turn_id = current_turn(root)
    turns = sorted((root / "work").glob("turn[0-9]*")) if rebuild else [root / "work" / turn_id]
    for turn in turns:
        base = turn
        rounds = sorted(p for p in base.iterdir() if p.is_dir() and
                        (p.name.startswith("round") or p.name in {"orientation", "finalization"}))
        page = turn / "README.md"
        from .events import read_events
        events = read_events(root, turn_id=turn.name)
        cache_path = turn / "state/navigation.json"
        cache = {} if rebuild else _read(cache_path).get("stages", {})
        owners, starts, seconds, progress = {}, {}, {}, {}
        # Only transport intervals with both endpoints count. This is summed
        # model time, not wall time; concurrent calls may overlap.
        for event in events:
            call = event.get("call_id", "")
            ref = event.get("call_ref", "")
            if ref and "/calls/" in ref:
                owners[call] = ref.split("/calls/", 1)[0]
            key = (call, event.get("invocation_id"), event.get("attempt_index"))
            if event["event_kind"] == "TRANSPORT_ATTEMPT_STARTED":
                starts[key] = event["timestamp"]
            elif event["event_kind"] == "TRANSPORT_ATTEMPT_FINISHED" and key in starts and call in owners:
                duration = (datetime.fromisoformat(event["timestamp"]) - datetime.fromisoformat(starts.pop(key))).total_seconds()
                owner = owners[call]
                seconds[owner] = seconds.get(owner, 0.0) + max(0.0, duration)
            elif event["event_kind"] == "PROGRESS":
                progress[event["stage_ref"]] = event
        lines = [f"# {turn.name}", "", "Rounds are dispatches, not user turns or proof-budget units.", "",
                 "Model time sums recorded transport intervals; parallel calls may overlap. Missing intervals are not estimated.", "",
                 "| Stage | Role / task | State | Recorded model time (s) |", "|---|---|---|---|"]
        for directory in rounds:
            relative = directory.relative_to(root).as_posix()
            update = rebuild or relative == stage or relative not in cache or not (directory / "README.md").exists()
            if update:
                assignment = _read(directory / "task.json")
                task = assignment.get("task", assignment.get("decision", assignment))
                result = _read(directory / "agent_result.json")
                cache[relative] = {"role": result.get("agent_id") or task.get("agent_id", directory.name),
                                   "status": result.get("status", "recorded")}
            row = cache[relative]
            observed = progress.get(relative, {})
            status = observed.get("status", row["status"])
            role = row["role"]
            elapsed = f"{seconds[relative]:.1f}" if relative in seconds else "—"
            lines.append(f"| {_link(root, page, relative + '/README.md', directory.name) or directory.name} | {role} | {status} | {elapsed} |")
            if update:
                child = directory / "README.md"
                entries = [f"# {directory.name}", "", f"Role: {role}; state: {status}.", "",
                           "Generated from committed records. Links confer no mathematical authority.", ""]
                refs = ["task.json", "context.json", "route_context.json", "agent_result.json",
                        "verification", "findings", "calls", "diagnostics.json", "specialist_failure.json"]
                refs += [p.name for p in sorted(directory.iterdir()) if p.is_file() and p.suffix == ".md" and p.name != "README.md"]
                refs += [p.name for p in sorted(directory.iterdir()) if p.is_dir() and directory.name == "finalization"]
                for ref in dict.fromkeys(refs):
                    link = _link(root, child, relative + "/" + ref)
                    if link:
                        entries.append("- " + link)
                if result.get("deliverable_ref"):
                    link = _link(root, child, result["deliverable_ref"], "Main deliverable")
                    if link:
                        entries.append("- " + link)
                _write(child, entries)
        _write_json_atomic(cache_path, {"stages": cache})
        # Stage pages may have just been created. Build links without another scan.
        for directory in rounds:
            lines = [line.replace(f"| {directory.name} |", f"| {_link(root, page, directory.relative_to(root).as_posix() + '/README.md', directory.name)} |", 1) for line in lines]
        for ref in ("final_report.json", "state/writing_task.json"):
            link = _link(root, page, turn.relative_to(root).as_posix() + "/" + ref)
            if link:
                lines.extend(["", "- " + link])
        _write(page, lines)
    page = root / "README.md"
    state = _read(root / "work/state/current_turn.json")
    policy = _read(root / "work" / turn_id / "state/research_execution_policy.json")
    director = _read(root / "work" / turn_id / "state/director_state.json")
    proof = _read(root / "work" / turn_id / "state/proof_state.json")
    lines = [f"# {root.name}", "", f"Current turn: {turn_id}; execution: {state.get('status', 'unknown')}.",
             f"Mode: {policy.get('mode', 'initializing')}; dispatch round: {director.get('round_index', proof.get('loop_index', 0))}.",
             f"Proof budget used: {director.get('proof_states_used', proof.get('loops_started', 0))}; "
             f"limit: {director.get('max_proof_states', proof.get('max_loops', 'not allocated'))}.", "",
             "This page is navigation, not a proof or an execution checkpoint.", ""]
    task_ref = director.get("pending_assignment_ref") or director.get("last_assignment_ref", "")
    if task_ref:
        link = _link(root, page, task_ref, "Current / latest task")
        if link:
            lines.extend([f"Research phase: {director.get('phase', 'unknown')}. {link}", ""])
    if proof:
        lines.extend([f"Proof phase: {proof.get('phase', 'unknown')}; loop: {proof.get('loop_index', 0)}.", ""])
    for ref in (f"work/{turn_id}/README.md", f"work/{turn_id}/final_report.json", f"work/{turn_id}/state/writing_task.json",
                "results/findings/README.md", "results/findings/catalog.md", "results/findings/archive.md",
                "results/outline.md", "results/research_report.md", "results/paper_outline.md",
                "results/paper/main.tex", "results/paper/main.pdf", "memory/README.md"):
        link = _link(root, page, ref, ref)
        if link:
            lines.append("- " + link)
    _write(page, lines)
