"""The owned schema generator renders each annotation as Agno did, and owned coercion gives a body what Agno's gave.

The generator derives every tool's schema, MindRoom's own and the Agno toolkits', which the tool goldens pin. The
annotation-kind cases run each kind of annotation and docstring a tool may use through both generators, and the two
outputs must serialize to the same bytes, so a plugin's Agno toolkit is offered what Agno offered; every argument golden
whose probe takes no framework object runs through owned toolkit functions. Values annotated ``Any`` or ``object`` are
where the two differ on purpose: the owned generator leaves them unconstrained, and the comparison renders them as
Agno's open fix does (``support.agno_derivation``).
"""

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
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.tool_schema import tool_description, tool_parameters, validated_entrypoint
from mindroom.tools.custom_api import custom_api_tools
from mindroom.tools.pandas import pandas_tools
from tests.agent_tool_helpers import toolkit_offers
from tests.agno_parity import tool_argument_probes as probes
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Callable

ARGUMENTS = json.loads(support.ARGUMENTS_PATH.read_text(encoding="utf-8"))


def owned_derivation(entrypoint: Callable[..., Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the description and parameters the owned generator derives for ``entrypoint``."""
    return tool_description(entrypoint), tool_parameters(entrypoint)


def assert_same_bytes(owned: object, agno: object) -> None:
    """Compare serialized bytes, key order included, because providers receive schemas serialized."""
    assert json.dumps(owned, indent=2) == json.dumps(agno, indent=2)


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
    typed: Optional[str] = None,  # noqa: UP045 - typing.Optional renders differently from X | None.
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

    mapping = {"type": "object", "propertyNames": {"type": "string"}, "additionalProperties": {}}
    assert tool_parameters(open_values)["properties"] == {
        "anything": {},
        "thing": {},
        "mapping": mapping,
        "objects": {"anyOf": [mapping, {"type": "null"}]},
        "rows": {"type": "array", "items": {"type": "array", "items": {}}},
    }


def test_owned_tools_taking_any_values_accept_real_arguments() -> None:
    """A thread tag's data and a Matrix event's content pass the argument validation agent CLI calls run.

    A value that is not an object is still refused there, so a tool body never receives one.
    """
    tag_thread = toolkit_offers(ThreadTagsTools(), "tag_thread")
    matrix_api = toolkit_offers(MatrixApiTools(), "matrix_api")
    event = {"action": "send_event", "event_type": "m.room.message", "content": {"msgtype": "m.text", "body": "Hi"}}

    validate_tool_arguments(dict(tag_thread.parameters), {"tag": "blocked", "data": {"priority": "high"}})
    validate_tool_arguments(dict(matrix_api.parameters), event)
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(dict(matrix_api.parameters), {**event, "content": "Hi"})


def test_vendor_tools_taking_any_values_accept_real_arguments() -> None:
    """A custom API request's JSON body and a pandas operation's own documented arguments pass argument validation.

    A value that is not an object is still refused there, so a tool body never receives one.
    """
    make_request = toolkit_offers(custom_api_tools()(), "make_request")
    operation = toolkit_offers(pandas_tools()(), "run_dataframe_operation")
    request = {
        "endpoint": "https://api.example.com/items",
        "method": "POST",
        "json_data": {"name": "lamp", "tags": ["new"], "price": 12.5},
    }

    validate_tool_arguments(dict(make_request.parameters), request)
    validate_tool_arguments(
        dict(operation.parameters),
        {"dataframe_name": "sales", "operation": "head", "operation_parameters": {"n": 5}},
    )
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(dict(make_request.parameters), {**request, "json_data": "lamp"})


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


@pytest.mark.parametrize("entrypoint", ANNOTATION_KINDS, ids=lambda entrypoint: entrypoint.__name__)
def test_each_annotation_kind_derives_agnos_schema(entrypoint: Callable[..., Any]) -> None:
    """Each kind of annotation, default, and docstring style derives the bytes Agno derives."""
    assert_same_bytes(owned_derivation(entrypoint), support.agno_derivation(entrypoint))


def test_a_bound_method_derives_agnos_schema() -> None:
    """A method's ``self`` is bound, so only its own parameters reach the schema."""

    class Holder:
        def method(self, query: str, limit: int = 5) -> str:
            """Search.

            Args:
                query: The query.
                limit: How many.

            """
            return query

    assert_same_bytes(owned_derivation(Holder().method), support.agno_derivation(Holder().method))


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


def owned_probe_functions() -> dict[str, Any]:
    """Adapt the argument probes that take no framework object as one owned toolkit, keyed by function name."""
    toolkit = Toolkit(name="probes", tools=[probe for probe in probes.PROBES if probe is not probes.probe_agent])
    return {function.name: function for function in adapt_toolkit(toolkit).functions}


_OWNED_CASES = sorted(case_id for case_id, case in ARGUMENTS.items() if case["function"] != "probe_agent")


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", _OWNED_CASES)
async def test_argument_case_reaches_an_owned_function_as_the_golden_records(case_id: str) -> None:
    """The owned coercion gives each probe body what Agno's coercion gave it, and fails with the same text."""
    outcome = await support.argument_outcome(ARGUMENTS[case_id], owned_probe_functions())
    assert json.loads(json.dumps(outcome)) == ARGUMENTS[case_id]
