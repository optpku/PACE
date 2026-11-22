"""Declarative bindings for ordinary model-facing semantic roles."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pace.runtime.semantic import SemanticCallResult, SemanticCallSpec

SemanticSchemaFactory = Callable[[], dict[str, Any]]
SemanticNormalizer = Callable[[dict[str, Any]], dict[str, Any]]
SemanticValidator = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass(frozen=True)
class SemanticRoleSpec:
    """Declare the stable semantic contract for one ordinary model role."""

    role: str
    schema: SemanticSchemaFactory
    host_schema_version: str
    authority: str
    fatal_scope: str = "RUN"
    validator: SemanticValidator | None = None
    on_accept: Callable[[dict[str, Any]], object] | None = None

    def call_spec(
        self,
        *,
        normalizer: SemanticNormalizer | None = None,
        validator: SemanticValidator | None = None,
    ) -> SemanticCallSpec:
        """Materialize a fresh call contract, optionally binding invocation context."""

        return SemanticCallSpec(
            role=self.role,
            semantic_schema=self.schema(),
            host_schema_version=self.host_schema_version,
            authority=self.authority,
            fatal_scope=self.fatal_scope,
            normalizer=normalizer,
            validator=self.validator if validator is None else validator,
            on_accept=self.on_accept,
        )


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


__all__ = ["SemanticRoleSpec", "run_semantic_role"]
