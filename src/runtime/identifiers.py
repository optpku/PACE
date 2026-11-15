from __future__ import annotations

import re
import uuid
from typing import Any


def new_id(prefix: str) -> str:
    """Create a run-local opaque identifier without deriving it from content."""

    label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(prefix or "ID")).strip("-") or "ID"
    return f"{label}-{uuid.uuid4().hex}"


def text_signature(value: Any) -> str:
    """Keep normalized text directly when equality, not identity, is required."""

    return " ".join(str(value or "").split())
