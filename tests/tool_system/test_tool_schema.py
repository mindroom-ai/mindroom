"""The owned schema generator derives what Agno derives, and the owned coercion gives a tool body what Agno's gave.

Every MindRoom-authored function whose schema Agno derives from its signature runs through both generators, for each
registered tool as the goldens build it, and the two outputs must serialize to the same bytes. The annotation-kind
cases do the same for each kind of annotation and docstring a tool may use, and every argument golden whose probe
takes no framework object runs through owned toolkit functions.
"""

# The annotation-kind probes exist for their signatures, which no body reads.
# ruff: noqa: ARG001, ARG002

from __future__ import annotations

import gc
import inspect
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

from mindroom.tool_system.agno_toolkits import AGNO_EMPTY_PARAMETERS, adapt_toolkit
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.tool_schema import tool_description, tool_parameters, validated_entrypoint
from tests.agno_parity import tool_argument_probes as probes
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning:daytona"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning:openbb_"),
]

SCHEMAS = json.loads(support.SCHEMAS_PATH.read_text(encoding="utf-8"))
ALL_ENABLED_SCHEMAS = json.loads(support.ALL_ENABLED_SCHEMAS_PATH.read_text(encoding="utf-8"))
ARGUMENTS = json.loads(support.ARGUMENTS_PATH.read_text(encoding="utf-8"))
_BUILDS = [
    *((tool_name, False) for tool_name in sorted(SCHEMAS)),
    *((tool_name, True) for tool_name in sorted(ALL_ENABLED_SCHEMAS)),
]
# MindRoom-authored functions Agno hands an agent or run context, which an owned function reads through current_call().
# The claude_agent ones move to current_call() when MindRoom's tools move to the owned API; the others override vendor
# toolkits' methods and stay Agno functions.
_FRAMEWORK_OBJECT_PARAMETERS = frozenset(
    {"agent", "team", "run_context", "_agno_agent", "_agno_team", "_agno_run_context"},
)
_TAKE_FRAMEWORK_OBJECTS = {
    "claude_agent": {
        "claude_end_session",
        "claude_interrupt",
        "claude_send",
        "claude_session_status",
        "claude_start_session",
    },
    "e2b": {"download_chart_data", "download_png_result"},
    "fal": {"generate_media", "image_to_image"},
    "gemini": {"generate_image"},
    "replicate": {"generate_media"},
}


def owned_derivation(entrypoint: Callable[..., Any]) -> tuple[str | None, dict[str, Any]]:
    """Return the description and parameters the owned generator derives for ``entrypoint``."""
    return tool_description(entrypoint), tool_parameters(entrypoint)


def assert_same_bytes(owned: object, agno: object) -> None:
    """Compare serialized bytes, key order included, because providers receive schemas serialized."""
    assert json.dumps(owned, indent=2) == json.dumps(agno, indent=2)


@pytest.fixture(scope="module")
def golden_runtime(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Config, RuntimePaths]:
    """Build the golden runtime once per worker."""
    root = tmp_path_factory.mktemp("tool-schema").resolve()
    with support.isolated_process(root):
        config, runtime_paths = support.golden_runtime(root)
    return root, config, runtime_paths


def _mindroom_authored(entrypoint: Callable[..., Any]) -> bool:
    source = inspect.unwrap(tool_schema_source(inspect.unwrap(entrypoint)))
    return source.__module__.startswith("mindroom.")


@pytest.mark.parametrize(("tool_name", "all_enabled"), _BUILDS)
def test_every_mindroom_authored_function_derives_agnos_schema(
    tool_name: str,
    all_enabled: bool,
    golden_runtime: tuple[Path, Config, RuntimePaths],
) -> None:
    """Each function of a registered tool, as the goldens build it, that Agno derives from MindRoom's own code."""
    root, config, runtime_paths = golden_runtime
    with support.isolated_process(root):
        if (missing := support.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        overrides = support.all_enabled_overrides(tool_name) if all_enabled else None
        toolkit = support.build_toolkit(tool_name, config, runtime_paths, overrides)
    derived = {
        name: function.entrypoint
        for name, function in toolkit.get_async_functions().items()
        if function.entrypoint is not None
        and _mindroom_authored(function.entrypoint)
        and not function.skip_entrypoint_processing
        and function.parameters == AGNO_EMPTY_PARAMETERS
    }
    taking = {
        name
        for name, entrypoint in derived.items()
        if _FRAMEWORK_OBJECT_PARAMETERS.intersection(inspect.signature(entrypoint).parameters)
    }
    assert taking <= _TAKE_FRAMEWORK_OBJECTS.get(tool_name, set())
    for name, entrypoint in derived.items():
        if name not in taking:
            assert_same_bytes(owned_derivation(entrypoint), support.agno_derivation(entrypoint))


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
