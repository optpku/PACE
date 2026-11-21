"""Shared execution shell for model-facing semantic roles."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pace.runtime.roles import (
    SemanticRoleSpec,
    SemanticValidator,
)
from pace.runtime.semantic import SemanticCallResult

SemanticCall = Callable[..., SemanticCallResult]


def run_semantic_role(
    llm: Any,
    prompt: str,
    *,
    workdir: Path,
    call_name: str,
    role: SemanticRoleSpec,
    input_refs: Iterable[str | Path],
    semantic_call_fn: SemanticCall,
    host_metadata: dict[str, Any] | None = None,
    validator: SemanticValidator | None = None,
) -> SemanticCallResult:
    """Run one role through the calling agent's patchable transport seam."""

    return semantic_call_fn(
        llm,
        prompt,
        workdir=workdir,
        call_name=call_name,
        spec=role.call_spec(validator=validator),
        input_refs=input_refs,
        host_metadata=host_metadata,
    )


__all__ = ["run_semantic_role"]
