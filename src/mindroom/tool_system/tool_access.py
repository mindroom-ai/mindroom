"""Transport-neutral tool identity, discovery, schema, and validation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from mindroom.llm.provider import ToolSpec
    from mindroom.tool_system.tool_function import ToolFunction


class _InvalidToolArgumentsError(ValueError):
    """Expose one neutral failure without leaking validator or schema details."""

    def __init__(self) -> None:
        super().__init__("Invalid tool arguments")


class UnknownToolError(ValueError):
    """A toolkit or function the catalog lacks; the message names what it does contain."""


@dataclass(frozen=True, slots=True)
class ToolKey:
    """Canonical identity for one function in one toolkit namespace."""

    toolkit: str
    function: str


def tool_definition(tool: ToolSpec) -> dict[str, object]:
    """Return the name, description, and input schema the model is shown for one tool definition."""
    return {"name": tool.name, "description": tool.description or "", "parameters": dict(tool.parameters)}


def _no_remote_schema(uri: str) -> Never:
    raise NoSuchResource(ref=uri)


def _with_nullable_optional_properties(
    schema: Mapping[str, Any],
    arguments: object,
    nullable_optional_parameters: Collection[str],
) -> dict[str, Any]:
    """Allow explicit JSON ``null`` for optional schema properties whose derived type omitted ``null``."""
    resolved = dict(schema)
    if not nullable_optional_parameters or not isinstance(arguments, Mapping):
        return resolved
    properties = resolved.get("properties")
    if not isinstance(properties, Mapping):
        return resolved
    raw_required = resolved.get("required")
    required = {item for item in raw_required if isinstance(item, str)} if isinstance(raw_required, list) else set()
    to_widen = [
        name
        for name in nullable_optional_parameters
        if name in properties and name not in required and name in arguments and arguments[name] is None
    ]
    if not to_widen:
        return resolved
    widened_properties = dict(properties)
    for name in to_widen:
        prop = widened_properties[name]
        if isinstance(prop, Mapping) and prop:
            widened_properties[name] = {"anyOf": [dict(prop), {"type": "null"}]}
    resolved["properties"] = widened_properties
    return resolved


def validate_tool_arguments(
    schema: Mapping[str, Any] | ToolFunction,
    arguments: dict[str, object],
    *,
    nullable_optional_parameters: Collection[str] = (),
) -> None:
    """Validate arguments locally without resolving remote schema references.

    When given a ``ToolFunction`` or ``nullable_optional_parameters``, explicit JSON ``null`` is admitted for optional
    properties whose derived schema omitted ``null`` because their Python default is ``None``.
    """
    if isinstance(schema, Mapping):
        raw_schema = schema
        nullable = nullable_optional_parameters
    else:
        raw_schema = schema.parameters
        nullable = (
            (*schema.nullable_optional_parameters, *nullable_optional_parameters)
            if nullable_optional_parameters
            else schema.nullable_optional_parameters
        )
    validation_schema = _with_nullable_optional_properties(raw_schema, arguments, nullable)
    try:
        Draft202012Validator(validation_schema, registry=Registry(retrieve=_no_remote_schema)).validate(arguments)
    except Exception as exc:  # jsonschema and referencing expose separate exception families.
        raise _InvalidToolArgumentsError from exc


def search_tool_metadata[T: Mapping[str, object]](
    items: Sequence[T],
    query: str,
    limit: int,
) -> list[T]:
    """Return stable catalog-order matches containing every query word."""
    words = query.lower().split()
    return [
        item for item in items if all(word in " ".join(str(value) for value in item.values()).lower() for word in words)
    ][:limit]
