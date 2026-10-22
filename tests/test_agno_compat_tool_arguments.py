"""String tool-call arguments reach the tool as the model sent them."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.response import ToolExecution
from agno.tools.function import Function

# Importing the tool hook bridge installs the repair, as it does for every agent MindRoom builds.
import mindroom.tool_system.tool_hooks  # noqa: F401

LITERAL_WORDS = ("none", "None", "null", "NULL", "true", "True", "false", " FALSE ")


def note(
    text: str,
    mode: Literal["none", "all"] = "all",
    title: str | None = None,
    pinned: bool = False,
    count: int | None = None,
) -> str:
    """Record a note."""
    return f"{title or mode}: {text} ({pinned}, {count})"


def _model() -> OpenAIChat:
    return OpenAIChat(id="gpt-test", api_key="test-key")


def _parsed(function: Function, arguments: str) -> dict[str, Any] | None:
    """Parse one tool call the way a model's response loop does."""
    message = Message(
        role="assistant",
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": function.name, "arguments": arguments}}],
    )
    errors: list[Message] = []
    [call] = _model().get_function_calls_to_run(message, errors, {function.name: function})
    assert errors == []
    return call.arguments


@pytest.mark.parametrize("word", LITERAL_WORDS)
def test_string_parameters_receive_literal_words_unchanged(word: str) -> None:
    """Free text, a string choice, and an optional string each receive the word the model sent."""
    arguments = {"text": word, "mode": "none", "title": word}

    assert _parsed(Function.from_callable(note), json.dumps(arguments)) == arguments


def test_a_json_null_still_reaches_a_nullable_string_parameter_as_none() -> None:
    """Only the model's own null clears an optional string."""
    assert _parsed(Function.from_callable(note), json.dumps({"text": "hi", "title": None})) == {
        "text": "hi",
        "title": None,
    }


def test_other_parameters_keep_agnos_conversion_of_literal_words() -> None:
    """Models send "true" for booleans and "null" for optional numbers, which Agno still converts."""
    function = Function.from_callable(note)

    assert _parsed(function, json.dumps({"text": "hi", "pinned": "true", "count": "null"})) == {
        "text": "hi",
        "pinned": True,
        "count": None,
    }
    assert _parsed(function, json.dumps({"text": "hi", "pinned": " False "})) == {"text": "hi", "pinned": False}


def test_strict_schemas_keep_string_arguments() -> None:
    """Strict schemas, which make optional parameters nullable unions, keep string arguments."""
    function = Function.from_callable(note, strict=True)
    function.process_entrypoint(strict=True)

    assert _parsed(function, json.dumps({"text": "null", "mode": "none", "title": "None", "pinned": "true"})) == {
        "text": "null",
        "mode": "none",
        "title": "None",
        "pinned": True,
    }


def test_python_literal_arguments_keep_string_arguments() -> None:
    """Arguments Agno decodes as a Python literal keep their strings too."""
    assert _parsed(Function.from_callable(note), "{'text': 'None', 'pinned': 'true'}") == {
        "text": "None",
        "pinned": True,
    }


def test_hand_written_schemas_keep_string_arguments() -> None:
    """Schemas written by hand, as MCP servers send them, keep string arguments wherever they admit a string."""
    function = Function(
        name="lookup",
        parameters={
            "type": "object",
            "properties": {
                "kind": {"$ref": "#/$defs/Kind"},
                "label": {"type": ["string", "null"]},
                "choice": {"enum": ["none", "some"]},
                "either": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                "flag": {"type": "boolean"},
                "untyped": {},
            },
            "$defs": {"Kind": {"type": "string", "enum": ["null", "all"]}},
        },
        entrypoint=lambda **kwargs: kwargs,
        skip_entrypoint_processing=True,
    )
    arguments = {
        "kind": "null",
        "label": "false",
        "choice": "none",
        "either": "true",
        "flag": "true",
        "untyped": "None",
    }

    assert _parsed(function, json.dumps(arguments)) == {
        "kind": "null",
        "label": "false",
        "choice": "none",
        "either": "true",
        "flag": True,
        "untyped": None,
    }


def test_a_resumed_call_keeps_string_arguments() -> None:
    """Agno parses a paused call's stored arguments again when an approval resumes it."""
    function = Function.from_callable(note)
    paused = ToolExecution(tool_call_id="call-1", tool_name="note", tool_args={"text": "None", "pinned": "true"})

    call = _model().get_function_call_to_run_from_tool_execution(paused, {"note": function})

    assert call.arguments == {"text": "None", "pinned": True}
