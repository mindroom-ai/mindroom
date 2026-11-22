"""Structured agent output: the schema an `agent()` call names, the value in an answer, and what is wrong with it."""

from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as InvalidSchemaError
from jsonschema.exceptions import best_match

# How many turns an agent gets to answer with a value that matches: its first, and four corrections.
SCHEMA_TURNS = 5
_INSTRUCTION = "End your answer with one fenced json block that matches this JSON Schema:"
_FENCED = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


class SchemaError(ValueError):
    """A schema that is no JSON Schema, or an answer that holds no JSON value."""


def _at(path: object, message: str) -> str:
    where = "/".join(str(part) for part in path)  # type: ignore[attr-defined]
    return f"{where}: {message}" if where else message


def check_schema(schema: object) -> None:
    """Raise `SchemaError` with where *schema* is no valid JSON Schema and why."""
    try:
        Draft202012Validator.check_schema(schema)
    except InvalidSchemaError as exc:
        msg = f"schema is not valid JSON Schema: {_at(exc.absolute_path or ('schema',), exc.message)}."
        raise SchemaError(msg) from exc


def schema_prompt(prompt: str, schema: object) -> str:
    """Return *prompt* with the instruction to end with one fenced json block, and the schema it must match."""
    return f"{prompt}\n\n{_INSTRUCTION}\n{json.dumps(schema, indent=2)}"


def extract_json(text: str) -> object:
    """Return the value of an agent's answer: its last fenced json block, else the whole answer."""
    blocks = _FENCED.findall(text)
    try:
        return json.loads(blocks[-1] if blocks else text)
    except ValueError:
        msg = "no JSON value found"
        raise SchemaError(msg) from None


def validation_error(value: object, schema: object) -> str | None:
    """Return what is wrong with *value* under *schema*, as `<path>: <message>`, or None when it matches."""
    error = best_match(Draft202012Validator(schema).iter_errors(value))
    return None if error is None else _at(error.absolute_path, error.message)
