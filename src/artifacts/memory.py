"""Persist agent-authored memory notes with their exact evidence references."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pace.artifacts.results import relocate_links
from pace.artifacts.workspace import RunWorkspace

logger = logging.getLogger(__name__)


def note_ref(result_ref: str, *, verification: bool = False) -> str:
    path = Path(result_ref)
    suffix = "-verification" if verification else ""
    return f"memory/notes/{path.parts[1]}-{path.parent.name}{suffix}.md"


def publish_note(
    workspace: RunWorkspace,
    *,
    source_ref: str,
    destination_ref: str,
    evidence_refs: tuple[str, ...],
) -> str:
    source = workspace.path(source_ref)
    if not source.is_file():
        return ""
    destination = workspace.path(destination_ref)
    if destination.is_file():
        return destination_ref
    try:
        body = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning(
            "Cannot read optional memory draft: %s", source_ref, exc_info=True
        )
        return ""
    if not body.strip():
        return ""
    body = relocate_links(body, source, destination)
    lines = [
        body.rstrip(),
        "",
        "---",
        "",
        "用途：SEARCH_CONTEXT_ONLY。范围及依据由以下原始文件保留：",
    ]
    for ref in dict.fromkeys(ref for ref in (source_ref, *evidence_refs) if ref):
        link = Path(os.path.relpath(workspace.path(ref), destination.parent)).as_posix()
        lines.append(f"- [{ref}]({link})")
    try:
        workspace.write_text_atomic(destination_ref, "\n".join(lines) + "\n")
    except OSError:
        logger.warning(
            "Cannot publish optional memory draft: %s", source_ref, exc_info=True
        )
        return ""
    return destination_ref
