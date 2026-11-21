"""Small, explicit coercions used at JSON artifact boundaries.

A caller must choose whether scalar values are accepted and whether strings
are trimmed or deduplicated; those differences are part of persisted behavior.
"""

from __future__ import annotations

from typing import Any
import json


def load_model_json(text: str) -> tuple[Any, tuple[int, ...]]:
    """Decode JSON, preserving unsupported string escapes as literal slashes.

    Valid JSON is unchanged. Only an Invalid-escape failure permits this
    encoding correction; structure, keys, values and routing actions are never
    inferred. The caller still validates the complete decoded schema.
    """
    try:
        return json.loads(text), ()
    except json.JSONDecodeError as original:
        if original.msg != "Invalid \\escape":
            raise
        output, positions = [], []
        in_string, index = False, 0
        while index < len(text):
            char = text[index]
            if char == '"':
                in_string = not in_string
            if in_string and char == "\\" and index + 1 < len(text):
                following = text[index + 1]
                if following in '"\\/bfnrtu':
                    output.append(text[index:index + 2])
                    index += 2
                    continue
                output.append("\\")
                positions.append(index)
            output.append(char)
            index += 1
        if not positions:
            raise original
        try:
            return json.loads("".join(output)), tuple(positions)
        except json.JSONDecodeError:
            raise original from None


def decode_structured_fields(value: Any, schema: dict) -> tuple[Any, list[str]]:
    """Decode JSON text only where the contract explicitly expects an array or object."""
    from pace.runtime.schema import validate_json_instance

    changes = []

    def visit(node, contract, pointer):
        expected = contract.get('type')
        if isinstance(node, str) and expected in {'array', 'object'}:
            try:
                decoded = json.loads(node)
            except ValueError:
                return node
            if validate_json_instance(decoded, contract):
                return node
            node = decoded
            changes.append(pointer or '/')
        if isinstance(node, dict):
            properties = contract.get('properties', {})
            return {key: visit(child, properties[key], pointer + '/' + key)
                    if key in properties else child for key, child in node.items()}
        if isinstance(node, list) and isinstance(contract.get('items'), dict):
            return [visit(child, contract['items'], pointer + f'/{index}') for index, child in enumerate(node)]
        return node

    return visit(value, schema, ''), changes


def as_dict(value: Any) -> dict[str, Any]:
    """Return an existing JSON object, or an empty object for every other type."""

    return value if isinstance(value, dict) else {}


def dict_rows(value: Any) -> list[dict[str, Any]]:
    """Keep object rows from a JSON array; reject scalar objects."""

    return [
        row
        for row in (value if isinstance(value, list) else [])
        if isinstance(row, dict)
    ]


def one_or_many_dict_rows(value: Any) -> list[dict[str, Any]]:
    """Keep object rows while accepting one scalar object as a one-row list."""

    if value is None:
        return []
    return [
        row
        for row in (value if isinstance(value, list) else [value])
        if isinstance(row, dict)
    ]
