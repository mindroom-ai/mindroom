"""The native schema generator renders every annotation kind and docstring style deterministically."""

# The annotation-kind probes exist for their signatures, which no body reads.
# ruff: noqa: ARG001, ARG002

from __future__ import annotations

import gc
import json
import weakref
from collections.abc import AsyncIterator  # noqa: TC003 - Validation resolves the probe's annotations.
from enum import Enum
from typing import (  # noqa: UP035 - The bare typing aliases are annotation-kind cases.
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Union,
)

import pytest
from pydantic import BaseModel

from mindroom.custom_tools.matrix_api import MatrixApiTools
from mindroom.custom_tools.thread_tags import ThreadTagsTools
from mindroom.tool_jobs.background_after import BACKGROUND_AFTER, background_after_schema
from mindroom.tool_jobs.execution import project_definitions
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT, ToolOutputFilePolicy, with_output_files
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.tool_schema import tool_description, tool_parameters, validated_entrypoint
from mindroom.tool_system.toolkits import adapt_callable, adapt_toolkit
from mindroom.tools.custom_api import custom_api_tools
from mindroom.tools.pandas import pandas_tools
from mindroom.tools.sql import sql_tools
from tests.agent_tool_helpers import toolkit_offers
from tests.agno_parity import tool_argument_probes as probes
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ARGUMENTS = json.loads(support.ARGUMENTS_PATH.read_text(encoding="utf-8"))


