"""Apply exact, local text revisions while preserving complete new versions."""

from __future__ import annotations

import json
from pathlib import Path
from pace.runtime.output.workspace import _write_text_atomic, path_has_symlink_component


def render_revision(root: Path, patch_ref: str, *, base_ref: str) -> str:
    patch = json.loads((root / patch_ref).read_text(encoding="utf-8"))
    if not isinstance(patch, dict):
        raise ValueError("revision must be a JSON object")
    base = root / base_ref
    original = base.read_bytes().decode("utf-8")
    text = _render_edits(original, patch.get("edits"))
    if base.read_bytes().decode("utf-8") != original:
        raise ValueError("revision base changed during application")
    return text


def _render_edits(original: str, edits: list) -> str:
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
    if not text.strip():
        raise ValueError("revision cannot remove the complete document")
    return text


def apply_bundle_revision(root: Path, patch_ref: str, *, base_ref: str, output_ref: str,
                          format_plans: dict | None = None) -> None:
    """Patch existing TeX/bibliography files in an already copied source bundle."""
    source, destination = (root / base_ref).parent, (root / output_ref).parent
    if source.resolve() == destination.resolve():
        raise ValueError("bundle revision must use a new directory")
    payload = json.loads((root / patch_ref).read_text(encoding="utf-8"))
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError("bundle revision requires a nonempty files list")
    rendered = {}
    originals = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("bundle revision requires relative file paths")
        relative = Path(item["path"])
        if (relative.is_absolute() or ".." in relative.parts or "\\" in item["path"]
                or relative.suffix not in {".tex", ".bib", ".bbl"} or relative in rendered
                or path_has_symlink_component(root, source / relative)
                or path_has_symlink_component(root, destination / relative)):
            raise ValueError("bundle revision must select distinct local TeX or bibliography files")
        originals[relative] = (source / relative).read_bytes().decode("utf-8")
        rendered[relative] = _render_edits(originals[relative], item.get("edits"))
        if format_plans is not None:
            from pace.runtime.math_format import validate_format_revision
            if relative.as_posix() not in format_plans:
                raise ValueError("Format patch targets a file without a diagnosed defect")
            validate_format_revision(originals[relative], rendered[relative], format_plans[relative.as_posix()], root=root)
    # Validate the entire patch before writing any part. Reapplying after a
    # partial install always renders from the unchanged original bundle.
    for relative, original in originals.items():
        if (source / relative).read_bytes().decode("utf-8") != original:
            raise ValueError("bundle revision base changed during application")
    for relative, text in rendered.items():
        _write_text_atomic(destination / relative, text)


def apply_revision(root: Path, patch_ref: str, *, base_ref: str, output_ref: str,
                   format_plan: dict | None = None) -> None:
    if (root / base_ref).resolve() == (root / output_ref).resolve():
        raise ValueError("revision output must be a new copy")
    updated = render_revision(root, patch_ref, base_ref=base_ref)
    if format_plan is not None:
        from pace.runtime.math_format import validate_format_revision
        validate_format_revision((root / base_ref).read_bytes().decode("utf-8"), updated, format_plan, root=root)
    _write_text_atomic(root / output_ref, updated)


def revision_instruction(base_ref: str, patch_ref: str) -> str:
    return (
        f"Write the local revision to `{patch_ref}` as JSON with edits "
        '[{"old":"unique exact passage","new":"complete replacement passage"}]. '
        f"The Host binds the base to `{base_ref}`; do not repeat base_ref in the patch. "
        "Use a unique surrounding heading for additions. The Host applies these changes "
        "to a complete new copy. Leave unrelated passages unchanged. If a global rewrite "
        "is necessary, one edit may replace the whole base, explaining why in the response. "
    )
