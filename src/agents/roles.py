"""Declarative bindings for ordinary model-facing semantic roles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from pace.runtime.semantic import SemanticCallSpec

SemanticSchemaFactory = Callable[[], dict[str, Any]]
SemanticNormalizer = Callable[[dict[str, Any]], dict[str, Any]]
SemanticValidator = Callable[[dict[str, Any]], list[dict[str, Any]]]

_USE_ROLE_DEFAULT: Final = object()


@dataclass(frozen=True)
class SemanticRoleSpec:
    """Declare the stable semantic contract for one ordinary model role."""

    role: str
    schema: SemanticSchemaFactory
    host_schema_version: str
    authority: str
    fatal_scope: str = "RUN"
    normalizer: SemanticNormalizer | None = None
    validator: SemanticValidator | None = None
    on_accept: Callable[[dict[str, Any]], None] | None = None

    def call_spec(
        self,
        *,
        normalizer: SemanticNormalizer | None | object = _USE_ROLE_DEFAULT,
        validator: SemanticValidator | None | object = _USE_ROLE_DEFAULT,
    ) -> SemanticCallSpec:
        """Materialize a fresh call contract, optionally binding invocation context."""

        bound_normalizer = (
            self.normalizer
            if normalizer is _USE_ROLE_DEFAULT
            else cast(SemanticNormalizer | None, normalizer)
        )
        bound_validator = (
            self.validator
            if validator is _USE_ROLE_DEFAULT
            else cast(SemanticValidator | None, validator)
        )
        return SemanticCallSpec(
            role=self.role,
            semantic_schema=self.schema(),
            host_schema_version=self.host_schema_version,
            authority=self.authority,
            fatal_scope=self.fatal_scope,
            normalizer=bound_normalizer,
            validator=bound_validator,
            on_accept=self.on_accept,
        )


__all__ = ["SemanticRoleSpec"]