def owned_derivation(entrypoint: Callable[..., Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the description and parameters the owned generator derives for ``entrypoint``."""
    return tool_description(entrypoint), tool_parameters(entrypoint)


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def _string_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "propertyNames": {"type": "string"}, "additionalProperties": value}


def assert_same_bytes(owned: object, expected: object) -> None:
    """Compare serialized bytes, key order included, because providers receive schemas serialized."""
    assert json.dumps(owned, indent=2) == json.dumps(expected, indent=2)


class Color(Enum):
    """Choices a tool takes by value."""

    RED = "red"
    GREEN = "green"


class Level(Enum):
    """Choices whose values are numbers."""

    LOW = 1
    HIGH = 2


class Inner(BaseModel):
    """A model nested in another."""

    key: str
    weight: float = 1.0


class Outer(BaseModel):
    """A model holding nested models."""

    name: str
    inner: Inner
    extras: list[Inner] = []
    note: str | None = None
    maybe: Inner | None = None


class TreeNode(BaseModel):
    """A model nested in itself."""

    label: str
    children: list[TreeNode] = []


def scalars(count: int, ratio: float, enabled: bool, label: str) -> str:
    """Take scalars.

    Args:
        count: A whole number.
        ratio: A decimal number.
        enabled: A flag.
        label: Free text.

    """
    return label


def optionals(
    typed: Optional[str] = None,  # noqa: UP045 - typing.Optional must match X | None.
    union: Union[int, None] = None,  # noqa: UP007
    piped: int | None = None,
    choice: Union[Literal["a", "b"], None] = None,  # noqa: UP007
    flag: bool = False,
) -> str:
    """Take optional values and defaults.

    Args:
        typed: Optional text.
        union: Optional number.
        piped: Optional number with a pipe.
        choice: An optional choice.
        flag: A defaulted flag.

    """
    return ""


def literals(
    words: Literal["fast", "slow"],
    numbers: Literal[1, 2],
    flags: Literal[True],
    mixed: Literal["a", 1],
) -> str:
    """Take constrained choices."""
    return ""


def enums(color: Color, level: Level = Level.LOW) -> str:
    """Take enumerations."""
    return ""


def collections(
    tags: list[str],
    pairs: tuple[int, ...],
    unique: set[str],
    frozen: frozenset[int],
    bare: list,
    legacy: List,  # noqa: UP006 - A bare typing.List holds strings.
    rows: list[list[str | int | float | bool | None]],
    either: str | list[str],
) -> str:
    """Take sequences."""
    return ""


def mappings(
    bare: dict,
    legacy: Dict,  # noqa: UP006 - A bare typing.Dict maps strings to strings.
    typed: dict[str, int],
    anything: dict[str, Any],
    objects: dict[str, object],
    unions: dict[str, str | int | bool],
    nested: dict[str, list[str]] | None = None,
) -> str:
    """Take mappings."""
    return ""


def models(outer: Outer, tree: TreeNode, many: list[Inner] | None = None) -> str:
    """Take structured values."""
    return ""


def untyped(value, anything: Any, thing: object) -> str:  # noqa: ANN001, ANN401
    """Take values of any type."""
    return ""


def test_values_annotated_any_or_object_take_any_json_value() -> None:
    """``Any`` and ``object`` are unconstrained wherever they appear, where Agno 3.0.9 admits only an empty object."""

    def open_values(
        anything: Any,  # noqa: ANN401
        thing: object,
        mapping: dict[str, Any],
        objects: dict[str, object] | None,
        rows: list[list[Any]],
    ) -> str:
        return ""

    mapping = _string_mapping({})
    assert tool_parameters(open_values)["properties"] == {
        "anything": {},
        "thing": {},
        "mapping": mapping,
        "objects": {"anyOf": [mapping, {"type": "null"}]},
        "rows": {"type": "array", "items": {"type": "array", "items": {}}},
    }


def test_owned_tools_taking_any_values_accept_real_arguments() -> None:
    """A thread tag's data, a Matrix event's content, and a custom API request body pass argument validation.

    A value that is not an object is still refused there, so a tool body never receives one.
    """
    thread_tags = toolkit_offers(ThreadTagsTools(), "thread_tags")
    matrix_api = toolkit_offers(MatrixApiTools(), "matrix_api_write")
    make_request = toolkit_offers(custom_api_tools()(), "custom_api_request")
    event = {"action": "send_event", "event_type": "m.room.message", "content": {"msgtype": "m.text", "body": "Hi"}}
    request = {
        "endpoint": "https://api.example.com/items",
        "method": "POST",
        "json_data": {"name": "lamp", "tags": ["new"], "price": 12.5},
    }

    validate_tool_arguments(
        dict(thread_tags.parameters),
        {"action": "tag", "tag": "blocked", "data": {"priority": "high"}},
    )
    validate_tool_arguments(dict(matrix_api.parameters), event)
    validate_tool_arguments(dict(make_request.parameters), request)
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(dict(matrix_api.parameters), {**event, "content": "Hi"})
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(dict(make_request.parameters), {**request, "json_data": "lamp"})


def test_vendor_tools_taking_any_values_accept_real_arguments() -> None:
    """A pandas operation's own documented arguments pass argument validation.

    A value that is not an object is still refused there, so a tool body never receives one.
    """
    operation = toolkit_offers(pandas_tools()(), "pandas_run_operation")
    arguments = {"dataframe_name": "sales", "operation": "head", "operation_parameters": {"n": 5}}

    validate_tool_arguments(dict(operation.parameters), arguments)
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(dict(operation.parameters), {**arguments, "operation_parameters": "five"})


def no_docstring(value: str) -> str:  # noqa: D103 - The case is a function without one.
    return value


def google_style(query: str, limit: int = 5) -> str:
    """Search for something.

    A longer body that explains
    over two lines.

    Args:
        query (str): The query.
        limit: How many results.

    Returns:
        The results.

    """
    return query


def numpy_style(query: str, limit: int = 5) -> str:
    """Search for something.

    Parameters
    ----------
    query : str
        The query.
    limit : int
        How many results.

    """
    return query


def rest_style(query: str, limit: int = 5) -> str:
    """Search for something.

    :param query: The query.
    :type query: str
    :param limit: How many results.
    """
    return query


def undocumented_parameter(query: str, limit: int = 5) -> str:  # noqa: D417 - The case documents query blank.
    """Search for something.

    Args:
        query:
        limit: How many results.

    """
    return query


async def asynchronous(query: str) -> str:
    """Search asynchronously.

    Args:
        query: The query.

    """
    return query


def keyword_only(query: str, *, limit: int = 5, required: bool) -> str:
    """Take keyword-only parameters."""
    return query


ANNOTATION_KINDS = (
    scalars,
    optionals,
    literals,
    enums,
    collections,
    mappings,
    models,
    untyped,
    no_docstring,
    google_style,
    numpy_style,
    rest_style,
    undocumented_parameter,
    asynchronous,
    keyword_only,
)


_INNER_MODEL_SCHEMA: dict[str, Any] = {
    "description": "A model nested in another.",
    "properties": {
        "key": {"title": "Key", "type": "string"},
        "weight": {"default": 1.0, "title": "Weight", "type": "number"},
    },
    "required": ["key"],
    "title": "Inner",
    "type": "object",
}

_EXPECTED_ANNOTATION_SCHEMAS: dict[str, tuple[str | None, dict[str, Any]]] = {
    "scalars": (
        "Take scalars.",
        _object_schema(
            {
                "count": {"type": "integer", "description": "A whole number."},
                "ratio": {"type": "number", "description": "A decimal number."},
                "enabled": {"type": "boolean", "description": "A flag."},
                "label": {"type": "string", "description": "Free text."},
            },
        ),
    ),
    "optionals": (
        "Take optional values and defaults.",
        _object_schema(
            {
                "typed": {"type": "string", "description": "Optional text."},
                "union": {"type": "integer", "description": "Optional number."},
                "piped": {"type": "integer", "description": "Optional number with a pipe."},
                "choice": {"type": "string", "enum": ["a", "b"], "description": "An optional choice."},
                "flag": {"type": "boolean", "description": "A defaulted flag."},
            },
            required=[],
        ),
    ),
    "literals": (
        "Take constrained choices.",
        _object_schema(
            {
                "words": {"type": "string", "enum": ["fast", "slow"]},
                "numbers": {"type": "integer", "enum": [1, 2]},
                "flags": {"type": "boolean", "enum": [True]},
                "mixed": {"enum": ["a", 1]},
            },
        ),
    ),
    "enums": (
        "Take enumerations.",
        _object_schema(
            {"color": {"type": "string", "enum": ["red", "green"]}, "level": {"type": "string", "enum": [1, 2]}},
            required=["color"],
        ),
    ),
    "collections": (
        "Take sequences.",
        _object_schema(
            {
                "tags": {"type": "array", "items": {"type": "string"}},
                "pairs": {"type": "array", "items": {"type": "integer"}},
                "unique": {"type": "array", "items": {"type": "string"}},
                "frozen": {"type": "array", "items": {"type": "integer"}},
                "bare": {"type": "array"},
                "legacy": {"type": "array", "items": {"type": "string"}},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "integer"},
                                {"type": "number"},
                                {"type": "boolean"},
                                {"type": "null"},
                            ],
                        },
                    },
                },
                "either": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            },
        ),
    ),
    "mappings": (
        "Take mappings.",
        _object_schema(
            {
                "bare": {"type": "object", "additionalProperties": True},
                "legacy": _string_mapping({"type": "string"}),
                "typed": _string_mapping({"type": "integer"}),
                "anything": _string_mapping({}),
                "objects": _string_mapping({}),
                "unions": _string_mapping(
                    {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]},
                ),
                "nested": _string_mapping({"type": "array", "items": {"type": "string"}}),
            },
            required=["bare", "legacy", "typed", "anything", "objects", "unions"],
        ),
    ),
    "models": (
        "Take structured values.",
        _object_schema(
            {
                "outer": {
                    "description": "A model holding nested models.",
                    "properties": {
                        "name": {"title": "Name", "type": "string"},
                        "inner": _INNER_MODEL_SCHEMA,
                        "extras": {"default": [], "items": _INNER_MODEL_SCHEMA, "title": "Extras", "type": "array"},
                        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Note"},
                        "maybe": {"anyOf": [_INNER_MODEL_SCHEMA, {"type": "null"}], "default": None},
                    },
                    "required": ["name", "inner"],
                    "title": "Outer",
                    "type": "object",
                },
                "tree": {
                    "description": "A model nested in itself.",
                    "properties": {
                        "label": {"title": "Label", "type": "string"},
                        "children": {
                            "default": [],
                            "items": {"type": "object", "description": "A nested TreeNode object of this same shape."},
                            "title": "Children",
                            "type": "array",
                        },
                    },
                    "required": ["label"],
                    "title": "TreeNode",
                    "type": "object",
                },
                "many": {"type": "array", "items": _INNER_MODEL_SCHEMA},
            },
            required=["outer", "tree"],
        ),
    ),
    "untyped": ("Take values of any type.", _object_schema({"value": {}, "anything": {}, "thing": {}})),
    "no_docstring": ("", _object_schema({"value": {"type": "string"}})),
    "google_style": (
        "Search for something.\nA longer body that explains\nover two lines.",
        _object_schema(
            {
                "query": {"type": "string", "description": "(str) The query."},
                "limit": {"type": "integer", "description": "How many results."},
            },
            required=["query"],
        ),
    ),
    "numpy_style": (
        "Search for something.",
        _object_schema(
            {
                "query": {"type": "string", "description": "(str) The query."},
                "limit": {"type": "integer", "description": "(int) How many results."},
            },
            required=["query"],
        ),
    ),
    "rest_style": (
        "Search for something.",
        _object_schema(
            {
                "query": {"type": "string", "description": "(str) The query."},
                "limit": {"type": "integer", "description": "How many results."},
            },
            required=["query"],
        ),
    ),
    "undocumented_parameter": (
        "Search for something.",
        _object_schema(
            {"query": {"type": "string"}, "limit": {"type": "integer", "description": "How many results."}},
            required=["query"],
        ),
    ),
    "asynchronous": (
        "Search asynchronously.",
        _object_schema({"query": {"type": "string", "description": "The query."}}),
    ),
    "keyword_only": (
        "Take keyword-only parameters.",
        _object_schema(
            {"query": {"type": "string"}, "limit": {"type": "integer"}, "required": {"type": "boolean"}},
            required=["query", "required"],
        ),
    ),
}


@pytest.mark.parametrize("entrypoint", ANNOTATION_KINDS, ids=lambda entrypoint: entrypoint.__name__)
def test_each_annotation_kind_derives_expected_schema(entrypoint: Callable[..., Any]) -> None:
    """Each kind of annotation, default, and docstring style derives its pinned schema bytes."""
    assert_same_bytes(owned_derivation(entrypoint), _EXPECTED_ANNOTATION_SCHEMAS[entrypoint.__name__])


def test_a_bound_method_derives_expected_schema() -> None:
    """A method's ``self`` is bound, so only its own parameters reach the schema."""

    class Holder:
        def method(self, query: str, limit: int = 5) -> str:
            """Search.

            Args:
                query: The query.
                limit: How many.

            """
            return query

    assert_same_bytes(
        owned_derivation(Holder().method),
        (
            "Search.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The query."},
                    "limit": {"type": "integer", "description": "How many."},
                },
                "required": ["query"],
            },
        ),
    )


