"""Publish cumulative findings and the current user report.

Callers own verification decisions. This module persists their explicit files
and provenance; it never decides whether a mathematical statement is true.
"""

from __future__ import annotations

from pace.runtime.output.workspace import stage_ref

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
FINDINGS_CATALOG_REF = "results/findings/catalog.md"
FINDINGS_ARCHIVE_REF = "results/findings/archive.md"


def _read_state(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def finding_entries(workdir: Path) -> list[dict]:
    root = Path(workdir)
    state = _read_state(root / FINDINGS_STATE_REF, {"entries": []})
    return [row for row in state["entries"] if (root / row["ref"]).is_file()]


def _findings_state(root: Path) -> dict:
    state = _read_state(root / FINDINGS_STATE_REF, {"entries": [], "publications": []})
    # Upgrade the index only; never renumber or reverify old manuscripts.
    if "publications" not in state:
        state["publications"] = [{**row, "action": "CREATE"} for row in state["entries"]]
    for row in [*state["entries"], *state["publications"]]:
        if "title" not in row:
            source = root / row["source_ref"]
            with source.open(encoding="utf-8") as stream:
                row["title"] = next((line[2:].strip() for line in stream if line.startswith("# ")), source.name)
    state["schema_version"] = "findings/v2"
    return state


def finding_publications(root: Path) -> list[dict]:
    return _findings_state(Path(root))["publications"]


def validate_publication(root: Path, decision: object) -> list[dict]:
    error = ""
    if (not isinstance(decision, dict) or not isinstance(decision.get("action"), str)
            or decision["action"] not in {"CREATE", "UPDATE", "NONE"}):
        error = "Choose CREATE, UPDATE or NONE in publication.action."
    elif decision["action"] == "UPDATE":
        current = {row["ref"]: row for row in finding_entries(root)}
        ref = decision.get("finding_ref")
        if not isinstance(ref, str) or ref not in current:
            error = "UPDATE must name an existing current finding_ref from the supplied catalog."
        elif decision.get("expected_source_ref", current[ref]["source_ref"]) != current[ref]["source_ref"]:
            error = "UPDATE refers to a replaced finding version; compare the current version before publishing."
    return ([{"stage": "finding_publication", "json_pointer": "/publication", "message": error}]
            if error else [])


def stage_finding_review_refs(
    workdir: Path, round_index: int, completed_result_refs: Iterable[str] = (),
) -> tuple[str, ...]:
    """Navigate all latest-round verdicts, including unpublished rejections."""
    root = Path(workdir)
    prefix = Path(work_ref(root)).parts
    results = [Path(ref) for ref in completed_result_refs
               if Path(ref).name == "agent_result.json"
               and Path(ref).parts[:len(prefix)] == prefix]
    directories = {root / f"{stage_ref(round_index, workdir=root)}/findings"}
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
    title = row.get("title", "")
    label = row["finding_id"] + (f" · {title}" if title else "")
    link = Path(os.path.relpath(root / row["ref"], document.parent)).as_posix()
    return f"- [{label}]({link})"


def refresh_findings_indexes(workdir: Path) -> None:
    """Refresh current entries and immutable sources without replacing editorial navigation."""
    root = Path(workdir)
    state = _findings_state(root)
    document = root / FINDINGS_ARCHIVE_REF
    lines = [
        "# 核验原稿档案", "",
        "按用户轮次保留每次核验的固定原稿及核验记录。成果编号指向持续完善的结果；历史原稿不会随当前成果更新。", "",
    ]
    turns: dict[str, list[dict]] = {}
    for row in state["publications"]:
        turns.setdefault(row["turn_id"], []).append(row)
    if not turns:
        lines.append("尚无已保存的核验原稿。")
    for turn_id, rows in turns.items():
        lines.extend([f"## {turn_id}", ""])
        for row in rows:
            source = root / row["source_ref"]
            title = row["title"]
            label = f"{row['finding_id']} · " if row["finding_id"] else "支撑材料或报告 · "
            original = Path(os.path.relpath(source, document.parent)).as_posix()
            verification = Path(os.path.relpath(root / row["verification_ref"], document.parent)).as_posix()
            lines.append(f"- [{label}{title}]({original}) · [核验记录]({verification})")
        lines.append("")
    _write_text_atomic(document, "\n".join(lines).rstrip() + "\n")
    catalog = root / FINDINGS_CATALOG_REF
    rows = [_finding_link(root, row, catalog) for row in state["entries"] if (root / row["ref"]).is_file()]
    _write_text_atomic(catalog, "# 当前成果条目\n\n" + "\n".join(rows or ["尚无独立成果。"]) + "\n")
    index = root / FINDINGS_INDEX_REF
    if not index.is_file():
        _write_text_atomic(index, "# 研究成果\n\n每个编号表示一项持续完善的数学结果。"
                           "主题说明与适用范围可在此整理，发布新稿不会覆盖本页。\n\n"
                           "- [当前成果条目](catalog.md)\n- [核验原稿档案](archive.md)\n")


def relocate_links(
    body: str,
    source: Path,
    destination: Path,
    *,
    resolve: Callable[[Path], tuple[Path, str] | None] | None = None,
) -> str:
    """Rebase ordinary Markdown file links without editing code or equations."""

    def link(match: re.Match) -> str:
        if match["code"]:
            return match[0]
        prefix = match["inline_prefix"] or match["reference_prefix"]
        raw = match["inline_target"] or match["reference_target"]
        suffix = match["inline_suffix"] or match["reference_suffix"]
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
                return match["label"] or ""
            path, anchor = public
        relative = Path(os.path.relpath(path, destination.parent)).as_posix()
        target = urlunsplit(("", "", relative, parsed.query, parsed.fragment or anchor))
        if raw.startswith("<") or " " in target:
            target = f"<{target}>"
        return prefix + target + suffix

    # Reports use ordinary inline links and reference links. Code examples are
    # literal text. Match whole links before inline code inside their labels.
    token = re.compile(
        r"(?P<code>`[^`\n]+`)"
        r"|(?P<inline_prefix>!?\[(?P<label>[^\]\n]*)\]\([ \t]*)"
        r"(?P<inline_target><[^>\n]+>|[^\s)]+)(?P<inline_suffix>[^\n)]*\))"
        r"|(?P<reference_prefix>^\s*\[[^\]\n]+\]:\s*)"
        r"(?P<reference_target><[^>\n]+>|\S+)(?P<reference_suffix>[^\n]*)",
        re.MULTILINE,
    )
    parts = re.split(r"(```[^\n]*\n[\s\S]*?```|~~~[^\n]*\n[\s\S]*?~~~)", body)
    for index in range(0, len(parts), 2):
        parts[index] = token.sub(link, parts[index])
    return "".join(parts)


def publish_finding(
    workdir: Path, *, source_ref: str, verification_ref: str, round_index: int,
    publication: dict,
) -> str:
    """Apply the verifier's CREATE/UPDATE/NONE decision and retain exact source versions."""

    root = Path(workdir)
    state = _findings_state(root)
    entries = state["entries"]
    history = state["publications"]
    row = next((item for item in history if item["source_ref"] == source_ref
                and item["verification_ref"] == verification_ref), None)
    if row is None:
        diagnostics = validate_publication(root, publication)
        if diagnostics:
            raise ValueError(diagnostics[0]["message"])
        action = publication["action"]
        previous = None
        finding_id, reference = "", ""
        if action == "UPDATE":
            reference = publication["finding_ref"]
            previous = next((item for item in entries if item["ref"] == reference), None)
            if previous is None:
                raise ValueError("UPDATE must name a current finding")
            finding_id = previous["finding_id"]
        elif action == "CREATE":
            next_id = 1 + max((int(item["finding_id"][1:]) for item in history
                               if item["finding_id"]), default=0)
            finding_id = f"R{next_id:04d}"
            reference = f"results/findings/{finding_id}.md"
        if not (root / source_ref).read_text(encoding="utf-8").strip():
            raise ValueError("checked manuscript is empty")
        if not (root / verification_ref).is_file():
            raise ValueError("finding publication requires its verification record")
        row = {
            "finding_id": finding_id,
            "ref": reference,
            "source_ref": source_ref,
            "verification_ref": verification_ref,
            "round_index": int(round_index),
            "turn_id": current_turn(root),
            "action": action,
            "title": next((line[2:].strip() for line in (root / source_ref).read_text(encoding="utf-8").splitlines()
                           if line.startswith("# ")), Path(source_ref).name),
        }
        history.append(row)
        if previous is not None:
            entries[entries.index(previous)] = dict(row)
        elif action == "CREATE":
            entries.append(dict(row))
        # Commit the identity and source first so replay completes this same publication.
        _write_json_atomic(root / FINDINGS_STATE_REF, state)
    current = next((item for item in entries if item["ref"] == row["ref"]), None)
    # Replaying an earlier stage must never roll back a later checked update.
    if current is not None and current["source_ref"] == source_ref and current["verification_ref"] == verification_ref:
        document = root / row["ref"]
        source = root / source_ref
        body = source.read_text(encoding="utf-8")
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
    refresh_findings_indexes(root)
    from .navigation import refresh_navigation
    refresh_navigation(root)
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
