"""Transport-neutral tool identity, discovery, schema, and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from mindroom.tool_system.agno_toolkits import adapt_function

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.tool_system.agno_toolkits import Function


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


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Selected tool details loaded only when a caller asks for its schema."""

    key: ToolKey
    description: str
    input_schema: dict[str, object]
    instructions: tuple[str, ...]


def function_definition(function: Function) -> dict[str, object]:
    """Return the name, description, and input schema the model is shown for one Agno function."""
    if function.entrypoint is None:
        return {"name": function.name, "description": function.description or "", "parameters": function.parameters}
    adapted = adapt_function(function)
    return {"name": adapted.name, "description": adapted.description or "", "parameters": dict(adapted.parameters)}


def _no_remote_schema(uri: str) -> Never:
    raise NoSuchResource(ref=uri)


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, object]) -> None:
    """Validate arguments locally without resolving remote schema references."""
    try:
        Draft202012Validator(schema, registry=Registry(retrieve=_no_remote_schema)).validate(arguments)
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
