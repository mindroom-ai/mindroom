"""String tool-call arguments reach the tool as the model sent them."""

from __future__ import annotations

import json
from typing import Literal, Optional

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


def _hand_written(properties: dict[str, object], **schema: object) -> ToolFunction:
    return ToolFunction(
        name="lookup",
        description=None,
        parameters={"type": "object", "properties": properties, **schema},
        entrypoint=lambda **kwargs: kwargs,
    )


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


def _list_page_nullable(page_token: str | None = None) -> str:
    """List one page."""
    return page_token or "first"


def _list_page_optional(page_token: Optional[str] = None) -> str:  # noqa: UP045 - the schema omits null.
    """List one page."""
    return page_token or "first"


def _list_page_default(page_token: str = "") -> str:
    """List one page."""
    return page_token or "first"


@pytest.mark.parametrize("word", NULL_WORDS)
@pytest.mark.parametrize("list_page", [_list_page_nullable, _list_page_optional, _list_page_default])
def test_a_string_with_a_default_reads_null_words_as_left_out(list_page: object, word: str) -> None:
    """Models send "None" or "null" to leave out a string they may omit, such as a page token.

    Agno leaves null out of the schema of ``Optional[str]`` and of a plain default, so the default decides.
    """
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
    function = _hand_written(
        {
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
            "looping": {"$ref": "#/$defs/Loop"},
        },
        required=["name", "kind", "label", "either"],
        **{"$defs": {"Kind": {"type": "string", "enum": ["null", "all"]}, "Loop": {"$ref": "#/$defs/Loop"}}},
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
        "looping": "true",
        "undeclared": "false",
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
        "looping": True,
        "undeclared": False,
    }
    assert _parsed(function, json.dumps({"cursor": "false", "label": "true"})) == {"cursor": "false", "label": "true"}


def test_choices_combined_with_a_string_type_stay_choices() -> None:
    """A string type narrowed to choices by allOf or a reference reads words that are not choices as values."""
    function = _hand_written(
        {
            "narrowed": {"type": "string", "allOf": [{"enum": ["none", "all"]}]},
            "referenced": {"type": "string", "$ref": "#/$defs/a~1b"},
            "either": {"anyOf": [{"$ref": "#/$defs/a~1b"}, {"type": "null"}]},
        },
        **{"$defs": {"a/b": {"enum": ["none", "all"]}}},
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


def test_self_referencing_schemas_read_literal_words_as_values() -> None:
    """A schema too large to read, such as a reference that branches into itself, reads the words as values."""
    function = _hand_written(
        {"node": {"$ref": "#/$defs/Node"}},
        **{"$defs": {"Node": {"type": "string", "anyOf": [{"$ref": "#/$defs/Node"}, {"$ref": "#/$defs/Node"}]}}},
    )

    assert _parsed(function, json.dumps({"node": "true"})) == {"node": True}
