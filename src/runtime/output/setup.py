"""Explicit checkpoints for initialization, before either engine owns a turn."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .workspace import (
    current_turn,
    path_has_symlink_component,
    work_ref,
    _write_json_atomic,
)

SETUP_PHASES = {"RESOURCES", "HANDOFF", "ORIENTATION", "CONTEXT", "READY"}


def setup_ref(root: Path) -> str:
    return work_ref(root, "state/setup_state.json")


def load_setup(root: Path) -> dict[str, Any] | None:
    path = Path(root) / setup_ref(root)
    if path_has_symlink_component(Path(root), path):
        raise ValueError("setup checkpoint must not contain a symlink")
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != "setup_state/v2"
        or state.get("turn_id") != current_turn(root)
        or state.get("phase") not in SETUP_PHASES
        or not isinstance(state.get("policy"), dict)
        or not isinstance(state.get("problem_text"), str)
    ):
        raise ValueError("invalid initialization checkpoint")
    literal = (Path(root) / work_ref(root, "input/problem.md")).read_text(
        encoding="utf-8"
    )
    if literal != state["problem_text"]:
        raise ValueError(
            "problem text changed after initialization was committed; start a new turn"
        )
    policy = state["policy"]
    cap = policy.get("max_proof_states")
    if (
        policy.get("resource_variant") not in {"standard", "weak"}
        or (cap is not None and (type(cap) is not int or cap < 1))
        or (policy.get("mode") not in {"proof", "research"} and not (
            policy.get("mode") is None
            and state["phase"] in {"RESOURCES", "HANDOFF", "ORIENTATION"}
        ))
        or policy.get("requested_mode") not in {None, "proof", "research"}
        or not isinstance(policy.get("control_context"), str)
    ):
        raise ValueError("invalid saved initialization policy")
    return state


def save_setup(root: Path, state: dict[str, Any]) -> None:
    _write_json_atomic(Path(root) / setup_ref(root), state)