def test_variadic_parameters_stay_out_of_the_schema() -> None:
    """``*args`` and ``**kwargs`` are no named argument the model could send."""

    def variadic(query: str, *args: str, **kwargs: int) -> str:
        return query

    assert tool_parameters(variadic) == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }


@pytest.mark.asyncio
async def test_a_validated_stream_closes_the_stream_it_wraps() -> None:
    """A consumer that stops early closes the validated stream, which closes the tool's own."""
    closed: list[bool] = []

    async def count_up(limit: int) -> AsyncIterator[int]:
        try:
            for number in range(limit):
                yield number
        finally:
            closed.append(True)

    stream = validated_entrypoint(count_up)("3")

    assert await anext(stream) == 0
    await stream.aclose()
    assert closed == [True]


def _takes_a_count(count: int) -> str:
    return str(count)


class _CallerState:
    """Stands for what the code that builds an agent holds on its stack, such as the reply's event and run state."""


def test_a_validated_entrypoint_keeps_nothing_of_the_code_that_wrapped_it_alive() -> None:
    """The wrapper lives as long as the tool it validates, so it must not hold the frames that built it."""

    def build() -> tuple[Callable[..., Any], weakref.ref[_CallerState]]:
        state = _CallerState()
        return validated_entrypoint(_takes_a_count), weakref.ref(state)

    validated, state = build()
    gc.collect()

    assert state() is None
    assert validated("3") == "3"


