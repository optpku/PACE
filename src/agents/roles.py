"""Shared execution shell for model-facing semantic roles."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pace.runtime.roles import (
    SemanticNormalizer,
    SemanticRoleSpec,
    SemanticValidator,
)
from pace.runtime.semantic import SemanticCallResult

SemanticCall = Callable[..., SemanticCallResult]
_USE_ROLE_DEFAULT = object()


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
    normalizer: SemanticNormalizer | None | object = _USE_ROLE_DEFAULT,
    validator: SemanticValidator | None | object = _USE_ROLE_DEFAULT,
) -> SemanticCallResult:
    """Run one role through the calling agent's patchable transport seam."""

    if normalizer is _USE_ROLE_DEFAULT and validator is _USE_ROLE_DEFAULT:
        call_spec = role.call_spec()
    elif normalizer is _USE_ROLE_DEFAULT:
        call_spec = role.call_spec(validator=validator)
    elif validator is _USE_ROLE_DEFAULT:
        call_spec = role.call_spec(normalizer=normalizer)
    else:
        call_spec = role.call_spec(
            normalizer=normalizer,
            validator=validator,
        )
    return semantic_call_fn(
        llm,
        prompt,
        workdir=workdir,
        call_name=call_name,
        spec=call_spec,
        input_refs=input_refs,
        host_metadata=host_metadata,
    )


__all__ = ["run_semantic_role"]
