"""String tool-call arguments reach the tool as the model sent them."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from agno.tools.function import Function

from mindroom.tool_system.agno_toolkits import adapt_function
from mindroom.tool_system.invocation import parse_tool_arguments
from mindroom.tool_system.tool_function import ToolFunction

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


def _note(*, strict: bool = False) -> ToolFunction:
    function = Function.from_callable(note)
    function.strict = strict or None
    return adapt_function(function)


def _parsed(function: ToolFunction, arguments: str) -> object:
    """Parse one tool call the way the model loop does."""
    return parse_tool_arguments(arguments, function)


@pytest.mark.parametrize("word", LITERAL_WORDS)
def test_a_required_string_receives_literal_words_unchanged(word: str) -> None:
    """A string parameter that cannot be left out receives the word the model sent."""
    assert _parsed(_note(), json.dumps({"text": word})) == {"text": word}


@pytest.mark.parametrize("parameter", ["mode", "scope"])
def test_a_choice_receives_its_exact_value_unchanged(parameter: str) -> None:
    """A choice named none reaches the tool, whether or not the parameter is optional."""
    assert _parsed(_note(), json.dumps({"text": "hi", parameter: "none"})) == {"text": "hi", parameter: "none"}


@pytest.mark.parametrize("word", ["None", "null", " NONE "])
def test_an_optional_choice_reads_other_null_words_as_left_out(word: str) -> None:
    """Only the exact choice is kept; other null words leave an optional choice out."""
    assert _parsed(_note(), json.dumps({"text": "hi", "scope": word})) == {"text": "hi", "scope": None}


@pytest.mark.parametrize("word", BOOLEAN_WORDS)
def test_an_optional_string_receives_boolean_words_unchanged(word: str) -> None:
    """An optional string's title of true or false reaches the tool as text."""
    assert _parsed(_note(), json.dumps({"text": "hi", "title": word})) == {"text": "hi", "title": word}


@pytest.mark.parametrize("word", NULL_WORDS)
def test_an_optional_string_reads_null_words_as_left_out(word: str) -> None:
    """Models send "None" or "null" to leave an optional string, such as a page token, out."""

    def list_page(page_token: str | None = None) -> str:
        """List one page."""
        return page_token or "first"

    function = adapt_function(Function.from_callable(list_page))

    assert _parsed(function, json.dumps({"page_token": word})) == {"page_token": None}


def test_a_json_null_still_reaches_an_optional_string_as_none() -> None:
    """The model's own null leaves an optional string out."""
    assert _parsed(_note(), json.dumps({"text": "hi", "title": None})) == {"text": "hi", "title": None}


def test_other_parameters_read_literal_words_as_values() -> None:
    """Models send "true" for booleans and "null" for optional numbers, which still read as values."""
    function = _note()

    assert _parsed(function, json.dumps({"text": "hi", "pinned": "true", "count": "null"})) == {
        "text": "hi",
        "pinned": True,
        "count": None,
    }
    assert _parsed(function, json.dumps({"text": "hi", "pinned": " False "})) == {"text": "hi", "pinned": False}


def test_strict_schemas_keep_string_arguments() -> None:
    """Strict schemas, which make every parameter required, keep string arguments by the same rule."""
    function = _note(strict=True)
    arguments = {"text": "null", "mode": "none", "title": "true", "scope": "none", "pinned": "true", "count": "null"}

    assert function.parameters["required"] == list(arguments)
    assert _parsed(function, json.dumps(arguments)) == {**arguments, "pinned": True, "count": None}
    assert _parsed(function, json.dumps({"text": "hi", "title": "None"})) == {"text": "hi", "title": None}


def test_python_literal_arguments_keep_string_arguments() -> None:
    """Arguments decoded as a Python literal keep their strings too."""
    assert _parsed(_note(), "{'text': 'None', 'pinned': 'true'}") == {"text": "None", "pinned": True}


def test_hand_written_schemas_keep_string_arguments() -> None:
    """Schemas written by hand, as MCP servers send them, follow the same rule."""
    function = ToolFunction(
        name="lookup",
        description=None,
        parameters={
            "type": "object",
            "properties": {
                "kind": {"$ref": "#/$defs/Kind"},
                "label": {"type": ["string", "null"]},
                "cursor": {"type": ["string", "null"]},
                "choice": {"enum": ["none", "some", None]},
                "either": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                "flag": {"type": "boolean"},
                "untyped": {},
                "malformed": {"type": {"not": "a type"}, "enum": 5, "anyOf": "string"},
                "looping": {"$ref": "#/$defs/Loop"},
            },
            "$defs": {"Kind": {"type": "string", "enum": ["null", "all"]}, "Loop": {"$ref": "#/$defs/Loop"}},
        },
        entrypoint=lambda **kwargs: kwargs,
    )
    arguments = {
        "kind": "null",
        "label": "false",
        "cursor": "null",
        "choice": "none",
        "either": "true",
        "flag": "true",
        "untyped": "None",
        "malformed": "None",
        "looping": "true",
        "undeclared": "false",
    }

    assert _parsed(function, json.dumps(arguments)) == {
        "kind": "null",
        "label": "false",
        "cursor": None,
        "choice": "none",
        "either": "true",
        "flag": True,
        "untyped": None,
        "malformed": None,
        "looping": True,
        "undeclared": False,
    }