def test_an_annotation_that_does_not_resolve_raises() -> None:
    """A schema cannot be derived from a name that does not exist."""

    def broken(query: Missing) -> str:  # noqa: F821 # ty: ignore[unresolved-reference]
        return str(query)

    with pytest.raises(NameError, match="Missing"):
        tool_parameters(broken)


def test_optional_piped_union_renders_identically_to_typing_optional() -> None:
    """Optional ``X | None`` and ``None | X`` drop ``null`` and match ``Optional[X]``."""

    def with_piped(x: str | None = None) -> str:
        return x or ""

    def with_reversed_piped(x: None | str = None) -> str:
        return x or ""

    def with_typing_optional(x: Optional[str] = None) -> str:  # noqa: UP045
        return x or ""

    expected = {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}
    assert_same_bytes(tool_parameters(with_piped), expected)
    assert_same_bytes(tool_parameters(with_reversed_piped), expected)
    assert_same_bytes(tool_parameters(with_typing_optional), expected)


def test_required_nullable_parameters_keep_null_alternative() -> None:
    """Required nullable parameters without a default keep ``anyOf: [X, null]`` so callers can pass ``null``."""

    def required_nullable(
        piped: str | None,
        typing_opt: Optional[str],  # noqa: UP045
        typing_union: Union[int, None],  # noqa: UP007
    ) -> str:
        return ""

    assert_same_bytes(
        tool_parameters(required_nullable),
        {
            "type": "object",
            "properties": {
                "piped": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "typing_opt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "typing_union": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            },
            "required": ["piped", "typing_opt", "typing_union"],
        },
    )


def test_multi_type_nullable_union_keeps_any_of() -> None:
    """Unions with three or more members keep ``anyOf`` even when the parameter is optional."""

    def multi_union(x: str | int | None = None) -> str:
        return ""

    assert_same_bytes(
        tool_parameters(multi_union),
        {
            "type": "object",
            "properties": {
                "x": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
            },
            "required": [],
        },
    )


def test_non_none_default_nullable_parameter_keeps_any_of_including_run_sql_query() -> None:
    """Nullable parameters with a non-``None`` default keep ``anyOf: [X, null]`` because ``null`` differs from omitted."""

    def query(
        limit: int | None = 10,
        legacy: Optional[int] = 10,  # noqa: UP045
        note: str | None = None,
    ) -> str:
        return ""

    assert_same_bytes(
        tool_parameters(query),
        {
            "type": "object",
            "properties": {
                "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "legacy": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "note": {"type": "string"},
            },
            "required": [],
        },
    )

    sql_function = toolkit_offers(sql_tools()(db_url="sqlite://"), "sql_run_query")
    assert sql_function.parameters["properties"]["limit"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert "limit" not in sql_function.parameters["required"]
    validate_tool_arguments(dict(sql_function.parameters), {"query": "SELECT 1", "limit": None})
    validate_tool_arguments(sql_function, {"query": "SELECT 1", "limit": None})


def test_validated_entrypoint_still_accepts_explicit_none_for_plain_optional_schema() -> None:
    """Runtime validation still accepts explicit ``None`` when the JSON schema omits ``null``."""

    def lookup(x: str | None = None, limit: int | None = 10) -> tuple[str | None, int | None]:
        return x, limit

    assert tool_parameters(lookup) == {
        "type": "object",
        "properties": {
            "x": {"type": "string"},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        },
        "required": [],
    }
    validated = validated_entrypoint(lookup)
    assert validated() == (None, 10)
    assert validated(x=None, limit=None) == (None, None)
    assert validated(x="ok", limit=5) == ("ok", 5)


def test_injected_background_after_and_mindroom_output_path_keep_nullable_any_of(tmp_path: Path) -> None:
    """ISSUE-211: injected ``background_after`` and ``mindroom_output_path`` keep ``anyOf: [..., null]``."""

    def fetch(query: str, tag: str | None = None) -> str:
        return query

    wrapped = with_output_files(
        adapt_callable(fetch),
        ToolOutputFilePolicy(workspace_root=tmp_path),
    )
    projected = project_definitions([wrapped.definition()], {"fetch": 30.0})[0]

    assert projected.parameters["properties"]["tag"] == {"type": "string"}
    assert projected.parameters["properties"][OUTPUT_PATH_ARGUMENT] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
    assert projected.parameters["properties"][BACKGROUND_AFTER] == background_after_schema(30.0)
    assert projected.parameters["properties"][BACKGROUND_AFTER]["anyOf"] == [
        {"type": "number", "minimum": 0},
        {"type": "null"},
    ]


def owned_probe_functions() -> dict[str, Any]:
    """Adapt the argument probes as one toolkit, keyed by function name."""
    toolkit = Toolkit(name="probes", tools=list(probes.PROBES))
    return {function.name: function for function in adapt_toolkit(toolkit).functions}


_OWNED_CASES = sorted(ARGUMENTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", _OWNED_CASES)
async def test_argument_case_reaches_an_owned_function_as_the_golden_records(case_id: str) -> None:
    """The owned coercion gives each probe body what Agno's coercion gave it, and fails with the same text."""
    outcome = await support.argument_outcome(ARGUMENTS[case_id], owned_probe_functions())
    assert json.loads(json.dumps(outcome)) == ARGUMENTS[case_id]
