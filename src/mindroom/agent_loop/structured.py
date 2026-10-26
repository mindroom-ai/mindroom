# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Ask a model for structured output the way its provider supports, and read the answer back into the schema.

Each adapter renders a request's output schema in its provider's wire format; a route that only offers JSON mode
also needs a JSON prompt, derived from the schema, at the end of the system message. An answer from a route that
enforces the schema is parsed strictly; only a JSON-mode answer gets the lenient repairs.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from mindroom.llm.provider import Capabilities

_MARKDOWN_KEY = re.compile(r'[*`#]?"([A-Za-z0-9_]+)"[*`#]?')
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1F\x7F]")
_QUOTED_VALUE = re.compile(r'"(?P<key>[^"]+)"\s*:\s*"(?P<value>.*?)(?="\s*(?:,|\}))')


def output_format_prompt(capabilities: Capabilities, schema: type[BaseModel] | None) -> str | None:
    """Return the JSON prompt a system message asking for ``schema`` must end with, or None when none is needed.

    Only a route that enforces no schema itself needs it.
    """
    if schema is None or capabilities.structured_output != "json_mode":
        return None
    return json_output_prompt(schema)


def json_output_prompt(schema: type[BaseModel]) -> str:
    """Return the system-message prompt that asks a JSON-mode model for an object matching ``schema``."""
    json_schema = json.dumps(schema.model_json_schema(), indent=2, ensure_ascii=False)
    return (
        "Answer with one JSON object that matches this JSON schema, and nothing else: no code fence, no text around it."
        f"\n<json_schema>\n{json_schema}\n</json_schema>"
    )


def parse_structured[T: BaseModel](content: str, schema: type[T], *, lenient: bool) -> T | None:
    """Read a model's text answer into ``schema``, or return None when it does not validate.

    Inline reasoning is dropped first (``_answer_text``). A route that enforces the schema answers with the object, so
    its answer is validated as it is. With ``lenient``, for a JSON-mode answer, the text is tried with code fences,
    markdown around keys, control characters, and unescaped quotes in values cleaned up, then each top-level JSON
    object, merging several by the schema's fields.
    """
    content = _answer_text(content)
    parsed = _validated(content, schema)
    if parsed is not None or not lenient:
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


def _answer_text(content: str) -> str:
    """Return the answer of a complete reply's text: what follows its first ``</think>``, else all of it.

    Streamed replies split inline reasoning by a narrower rule (``ThinkTagSplitter``, ``providers/assistant_turn.py``):
    only a block the reply opens with ``<think>``, because a stream cannot hold its text back until a ``</think>``
    that may never come. A JSON answer needs the wider rule: a chat template that opens the block in the prompt
    leaves the model writing only its end (``reasoning</think>{...}``), and reasoning left before the JSON can carry
    draft objects that the lenient parse below would merge into the answer.
    """
    _, closed, answer = content.partition("</think>")
    return answer if closed else content


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
    "json_output_prompt",
    "output_format_prompt",
    "parse_structured",
]
