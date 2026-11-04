"""Pause publication at an owned boundary without changing mathematical state."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .workspace import _write_json_atomic, work_ref


class DeliveryPaused(ValueError):
    """A committed task remains intact while its publication awaits resume."""


def delivery_pause(root: Path) -> dict:
    path = Path(root) / work_ref(root, "state/delivery_pause.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def pause_delivery(root: Path, *, trigger: str, round_index: int, stage: str) -> None:
    record = {"trigger": trigger, "round_index": round_index, "stage": stage}
    ref = work_ref(root, f"state/delivery_attempts/{uuid.uuid4().hex}.json")
    _write_json_atomic(Path(root) / ref, record)
    _write_json_atomic(Path(root) / work_ref(root, "state/delivery_pause.json"),
                       {**record, "diagnostic_ref": ref})


def clear_delivery_pause(root: Path) -> None:
    # The immutable attempt records remain available after a successful retry.
    (Path(root) / work_ref(root, "state/delivery_pause.json")).unlink(missing_ok=True)
