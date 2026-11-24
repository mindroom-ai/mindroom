"""Structured agent output: the schema an `agent()` call names, the value in an answer, and what is wrong with it."""

from __future__ import annotations

import json
import re
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as InvalidSchemaError
from jsonschema.exceptions import best_match
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from mindroom.json_utils import has_only_finite_numbers

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# How many turns an agent gets to answer with a value that matches: its first, and four corrections.
SCHEMA_TURNS = 5
_INSTRUCTION = "End your answer with one fenced json block that matches this JSON Schema:"
_FENCED = re.compile(r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```[ \t]*\r?$", re.DOTALL | re.MULTILINE)
# What a schema may reach beside itself: nothing, so no reference is ever fetched.
_NOTHING_ELSE: Registry = Registry()
_REFERENCES = ("$ref", "$dynamicRef")
_INVALID = "schema is not valid JSON Schema: {reason}."


class SchemaError(ValueError):
    """A schema that is no JSON Schema, or an answer that holds no JSON value."""


def _at(path: Iterable[object], message: str) -> str:
    where = "/".join(str(part) for part in path)
    return f"{where}: {message}" if where else message


def _check_draft_schema(schema: object) -> Mapping[str, Any] | bool:
    try:
        Draft202012Validator.check_schema(cast("Mapping[str, Any] | bool", schema))
    except InvalidSchemaError as exc:
        reason = _at(exc.absolute_path or ("schema",), exc.message)
        raise SchemaError(_INVALID.format(reason=reason)) from exc
    return cast("Mapping[str, Any] | bool", schema)


def check_schema(schema: object) -> None:
    """Raise `SchemaError` with where *schema* is no valid JSON Schema and why.

    A schema reaches only what it holds itself: a reference that leaves it, to a URL or a file, is refused and
    never fetched, and so is one that names nothing in it.
    """
    root_schema = _check_draft_schema(schema)
    root = DRAFT202012.create_resource(root_schema)
    root_resolver = _NOTHING_ELSE.resolver_with_root(root)
    pending: deque[tuple[object, Any, bool]] = deque([(root_schema, root_resolver, False)])
    seen: set[tuple[int, str, tuple[str, ...]]] = set()
    while pending:
        contents, resolver, check_target = pending.popleft()
        scope = (id(contents), resolver._base_uri, tuple(dict.fromkeys(uri for uri, _ in resolver.dynamic_scope())))
        if scope in seen:
            continue
        seen.add(scope)
        checked = _check_draft_schema(contents) if check_target else cast("Mapping[str, Any] | bool", contents)
        if isinstance(checked, dict):
            for name in _REFERENCES:
                reference = checked.get(name)
                if not isinstance(reference, str):
                    continue
                if not reference.startswith("#"):
                    raise SchemaError(_INVALID.format(reason=f"reference {reference!r} is not inside the schema"))
                try:
                    resolved = resolver.lookup(reference)
                except (Unresolvable, LookupError, ValueError, TypeError):
                    raise SchemaError(_INVALID.format(reason=f"reference {reference!r} does not resolve")) from None
                pending.append((resolved.contents, resolved.resolver, True))
        resource = DRAFT202012.create_resource(checked)
        pending.extend((sub.contents, resolver.in_subresource(sub), False) for sub in resource.subresources())


def schema_prompt(prompt: str, schema: object) -> str:
    """Return *prompt* with the instruction to end with one fenced json block, and the schema it must match."""
    return f"{prompt}\n\n{_INSTRUCTION}\n{json.dumps(schema, indent=2)}"


def extract_json(text: str) -> object:
    """Return the value of an agent's answer: its last fenced json block, else the whole answer."""
    blocks = _FENCED.findall(text)
    try:
        value = json.loads(blocks[-1] if blocks else text)
    except ValueError:
        msg = "no JSON value found"
        raise SchemaError(msg) from None
    if not has_only_finite_numbers(value):
        msg = "JSON value contains non-finite numbers"
        raise SchemaError(msg)
    return value


def validation_error(value: object, schema: object) -> str | None:
    """Return what is wrong with *value* under *schema*, as `<path>: <message>`, or None when it matches.

    A schema that cannot be applied, whatever the value, raises `SchemaError`: a reference that resolves nowhere,
    or one that never ends. Nothing is fetched for it.
    """
    try:
        error = best_match(
            Draft202012Validator(cast("Mapping[str, Any] | bool", schema), registry=_NOTHING_ELSE).iter_errors(value),
        )
    except Exception as exc:
        msg = f"schema cannot be applied: {type(exc).__name__}: {str(exc).strip()[:200]}"
        raise SchemaError(msg) from exc
    return None if error is None else _at(error.absolute_path, error.message)
