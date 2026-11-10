#!/usr/bin/env python3
"""Project recorded outcomes for each problem's current turn; never infer a verdict."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pace.runtime.output.workspace import list_problems
from pace.reporting.writing import active_writing_request, writing_artifacts, writing_complete


def read_optional(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def collect_results(runs_dir: Path) -> list[dict]:
    rows = []
    for item in list_problems(runs_dir):
        root = Path(item["run_dir"])
        turn_ref = f"work/{item['turn_id']}"
        final_ref = f"{turn_ref}/final_report.json"
        report = read_optional(root / final_ref)
        findings = read_optional(root / "work/state/findings.json")
        calls = read_optional(root / turn_ref / "state/call_summary.json")
        artifacts = {**(report or {}).get("artifact_refs", {}), **writing_artifacts(root, snapshots=True)}
        rows.append({
            "problem_id": item["problem_id"],
            "title": item["title"],
            "turn_id": item["turn_id"],
            "workspace_status": item["status"],
            "updated_at": item.get("updated_at"),
            **{key: report.get(key) if report is not None else None for key in (
                "run_status", "result", "answer_mode", "reason_code",
                "mathematical_status", "operational_failure_owner", "operational_reason_code",
                "execution_mode", "mode_source", "mode_reason", "proof_loops_used", "max_proof_loops",
            )},
            "cumulative_finding_count": len(findings["entries"]) if findings is not None else None,
            "semantic_call_count": calls.get("semantic_call_count") if calls is not None else None,
            "transport_attempt_count": calls.get("transport_attempt_count") if calls is not None else None,
            "final_report_ref": final_ref if report is not None else None,
            "navigation_ref": "README.md" if (root / "README.md").is_file() else None,
            "writing_action": active_writing_request(root).get("action"),
            "writing_complete": writing_complete(root),
            "artifact_refs": artifacts,
        })
    return rows


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "# Recorded results", "",
        "Current turn per problem; findings accumulate across turns. Missing records are shown as —.",
        "",
        "| Problem | Turn | Workspace | Run status | Result | Answer mode | Findings (cumulative) | Calls |",
        "|---|---|---|---|---|---|---:|---:|",
    ]
    keys = ("problem_id", "turn_id", "workspace_status", "run_status", "result", "answer_mode",
            "cumulative_finding_count", "semantic_call_count")
    for row in rows:
        cells = ["—" if row[key] is None or row[key] == "" else str(row[key]).replace("|", "\\|").replace("\n", " ") for key in keys]
        lines.append("| " + " | ".join(cells) + " |")
    if not rows:
        lines.extend(["", "No recorded problem workspaces."])
    lines.extend(["", "PASS must be read with answer_mode and the original verifier evidence."])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new directory for summary.json and README.md")
    args = parser.parse_args(argv)
    source, destination = args.runs_dir.expanduser().resolve(), args.output.expanduser().resolve()
    if not source.is_dir():
        parser.error("runs-dir does not exist")
    if source == destination or source in destination.parents or destination in source.parents:
        parser.error("source and destination must be separate, non-nested directories")
    if destination.exists():
        parser.error("output already exists; choose a new summary directory")
    rows = collect_results(source)
    destination.mkdir(parents=True)
    (destination / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "README.md").write_text(render_markdown(rows), encoding="utf-8")
    print(f"Summarized {len(rows)} problem(s): {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
