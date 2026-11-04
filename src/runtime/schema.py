"""Canonical JSON-schema primitives shared by runtime semantic calls."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SchemaPreflightResult:
    ok: bool
    diagnostics: tuple[dict[str, Any], ...] = ()


def closed_object(
    properties: dict[str, dict[str, Any]],
    *,
    required: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a model-owned object with a host-enforced field allowlist."""

    fields = dict(properties)
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields) if required is None else list(required),
        "additionalProperties": False,
    }


def array_of(
    item_schema: dict[str, Any],
    *,
    min_items: int | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": item_schema}
    if min_items is not None:
        schema["minItems"] = int(min_items)
    if max_items is not None:
        schema["maxItems"] = int(max_items)
    return schema


STRING = {"type": "string"}
STRING_LIST = array_of(STRING)
BOOLEAN = {"type": "boolean"}
INTEGER = {"type": "integer"}


def open_object(
    properties: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Schema for an extensible model-owned JSON object.

    ``properties`` are optional documentation/validation hints.  They are
    deliberately never required and unknown fields remain valid.  This is the
    representation to use for mathematical evidence and search context whose
    shape is owned by the model rather than by the Host.
    """

    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": [],
        "additionalProperties": True,
    }


def preflight_output_schema(schema: Any) -> SchemaPreflightResult:
    """Validate the local output contract before calling a model."""

    diagnostics: list[dict[str, Any]] = []
    try:
        json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        diagnostics.append(
            _schema_error(
                "/",
                type(exc).__name__,
                "stably JSON-serializable schema",
            )
        )
    if not isinstance(schema, dict):
        diagnostics.append(_schema_error("/", schema, "schema object"))
    else:
        _preflight_node(schema, pointer="", diagnostics=diagnostics)
    return SchemaPreflightResult(
        ok=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def validate_json_instance(
    value: Any,
    schema: dict[str, Any],
    *,
    pointer: str = "",
) -> list[dict[str, Any]]:
    """Validate the JSON-Schema subset used by model-owned contracts."""

    errors: list[dict[str, Any]] = []
    expected = schema.get("type")
    if expected and not _matches_type(value, str(expected)):
        return [_schema_error(pointer or "/", value, f"type={expected}")]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(_schema_error(pointer or "/", value, f"enum={schema['enum']}"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if (
            isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and value < minimum
        ):
            errors.append(_schema_error(pointer or "/", value, f"minimum={minimum}"))
        if (
            isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and value > maximum
        ):
            errors.append(_schema_error(pointer or "/", value, f"maximum={maximum}"))
    if isinstance(value, dict):
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(
                    _schema_error(f"{pointer}/{key}", None, "required")
                )
        for key, child in value.items():
            if key not in properties and schema.get("additionalProperties") is not True:
                errors.append(
                    _schema_error(
                        f"{pointer}/{key}",
                        child,
                        "declared semantic property",
                    )
                )
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                errors.extend(
                    validate_json_instance(
                        child,
                        child_schema,
                        pointer=f"{pointer}/{key}",
                    )
                )
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(_schema_error(pointer or "/", len(value), f"minItems={minimum}"))
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(_schema_error(pointer or "/", len(value), f"maxItems={maximum}"))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                errors.extend(
                    validate_json_instance(
                        child,
                        item_schema,
                        pointer=f"{pointer}/{index}",
                    )
                )
    return errors


def _preflight_node(
    node: dict[str, Any],
    *,
    pointer: str,
    diagnostics: list[dict[str, Any]],
) -> None:
    node_type = node.get("type")
    _preflight_common_keywords(node, pointer=pointer, diagnostics=diagnostics)
    if node_type == "object":
        properties = node.get("properties")
        required = node.get("required")
        if not isinstance(properties, dict):
            diagnostics.append(
                _schema_error(pointer or "/", properties, "explicit object properties")
            )
            return
        if not isinstance(node.get("additionalProperties"), bool):
            diagnostics.append(
                _schema_error(
                    f"{pointer}/additionalProperties",
                    node.get("additionalProperties"),
                    "explicit boolean for object field policy",
                )
            )
        if (
            not isinstance(required, list)
            or not all(isinstance(key, str) for key in required)
            or len(required) != len(set(required))
            or not set(required).issubset(properties)
        ):
            diagnostics.append(
                _schema_error(
                    f"{pointer}/required",
                    required,
                    f"a unique subset of property names: {list(properties)}",
                )
            )
        for key, child in properties.items():
            if not isinstance(child, dict):
                diagnostics.append(
                    _schema_error(
                        f"{pointer}/properties/{key}",
                        child,
                        "schema object",
                    )
                )
                continue
            _preflight_node(
                child,
                pointer=f"{pointer}/properties/{key}",
                diagnostics=diagnostics,
            )
        return
    if node_type == "array":
        items = node.get("items")
        if not isinstance(items, dict):
            diagnostics.append(
                _schema_error(
                    f"{pointer}/items",
                    items,
                    "array item schema",
                )
            )
            return
        _preflight_node(
            items,
            pointer=f"{pointer}/items",
            diagnostics=diagnostics,
        )
        return
    if node_type not in {"string", "integer", "number", "boolean", "null"}:
        diagnostics.append(
            _schema_error(pointer or "/", node_type, "supported explicit type")
        )


def _preflight_common_keywords(
    node: dict[str, Any],
    *,
    pointer: str,
    diagnostics: list[dict[str, Any]],
) -> None:
    node_type = node.get("type")
    if "enum" in node:
        enum = node.get("enum")
        if not isinstance(enum, list) or not enum:
            diagnostics.append(
                _schema_error(
                    f"{pointer}/enum",
                    enum,
                    "non-empty enum array",
                )
            )
        elif node_type in {"string", "integer", "number", "boolean", "null"}:
            invalid = [
                value for value in enum if not _matches_type(value, str(node_type))
            ]
            if invalid:
                diagnostics.append(
                    _schema_error(
                        f"{pointer}/enum",
                        invalid,
                        f"values of type {node_type}",
                    )
                )

    for key in ("minItems", "maxItems"):
        if key not in node:
            continue
        value = node.get(key)
        if (
            node_type != "array"
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            diagnostics.append(
                _schema_error(
                    f"{pointer}/{key}",
                    value,
                    "non-negative integer on array",
                )
            )
    minimum_items = node.get("minItems")
    maximum_items = node.get("maxItems")
    if (
        isinstance(minimum_items, int)
        and not isinstance(minimum_items, bool)
        and isinstance(maximum_items, int)
        and not isinstance(maximum_items, bool)
        and minimum_items > maximum_items
    ):
        diagnostics.append(
            _schema_error(
                f"{pointer}/minItems",
                minimum_items,
                f"not greater than maxItems={maximum_items}",
            )
        )

    for key in ("minimum", "maximum"):
        if key not in node:
            continue
        value = node.get(key)
        if (
            node_type not in {"integer", "number"}
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            diagnostics.append(
                _schema_error(
                    f"{pointer}/{key}",
                    value,
                    "numeric bound on integer or number",
                )
            )
    minimum = node.get("minimum")
    maximum = node.get("maximum")
    if (
        isinstance(minimum, (int, float))
        and not isinstance(minimum, bool)
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and minimum > maximum
    ):
        diagnostics.append(
            _schema_error(
                f"{pointer}/minimum",
                minimum,
                f"not greater than maximum={maximum}",
            )
        )


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _schema_error(pointer: str, observed: Any, expected: str) -> dict[str, Any]:
    return {
        "stage": "schema_preflight",
        "json_pointer": pointer or "/",
        "observed": observed,
        "expected": expected,
    }


__all__ = [
    "BOOLEAN",
    "INTEGER",
    "STRING",
    "STRING_LIST",
    "SchemaPreflightResult",
    "array_of",
    "closed_object",
    "preflight_output_schema",
    "validate_json_instance",
]
