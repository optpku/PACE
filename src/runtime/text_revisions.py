"""Apply exact, local text revisions while preserving complete new versions."""

import json
from pathlib import Path

from pace.artifacts.workspace import _write_text_atomic


def render_revision(root: Path, patch_ref: str, *, base_ref: str) -> str:
    patch = json.loads((root / patch_ref).read_text(encoding="utf-8"))
    if not isinstance(patch, dict):
        raise ValueError("revision must be a JSON object")
    base = root / base_ref
    original = base.read_text(encoding="utf-8")
    edits = patch.get("edits")
    if not isinstance(edits, list):
        raise ValueError("revision edits must be a list")
    text = original
    for item in edits:
        if not isinstance(item, dict) or not {"old", "new"} <= item.keys():
            raise ValueError("each revision edit must contain old and new text")
        old, new = item["old"], item["new"]
        if (
            not isinstance(old, str)
            or not old
            or text.count(old) != 1
            or not isinstance(new, str)
        ):
            raise ValueError("revision anchor is missing or ambiguous")
        text = text.replace(old, new, 1)
    if base.read_text(encoding="utf-8") != original:
        raise ValueError("revision base changed during application")
    if not text.strip():
        raise ValueError("revision cannot remove the complete document")
    return text


def apply_revision(
    root: Path, patch_ref: str, *, base_ref: str, output_ref: str
) -> None:
    if (root / base_ref).resolve() == (root / output_ref).resolve():
        raise ValueError("revision output must be a new copy")
    _write_text_atomic(
        root / output_ref, render_revision(root, patch_ref, base_ref=base_ref)
    )
