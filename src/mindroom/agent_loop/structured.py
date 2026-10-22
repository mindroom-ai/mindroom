# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Ask a model for structured output the way its provider supports, and read the answer back into the schema.

Providers with native structured output receive the pydantic schema, JSON-schema providers receive Agno's
``json_schema`` response format, and every other provider receives JSON mode plus a JSON prompt in the system message.
The request formats, the prompt text, and the lenient parsing are Agno 3.0.9's, because helper prompts and
response formats are part of the model-visible parity contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.provider import Capabilities

_MARKDOWN_KEY = re.compile(r'[*`#]?"([A-Za-z0-9_]+)"[*`#]?')
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1F\x7F]")
_QUOTED_VALUE = re.compile(r'"(?P<key>[^"]+)"\s*:\s*"(?P<value>.*?)(?="\s*(?:,|\}))')


@dataclass(frozen=True, slots=True)
class ResponseFormat[T: BaseModel]:
    """How one request asks for ``schema``.

    ``request_format`` is the request's ``response_format``; ``prompt`` is the JSON prompt the system message must
    end with, or None when the provider enforces the schema itself.
    """

    schema: type[T]
    request_format: type[T] | Mapping[str, Any]
    prompt: str | None


def select_response_format[T: BaseModel](capabilities: Capabilities, schema: type[T]) -> ResponseFormat[T]:
    """Return the response format and system-message prompt a provider with ``capabilities`` needs for ``schema``."""
    if capabilities.structured_output == "native":
        return ResponseFormat(schema, schema, None)
    if capabilities.structured_output == "json_schema":
        request_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
        return ResponseFormat(schema, request_format, None)
    return ResponseFormat(schema, {"type": "json_object"}, json_output_prompt(schema))


def json_output_prompt(schema: type[BaseModel]) -> str:
    """Return the system-message prompt that asks a JSON-mode model for an object matching ``schema``."""
    prompt = "Provide your output as a JSON containing the following fields:"
    json_schema = schema.model_json_schema()
    properties: dict[str, Any] = {
        name: _without_title(field) for name, field in (json_schema.get("properties") or {}).items()
    }
    definitions = json_schema.get("$defs")
    if definitions is not None:
        properties["$defs"] = {}
        for name, definition in definitions.items():
            if "enum" in definition:
                properties["$defs"][name] = {
                    "type": "string",
                    "enum": definition["enum"],
                    "description": definition.get("description", ""),
                }
                continue
            fields = {
                field_name: _without_title(field) for field_name, field in (definition.get("properties") or {}).items()
            }
            if fields:
                properties["$defs"][name] = fields
    if properties:
        field_names = [name for name in properties if name != "$defs"]
        prompt += f"\n<json_fields>\n{json.dumps(field_names, ensure_ascii=False)}\n</json_fields>"
        prompt += "\n\nHere are the properties for each field:"
        prompt += f"\n<json_field_properties>\n{json.dumps(properties, indent=2, ensure_ascii=False)}"
        prompt += "\n</json_field_properties>"
    prompt += "\nStart your response with `{` and end it with `}`."
    prompt += "\nYour output will be passed to json.loads() to convert it to a Python object."
    prompt += "\nMake sure it only contains valid JSON."
    return prompt


def _without_title(field: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in field.items() if key != "title"}


def parse_structured[T: BaseModel](content: str, schema: type[T]) -> T | None:
    """Read a model's text answer into ``schema``, or return None when no attempt validates.

    Tries the text as is, then with code fences, markdown around keys, control characters, and unescaped quotes in
    values cleaned up, then each top-level JSON object, merging several by the schema's fields.
    A leading ``<think>`` block is dropped first.
    """
    if "</think>" in content:
        content = _without_thinking(content)
    parsed = _validated(content, schema)
    if parsed is not None:
        return parsed
    cleaned = _cleaned_json(content)
    parsed = _validated(cleaned, schema)
    if parsed is not None:
        return parsed
    candidates = _json_objects(cleaned)
    if len(candidates) == 1:
        parsed = _validated_python(candidates[0], schema)
        if parsed is not None:
            return parsed
    return _merged(candidates, schema)


def _validated[T: BaseModel](text: str, schema: type[T]) -> T | None:
    try:
        return schema.model_validate_json(text)
    except ValidationError:
        return _validated_python(text, schema)


def _validated_python[T: BaseModel](text: str, schema: type[T]) -> T | None:
    try:
        return schema.model_validate(json.loads(text))
    except (ValidationError, json.JSONDecodeError):
        return None


def _without_thinking(content: str) -> str:
    end = content.find("</think>")
    start = content.find("<think>")
    reasoning = content[:end] if start == -1 else content[start + len("<think>") : end]
    # Agno keeps the text unchanged when the thinking block is empty.
    return content[end + len("</think>") :].strip() if reasoning.strip() else content


def _cleaned_json(content: str) -> str:
    if "```json" in content:
        parts = content.rsplit("```json", maxsplit=1)[-1].strip().split("```")
        if len(parts) > 1:
            parts.pop(-1)
        content = "".join(parts)
    elif "```" in content:
        content = content.split("```")[1].strip()
    content = _MARKDOWN_KEY.sub(r'"\1"', content)
    content = _CONTROL_CHARACTERS.sub("", content.replace("\n", " ").replace("\r", ""))
    return _QUOTED_VALUE.sub(_escape_value_quotes, content)


def _escape_value_quotes(match: re.Match[str]) -> str:
    value = match.group("value").replace('\\"', '"').replace('"', '\\"')
    return f'"{match.group("key")}": "{value}'


def _json_objects(text: str) -> list[str]:
    """Return each top-level ``{...}`` span of ``text``, skipping braces inside strings."""
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            in_string, escaped = _string_continues(character, escaped=escaped)
        elif character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects


def _string_continues(character: str, *, escaped: bool) -> tuple[bool, bool]:
    """Return whether a JSON string is still open after ``character``, and whether the next character is escaped."""
    if escaped:
        return True, False
    if character == "\\":
        return True, True
    return character != '"', False


def _merged[T: BaseModel](candidates: list[str], schema: type[T]) -> T | None:
    """Merge the schema's fields across several JSON objects: lists extend, other values take the latest."""
    merged: dict[str, Any] = {}
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        for name in schema.model_fields:
            if name not in data:
                continue
            value = data[name]
            if isinstance(value, list):
                merged.setdefault(name, []).extend(value)
            else:
                merged[name] = value
    if not merged:
        return None
    try:
        return schema.model_validate(merged)
    except ValidationError:
        return None


__all__ = [
    "ResponseFormat",
    "json_output_prompt",
    "parse_structured",
    "select_response_format",
]
