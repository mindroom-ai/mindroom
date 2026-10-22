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
BOOLEAN_WORDS = ("true", "True", "false", " FALSE ")
NULL_WORDS = ("none", "None", "null", "NULL", " none ")


def note(
    text: str,
    mode: Literal["none", "all"] = "all",
    title: str | None = None,
    scope: Literal["none", "all"] | None = None,
    pinned: bool = False,
    count: int | None = None,
) -> str:
    """Record a note."""
    return f"{title or mode}: {text} ({scope}, {pinned}, {count})"


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
def test_a_required_string_receives_literal_words_unchanged(word: str) -> None:
    """A string parameter that cannot be left out receives the word the model sent."""
    assert _parsed(Function.from_callable(note), json.dumps({"text": word})) == {"text": word}


@pytest.mark.parametrize("parameter", ["mode", "scope"])
def test_a_choice_receives_its_exact_value_unchanged(parameter: str) -> None:
    """A choice named none reaches the tool, whether or not the parameter is optional."""
    assert _parsed(Function.from_callable(note), json.dumps({"text": "hi", parameter: "none"})) == {
        "text": "hi",
        parameter: "none",
    }


@pytest.mark.parametrize("word", ["None", "null", " NONE "])
def test_an_optional_choice_reads_other_null_words_as_left_out(word: str) -> None:
    """Only the exact choice is kept; other null words leave an optional choice out, as before."""
    assert _parsed(Function.from_callable(note), json.dumps({"text": "hi", "scope": word})) == {
        "text": "hi",
        "scope": None,
    }


@pytest.mark.parametrize("word", BOOLEAN_WORDS)
def test_an_optional_string_receives_boolean_words_unchanged(word: str) -> None:
    """An optional string's title of true or false reaches the tool as text."""
    assert _parsed(Function.from_callable(note), json.dumps({"text": "hi", "title": word})) == {
        "text": "hi",
        "title": word,
    }


@pytest.mark.parametrize("word", NULL_WORDS)
def test_an_optional_string_reads_null_words_as_left_out(word: str) -> None:
    """Models send "None" or "null" to leave an optional string, such as a page token, out."""

    def list_page(page_token: str | None = None) -> str:
        """List one page."""
        return page_token or "first"

    assert _parsed(Function.from_callable(list_page), json.dumps({"page_token": word})) == {"page_token": None}


def test_a_json_null_still_reaches_an_optional_string_as_none() -> None:
    """The model's own null leaves an optional string out."""
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


def test_python_literal_arguments_keep_string_arguments() -> None:
    """Arguments Agno decodes as a Python literal keep their strings too."""
    assert _parsed(Function.from_callable(note), "{'text': 'None', 'pinned': 'true'}") == {
        "text": "None",
        "pinned": True,
    }


def test_hand_written_schemas_keep_string_arguments() -> None:
    """Schemas written by hand, as MCP servers send them, follow the same rule."""
    function = Function(
        name="lookup",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cursor": {"type": "string"},
                "kind": {"$ref": "#/$defs/Kind"},
                "label": {"type": ["string", "null"]},
                "choice": {"enum": ["none", "some", None]},
                "either": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                "flag": {"type": "boolean"},
                "untyped": {},
                "malformed": {"type": {"not": "a type"}, "enum": 5, "anyOf": "string"},
                "switch": {"type": "boolean", "enum": ["true", True]},
            },
            "required": ["name", "kind", "label", "either"],
            "$defs": {"Kind": {"type": "string", "enum": ["null", "all"]}},
        },
        entrypoint=lambda **kwargs: kwargs,
        skip_entrypoint_processing=True,
    )
    arguments = {
        "name": "None",
        "cursor": "None",
        "kind": "null",
        "label": "null",
        "choice": "none",
        "either": "null",
        "flag": "true",
        "untyped": "None",
        "malformed": "None",
        "switch": "true",
    }

    assert _parsed(function, json.dumps(arguments)) == {
        "name": "None",
        "cursor": None,
        "kind": "null",
        "label": None,
        "choice": "none",
        "either": "null",
        "flag": True,
        "untyped": None,
        "malformed": None,
        "switch": True,
    }
    assert _parsed(function, json.dumps({"cursor": "false", "label": "true"})) == {"cursor": "false", "label": "true"}


def test_a_resumed_call_keeps_string_arguments() -> None:
    """Agno parses a paused call's stored arguments again when an approval resumes it."""
    function = Function.from_callable(note)
    paused = ToolExecution(
        tool_call_id="call-1",
        tool_name="note",
        tool_args={"text": "None", "scope": "none", "title": "null", "pinned": "true"},
    )

    call = _model().get_function_call_to_run_from_tool_execution(paused, {"note": function})

    assert call.arguments == {"text": "None", "scope": "none", "title": None, "pinned": True}


def test_choices_combined_with_a_string_type_stay_choices() -> None:
    """A string type narrowed to choices by allOf or a reference keeps Agno's None for words that are not choices."""
    function = Function(
        name="pick",
        parameters={
            "type": "object",
            "properties": {
                "narrowed": {"type": "string", "allOf": [{"enum": ["none", "all"]}]},
                "referenced": {"type": "string", "$ref": "#/$defs/a~1b"},
                "either": {"anyOf": [{"$ref": "#/$defs/a~1b"}, {"type": "null"}]},
            },
            "$defs": {"a/b": {"enum": ["none", "all"]}},
        },
        entrypoint=lambda **kwargs: kwargs,
        skip_entrypoint_processing=True,
    )

    assert _parsed(function, json.dumps({"narrowed": "none", "referenced": "none", "either": "none"})) == {
        "narrowed": "none",
        "referenced": "none",
        "either": "none",
    }
    assert _parsed(function, json.dumps({"narrowed": "null", "referenced": "True", "either": "null"})) == {
        "narrowed": None,
        "referenced": True,
        "either": None,
    }


def test_self_referencing_schemas_fall_back_to_agnos_conversion() -> None:
    """A schema too large to read, such as a reference that branches into itself, keeps Agno's conversion."""
    function = Function(
        name="tree",
        parameters={
            "type": "object",
            "properties": {"node": {"$ref": "#/$defs/Node"}},
            "$defs": {"Node": {"type": "string", "anyOf": [{"$ref": "#/$defs/Node"}, {"$ref": "#/$defs/Node"}]}},
        },
        entrypoint=lambda **kwargs: kwargs,
        skip_entrypoint_processing=True,
    )

    assert _parsed(function, json.dumps({"node": "true"})) == {"node": True}
