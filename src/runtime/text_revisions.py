"""Apply exact, local text revisions while preserving complete new versions."""

import json
import re
from pathlib import Path


def _heading_section(text: str, heading: str) -> tuple[int, int]:
    """Locate an exact ATX heading and its section outside fenced examples."""
    headings = []
    fence = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        value = line.rstrip("\r\n")
        delimiter = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", value)
        if fence:
            if delimiter and delimiter[1].startswith(fence) and not delimiter[2].strip():
                fence = ""
        elif delimiter:
            fence = delimiter[1]
        elif match := re.fullmatch(r" {0,3}(#{1,6})[ \t]+\S.*", value):
            headings.append((value, len(match[1]), offset))
        offset += len(line)
    matches = [i for i, row in enumerate(headings) if row[0] == heading]
    if len(matches) != 1:
        raise ValueError("revision heading is missing or ambiguous")
    index = matches[0]
    _, level, start = headings[index]
    end = next(
        (pos for _, size, pos in headings[index + 1 :] if size <= level), len(text)
    )
    return start, end


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
        if (
            not isinstance(item, dict)
            or "new" not in item
            or not {"old", "heading"}.intersection(item)
        ):
            raise ValueError("each revision edit must name one old passage or heading and new text")
        if "old" not in item:
            heading, new = item["heading"], item["new"]
            if (
                not isinstance(heading, str)
                or not isinstance(new, str)
                or not new.splitlines()
                or new.splitlines()[0] != heading
            ):
                raise ValueError("replacement must retain the exact section heading")
            start, end = _heading_section(text, heading)
            if _heading_section(new, heading) != (0, len(new)):
                raise ValueError("replacement must contain one section at the original heading level")
            text = text[:start] + new.rstrip("\r\n") + "\n\n" + text[end:]
            continue
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
