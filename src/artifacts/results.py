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
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .workspace import (
    _write_json_atomic,
    _write_text_atomic,
    current_turn,
    is_rci_critic_material,
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


def committed_round_reading_refs(root: Path, result_ref: str) -> tuple[str, ...]:
    """Surface the accepted handoff, its Reporter summary and all local finding reviews."""
    folder = (root / result_ref).parent
    refs = [result_ref]
    if (folder / "reporter_result.json").is_file() and (
        folder / "progress.md"
    ).is_file():
        refs.append((folder / "progress.md").relative_to(root).as_posix())
    refs.extend(
        str(Path(ref).with_suffix(".verification.json"))
        for ref in committed_finding_refs(root, result_ref)
        if (root / Path(ref).with_suffix(".verification.json")).is_file()
    )
    return tuple(refs)


def committed_finding_refs(root: Path, result_ref: str) -> tuple[str, ...]:
    """Read the submitted original manuscripts, without creating proof copies."""
    folder = (root / result_ref).parent
    reporter = folder / "reporter_result.json"
    if reporter.is_file():
        return tuple(
            ref
            for ref in json.loads(reporter.read_text())["payload"].get(
                "finding_refs", ()
            )
            if not is_rci_critic_material(ref)
        )
    result = json.loads((root / result_ref).read_text())
    if "manuscript_refs" in result:
        return tuple(
            ref for ref in result["manuscript_refs"] if not is_rci_critic_material(ref)
        )
    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted((folder / "findings").glob("*.md"))
    )


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
    workdir: Path,
    *,
    source_ref: str,
    verification_ref: str,
    round_index: int,
    publication: dict,
) -> str:
    """Apply the verifier's CREATE/UPDATE/NONE decision and retain exact source versions."""

    root = Path(workdir)
    state = _read_state(root / FINDINGS_STATE_REF, {"entries": [], "publications": []})
    entries = state["entries"]
    history = state["publications"]
    row = next(
        (
            item
            for item in history
            if item["source_ref"] == source_ref
            and item["verification_ref"] == verification_ref
        ),
        None,
    )
    if row is None:
        action = publication["action"]
        if action not in {"CREATE", "UPDATE", "NONE"}:
            raise ValueError(
                "finding publication action must be CREATE, UPDATE or NONE"
            )
        previous = None
        finding_id, reference = "", ""
        if action == "UPDATE":
            reference = publication["finding_ref"]
            previous = next(
                (item for item in entries if item["ref"] == reference), None
            )
            if previous is None:
                raise ValueError("UPDATE must name a current finding")
            finding_id = previous["finding_id"]
        elif action == "CREATE":
            next_id = 1 + max(
                (int(item["finding_id"][1:]) for item in history if item["finding_id"]),
                default=0,
            )
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
    if (
        current is not None
        and current["source_ref"] == source_ref
        and current["verification_ref"] == verification_ref
    ):
        document = root / row["ref"]
        source = root / source_ref
        body = source.read_text(encoding="utf-8")
        body = relocate_links(body, source, document)
        verification_link = Path(
            os.path.relpath(root / verification_ref, document.parent)
        ).as_posix()
        lines = [
            f"> 发布状态：已通过独立核验，适用范围见[核验记录]({verification_link})。",
            "> 下文保留送审原稿，文中的“待核验”等状态说明记录的是送审时的状态。",
            "",
            body.rstrip(),
            "",
            "---",
            "",
            f"成果编号：{row['finding_id']}。",
        ]
        source_link = Path(
            os.path.relpath(root / source_ref, document.parent)
        ).as_posix()
        lines.append(f"- [核验原稿]({source_link})")
        _write_text_atomic(document, "\n".join(lines) + "\n")
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
    if any(
        kernel.get(key) != binding.get(key)
        for key in ("proof_id", "proof_ref", "target_contract_id")
    ):
        return {}
    return {
        "source_ref": ref,
        **{
            key: kernel[key]
            for key in (
                "proof_id",
                "proof_ref",
                "target_contract_id",
                "stage_status",
                "target_clause_checks",
                "canonical_blockers",
                "final_assembly",
            )
            if key in kernel
        },
    }


def write_dependency_manifest(
    *,
    workdir: Path,
    output_ref: str,
    target_contract_id: str,
    selected_dependencies: Iterable[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Index exact finding sources and receipts without inventing proof authority."""

    entries = []
    from pace.contracts.research.memory import enabled

    selected = (
        set(selected_dependencies or ())
        if enabled(workdir)
        else None
        if selected_dependencies is None
        else set(selected_dependencies)
    )
    for row in finding_entries(workdir):
        if selected is not None and not (
            {row["source_ref"], row["verification_ref"], row.get("ref", "")} & selected
        ):
            continue
        source = workdir / row["source_ref"]
        body = source.read_text(encoding="utf-8")
        title = next(
            (line[2:].strip() for line in body.splitlines() if line.startswith("# ")),
            row["finding_id"],
        )
        receipt = json.loads(
            (workdir / row["verification_ref"]).read_text(encoding="utf-8")
        )
        entries.append(
            {
                **row,
                "statement_and_assumptions_ref": row["source_ref"],
                "source_lines": [1, len(body.splitlines())],
                "source_summary_verbatim": title,
                **(
                    {"source_text": body, "verification_receipt": receipt}
                    if selected is None
                    else {
                        "source_sha256": __import__("hashlib")
                        .sha256(body.encode())
                        .hexdigest(),
                        "verification_ref": row["verification_ref"],
                        "verification_sha256": __import__("hashlib")
                        .sha256((workdir / row["verification_ref"]).read_bytes())
                        .hexdigest(),
                    }
                ),
                "current_target_contract_id": target_contract_id,
                "target_relationship": "To be established explicitly in this candidate; not inferred by the Host.",
            }
        )
    ref = str(Path(output_ref).with_name("dependency_manifest.json"))
    _write_json_atomic(
        workdir / ref, {"target_contract_id": target_contract_id, "entries": entries}
    )
    return ref, entries
