"""Publish cumulative findings and the current user report.

Callers own verification decisions. This module persists their explicit files
and provenance; it never decides whether a mathematical statement is true.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


from .workspace import (
    _write_json_atomic,
    _write_text_atomic,
    current_turn,
    work_ref,
)

FINDINGS_STATE_REF = "work/state/findings.json"
FINDINGS_INDEX_REF = "results/findings/README.md"
FINDINGS_ARCHIVE_REF = "results/findings/archive.md"


def _read_state(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def finding_entries(workdir: Path) -> list[dict]:
    root = Path(workdir)
    state = _read_state(root / FINDINGS_STATE_REF, {"entries": []})
    return [row for row in state["entries"] if (root / row["ref"]).is_file()]


def stage_finding_review_refs(
    workdir: Path, round_index: int, completed_result_refs: Iterable[str] = (),
) -> tuple[str, ...]:
    """Navigate all latest-round verdicts, including unpublished rejections."""
    root = Path(workdir)
    prefix = Path(work_ref(root, "stages")).parts
    results = [Path(ref) for ref in completed_result_refs
               if Path(ref).name == "agent_result.json"
               and Path(ref).parts[:len(prefix)] == prefix]
    directories = {root / work_ref(root, f"stages/{round_index:04d}/findings")}
    if results:
        latest = results[-1].parts[:len(prefix) + 1]
        # Both committed workers of one batch share the outer round directory.
        directories = {(root / ref).parent / "findings" for ref in results
                       if ref.parts[:len(prefix) + 1] == latest}
    return tuple(sorted({path.relative_to(root).as_posix()
                         for directory in directories
                         for path in directory.glob("*.verification.json")
                         if path.is_file() and not path.is_symlink()}))


def _finding_link(root: Path, row: dict, document: Path) -> str:
    with (root / row["ref"]).open(encoding="utf-8") as manuscript:
        title = next((line[2:].strip() for line in manuscript if line.startswith("# ")), "")
    label = row["finding_id"] + (f" · {title}" if title else "")
    link = Path(os.path.relpath(root / row["ref"], document.parent)).as_posix()
    return f"- [{label}]({link})"


def _refresh_findings_archive(root: Path) -> None:
    document = root / FINDINGS_ARCHIVE_REF
    lines = [
        "# 核验原稿档案", "",
        "按首次发布的用户轮次列出核验原稿；编号标识原稿，不代表独立研究贡献的数量。", "",
    ]
    turns: dict[str, list[dict]] = {}
    for row in finding_entries(root):
        turns.setdefault(row["turn_id"], []).append(row)
    if not turns:
        lines.append("尚无已发布的核验原稿。")
    for turn_id, rows in turns.items():
        lines.extend([f"## {turn_id}", ""])
        lines.extend(_finding_link(root, row, document) for row in rows)
        lines.append("")
    _write_text_atomic(document, "\n".join(lines).rstrip() + "\n")


def relocate_links(
    body: str,
    source: Path,
    destination: Path,
    *,
    resolve: Callable[[Path], tuple[Path, str] | None] | None = None,
) -> str:
    """Rebase ordinary Markdown file links without editing code or equations."""

    def link(match: re.Match) -> str:
        raw = match[2]
        target = raw[1:-1] if raw.startswith("<") else raw
        parsed = urlsplit(target)
        if (
            parsed.scheme
            or parsed.netloc
            or not parsed.path
            or (parsed.path.startswith("/") and resolve is None)
        ):
            return match[0]
        path = source.parent / unquote(parsed.path)
        anchor = ""
        if resolve is not None:
            public = resolve(path.resolve())
            if public is None:
                return match[1].lstrip("!")[1:-2] if match[1].endswith("](") else ""
            path, anchor = public
        relative = Path(os.path.relpath(path, destination.parent)).as_posix()
        target = urlunsplit(("", "", relative, parsed.query, parsed.fragment or anchor))
        if raw.startswith("<") or " " in target:
            target = f"<{target}>"
        return match[1] + target + match[3]

    # Reports use ordinary inline links and reference links. Code examples are
    # literal text, including examples that happen to contain Markdown links.
    parts = re.split(r"(```[^\n]*\n[\s\S]*?```|~~~[^\n]*\n[\s\S]*?~~~|`[^`\n]+`)", body)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(
            r"(!?\[[^\]\n]*\]\()(<[^>\n]+>|[^\s)]+)([^\n)]*\))", link, parts[index]
        )
        parts[index] = re.sub(
            r"(^\s*\[[^\]\n]+\]:\s*)(<[^>\n]+>|\S+)([^\n]*)",
            link,
            parts[index],
            flags=re.MULTILINE,
        )
    return "".join(parts)


def publish_finding(
    workdir: Path, *, source_ref: str, verification_ref: str, round_index: int
) -> str:
    """Publish the caller-approved manuscript once, retaining its checked text."""

    root = Path(workdir)
    state = _read_state(root / FINDINGS_STATE_REF, {"entries": []})
    entries = state["entries"]
    row = next((item for item in entries if item["source_ref"] == source_ref), None)
    if row is None:
        finding_id = f"R{len(entries) + 1:04d}"
        row = {
            "finding_id": finding_id,
            "ref": f"results/findings/{finding_id}.md",
            "source_ref": source_ref,
            "verification_ref": verification_ref,
            "round_index": int(round_index),
            "turn_id": current_turn(root),
        }
        entries.append(row)
        # Reserve the explicit identity first. An interrupted publication uses
        # this same identity when the parent resumes.
        _write_json_atomic(root / FINDINGS_STATE_REF, state)
    document = root / row["ref"]
    if not document.is_file():
        source = root / source_ref
        body = source.read_text(encoding="utf-8")
        if not body.strip():
            raise ValueError("approved finding manuscript is empty")
        body = relocate_links(body, source, document)
        verification_link = Path(os.path.relpath(root / verification_ref, document.parent)).as_posix()
        lines = [
            f"> 发布状态：已通过独立核验，适用范围见[核验记录]({verification_link})。",
            "> 下文保留送审原稿，文中的“待核验”等状态说明记录的是送审时的状态。",
            "", body.rstrip(), "", "---", "", f"成果编号：{row['finding_id']}。",
        ]
        source_link = Path(os.path.relpath(root / source_ref, document.parent)).as_posix()
        lines.append(f"- [核验原稿]({source_link})")
        _write_text_atomic(document, "\n".join(lines) + "\n")
    _refresh_findings_archive(root)
    _write_text_atomic(root / FINDINGS_INDEX_REF, "# Verified findings\n\n" + "\n".join(_finding_link(root, entry, root / FINDINGS_INDEX_REF) for entry in finding_entries(root)) + "\n")
    return row["ref"]









def verification_components(root: Path, report: dict) -> dict:
    """Expose exact Kernel component records only for the bound current proof."""
    binding = report.get("proof_binding") or {}
    ref = report.get("artifact_refs", {}).get("kernel", "")
    if not ref or not binding:
        return {}
    path = (Path(root) / ref).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        return {}
    kernel = _read_state(path, {})
    if any(kernel.get(key) != binding.get(key) for key in
           ("proof_id", "proof_ref", "target_contract_id")):
        return {}
    return {
        "source_ref": ref,
        **{key: kernel[key] for key in ("proof_id", "proof_ref", "target_contract_id",
                                      "stage_status", "target_clause_checks", "canonical_blockers",
                                      "final_assembly") if key in kernel},
    }
