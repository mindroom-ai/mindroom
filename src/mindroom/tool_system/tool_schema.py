"""Derive a tool function's model-facing description and parameter schema from its signature and docstring.

``tool_description`` and ``tool_parameters`` give the schema Agno 3.0.9's entrypoint processing derives for the
annotations MindRoom's tools and its plugins use, byte for byte (``tests/tool_system/test_tool_schema.py`` runs every
MindRoom-authored function through both), and ``validated_entrypoint`` coerces a call's arguments to the annotations
the same way. The module imports no Agno code.
Every parameter belongs to the model except ``*args`` and ``**kwargs``, which the schema leaves out; refusing
parameters a framework would fill is the adapter's job.
"""

from __future__ import annotations

import functools
import inspect
from enum import Enum
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Any, Literal, Union, cast, get_args, get_origin, get_type_hints

from docstring_parser import parse
from pydantic import BaseModel, ConfigDict, validate_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

_JSON_TYPES = {
    "int": "integer",
    "float": "number",
    "complex": "number",
    "Decimal": "number",
    "str": "string",
    "bool": "boolean",
    "NoneType": "null",
    "list": "array",
    "tuple": "array",
    "set": "array",
    "frozenset": "array",
}
_UNNAMED_PARAMETERS = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
# Built once at import, which leaves pydantic no caller's namespace to keep: called inside a function, validate_call
# keeps that frame's locals to resolve forward references, and from Python 3.13 those locals are a proxy that keeps the
# frame and every frame below it alive for as long as the wrapper, and so the tool, lives. A tool's annotations resolve
# in its own module either way. Types pydantic has no schema for, such as a client object, are checked with isinstance.
_validate_call = validate_call(config=ConfigDict(arbitrary_types_allowed=True))


def tool_description(entrypoint: Callable[..., Any]) -> str:
    """Return the description the model reads: the docstring's summary and body, without its sections."""
    docstring = inspect.getdoc(entrypoint)
    if not docstring:
        return ""
    parsed = parse(docstring)
    return "\n".join(part for part in (parsed.short_description, parsed.long_description) if part)


def tool_parameters(entrypoint: Callable[..., Any]) -> dict[str, Any]:
    """Return the JSON schema of ``entrypoint``'s parameters, each described by its docstring entry.

    A parameter without a default is required. An annotation that does not resolve raises.
    """
    signature = inspect.signature(entrypoint)
    hints = get_type_hints(entrypoint)
    descriptions = _parameter_descriptions(entrypoint)
    properties: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in _UNNAMED_PARAMETERS:
            continue
        schema = _parameter_schema(hints.get(name))
        if description := descriptions.get(name):
            schema["description"] = description
        properties[name] = schema
    required = [
        name
        for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty and name in properties
    ]
    return {"type": "object", "properties": properties, "required": required}


def closed_parameters(entrypoint: Callable[..., Any], parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``parameters``, the schema a toolkit authored for ``entrypoint`` in place of the derived one, completed.

    As Agno completes an authored schema whose arguments it still validates, it requires each of its properties the
    signature gives no default, and admits no other property.
    """
    # TODO(simplify): the toolkits that author a schema could state its required properties and closure themselves.
    properties = parameters.get("properties") or {}
    required = [
        name
        for name, parameter in inspect.signature(entrypoint).parameters.items()
        if parameter.default is inspect.Parameter.empty and name in properties
    ]
    return {**parameters, "required": required, "additionalProperties": False}


def validated_entrypoint[F: Callable[..., Any]](entrypoint: F) -> F:
    """Return ``entrypoint`` validating each call's arguments against its annotations, coercing what pydantic can.

    An argument that does not validate raises pydantic's ``ValidationError``, whose text the model reads. A streaming
    tool stays an async generator function, so callers still recognize it as one.
    """
    validated = _validate_call(entrypoint)
    if not inspect.isasyncgenfunction(entrypoint):
        return validated

    @functools.wraps(entrypoint)
    async def stream(*args: object, **kwargs: object) -> AsyncIterator[object]:
        inner = validated(*args, **kwargs)
        try:
            async for item in inner:
                yield item
        finally:
            await inner.aclose()

    return cast("F", stream)


def _parameter_descriptions(entrypoint: Callable[..., Any]) -> dict[str, str]:
    """Return each documented parameter's description, prefixed by the docstring's type for it."""
    docstring = inspect.getdoc(entrypoint)
    if not docstring:
        return {}
    descriptions: dict[str, str] = {}
    for parameter in parse(docstring).params:
        text = parameter.description or ""
        descriptions[parameter.arg_name] = f"({parameter.type_name}) {text}" if parameter.type_name else text
    return descriptions


def _parameter_schema(hint: object) -> dict[str, Any]:
    """Return one parameter's schema: an unannotated one takes anything, and ``Optional[X]`` is ``X``'s.

    Only ``typing.Optional`` and ``Union[X, None]`` drop the null; ``X | None`` keeps it as an alternative.
    """
    if hint is None:
        return {}
    arguments = get_args(hint)
    if get_origin(hint) is Union and len(arguments) == 2 and NoneType in arguments:
        hint = next(argument for argument in arguments if argument is not NoneType)
    return _schema(hint)


def _schema(hint: Any) -> dict[str, Any]:  # noqa: ANN401, PLR0911 - One branch per kind of annotation.
    """Return the JSON schema of one annotation; a bare ``typing.List`` or ``typing.Dict`` holds strings."""
    origin = get_origin(hint)
    arguments = get_args(hint)
    if origin is Literal:
        return _literal_schema(arguments)
    if origin in (list, tuple, set, frozenset):
        return {"type": "array", "items": _schema(arguments[0]) if arguments else {"type": "string"}}
    if origin is dict:
        return {
            "type": "object",
            "propertyNames": _schema(arguments[0]) if arguments else {"type": "string"},
            "additionalProperties": _schema(arguments[1]) if len(arguments) > 1 else {"type": "string"},
        }
    if origin is Union or origin is UnionType:
        return {"anyOf": [_schema(argument) for argument in arguments]}
    if isinstance(hint, type) and issubclass(hint, Enum):
        return {"type": "string", "enum": [member.value for member in hint]}
    if isinstance(hint, type) and issubclass(hint, BaseModel):
        return _inlined_model_schema(hint.model_json_schema())
    if hint is dict:
        return {"type": "object", "additionalProperties": True}
    json_type = _JSON_TYPES.get(hint.__name__, "object")
    if json_type != "object":
        return {"type": json_type}
    # TODO(simplify): ``Any``, ``object``, and any class without a JSON type render as an object that admits no
    # property, which tells the model it can send only ``{}``; MindRoom's tools annotate such values as a bare ``dict``
    # instead, as custom_tools/microsoft_sharepoint.py does, and the AGNO_COMPAT marker in custom_tools/google_sheets.py
    # works around it in a toolkit that subclasses Agno's. Rendering ``Any`` unconstrained changes those schemas.
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _literal_schema(values: tuple[object, ...]) -> dict[str, Any]:
    """Return the schema of a ``Literal``: its values, typed when they share one JSON type."""
    if all(isinstance(value, str) for value in values):
        return {"type": "string", "enum": list(values)}
    if all(isinstance(value, bool) for value in values):
        return {"type": "boolean", "enum": list(values)}
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return {"type": "integer", "enum": list(values)}
    return {"enum": list(values)}


def _inlined_model_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a pydantic model's JSON schema with each ``$ref`` replaced by the definition it names."""
    definitions = schema.pop("$defs", {})
    return _inlined(schema, definitions, frozenset())


def _inlined(node: Any, definitions: dict[str, Any], active: frozenset[str]) -> Any:  # noqa: ANN401 - JSON values.
    """Return ``node`` with its references inlined; a model nested in itself becomes an object of the same shape."""
    if isinstance(node, list):
        return [_inlined(item, definitions, active) for item in node]
    if not isinstance(node, dict):
        return node
    reference = node.get("$ref")
    if not isinstance(reference, str):
        return {key: _inlined(value, definitions, active) for key, value in node.items()}
    name = reference.removeprefix("#/$defs/")
    if name in active:
        return {"type": "object", "description": f"A nested {name} object of this same shape."}
    return _inlined(definitions[name], definitions, active | {name})


__all__ = ["closed_parameters", "tool_description", "tool_parameters", "validated_entrypoint"]
