"""MindRoom's tool authoring API: a toolkit's functions, their adaptation without Agno, and the call each runs for."""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator  # noqa: TC003 - A tool's annotations resolve when its toolkit adapts.
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.tool_system.agno_toolkits import (
    Function,
    adapt_toolkit,
    configure_toolkit_functions,
    requires_connect,
    toolkit_function,
    toolkit_function_names,
)
from mindroom.tool_system.agno_toolkits import Toolkit as AgnoToolkit
from mindroom.tool_system.authoring import (
    CurrentCall,
    Toolkit,
    current_call,
    passthrough_tool,
    tool,
    toolkit_functions,
)
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT, ToolOutputFilePolicy
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.tool_schema import closed_parameters
from tests.conftest import restore_builtin_tools, snapshot_builtin_tools
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction


class GreeterTools(Toolkit):
    """A toolkit that names itself and its instructions as class attributes."""

    name = "greeter"
    instructions = "Greet people by the name they gave."

    @tool
    async def greet(self, name: str, times: int = 1) -> str:
        """Greet someone by name.

        Args:
            name: The person's name.
            times: How often to greet.

        """
        return " ".join([f"Hello, {name}!"] * times)

    @tool(name="farewell", description="Say goodbye to someone.")
    def bye(self, name: str) -> str:
        """Part with someone."""
        return f"Goodbye, {name}."

    def helper(self) -> str:
        """Help the toolkit, which offers it to no model."""
        return "not offered"


def shout(text: str) -> str:
    """Shout the text.

    Args:
        text: What to shout.

    """
    return text.upper()


_CONTEXT = ToolCallContext(run_id="run-1", session_id="session-1", user_id=None)


def _by_name(functions: tuple[ToolFunction, ...]) -> dict[str, ToolFunction]:
    return {function.name: function for function in functions}


def test_a_toolkits_functions_are_its_tool_methods_then_its_tools() -> None:
    """``@tool`` methods come in definition order under the names the model calls them by, then ``tools``."""
    toolkit = GreeterTools(tools=[shout])

    assert list(toolkit.functions) == ["greet", "farewell", "shout"]
    assert toolkit.functions["farewell"] == toolkit.bye
    assert toolkit.functions["shout"] is shout
    assert (toolkit.name, toolkit.instructions) == ("greeter", "Greet people by the name they gave.")


@pytest.mark.asyncio
async def test_tool_methods_stay_directly_callable() -> None:
    """``@tool`` returns the method itself, so code calls it as written."""
    toolkit = GreeterTools()

    assert await toolkit.greet("Ada", times=2) == "Hello, Ada! Hello, Ada!"
    assert toolkit.bye("Ada") == "Goodbye, Ada."


def test_a_subclass_passes_agnos_constructor_keywords() -> None:
    """A toolkit written for Agno's constructor keeps its ``name``, ``tools``, and ``instructions`` keywords."""

    class LookupTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="lookup", tools=[self.find], instructions="Look things up.")

        def find(self, query: str) -> str:
            return query

    toolkit = LookupTools()

    assert (toolkit.name, toolkit.instructions, list(toolkit.functions)) == ("lookup", "Look things up.", ["find"])


def test_a_subclass_offers_its_bases_tool_methods_first() -> None:
    """Inherited ``@tool`` methods keep the base's order, a marked override keeps its place, and new ones follow."""

    class BaseTools(Toolkit):
        @tool
        def first(self) -> str:
            return "first"

        @tool
        def second(self) -> str:
            return "second"

    class SubTools(BaseTools):
        @tool
        def third(self) -> str:
            return "third"

        @tool
        def second(self) -> str:
            return "overridden"

    toolkit = SubTools()

    assert list(toolkit.functions) == ["first", "second", "third"]
    assert toolkit.functions["second"]() == "overridden"


class StaticAndClassTools(Toolkit):
    """Static and class methods marked with ``@tool`` in either decorator order."""

    @staticmethod
    @tool(name="static_marked_first", description="A static method marked before it became one.")
    def _static_inside(text: str) -> str:
        return f"static:{text}"

    @tool(name="static_marked_last", description="A static method marked after it became one.")
    @staticmethod
    def _static_outside(text: str) -> str:
        return f"static:{text}"

    @classmethod
    @tool(name="class_marked_first", description="A class method marked before it became one.")
    def _class_inside(cls, text: str) -> str:
        return f"{cls.__name__}:{text}"

    @tool(name="class_marked_last", description="A class method marked after it became one.")
    @classmethod
    def _class_outside(cls, text: str) -> str:
        return f"{cls.__name__}:{text}"


@pytest.mark.asyncio
async def test_static_and_class_methods_are_tool_functions_in_either_decorator_order() -> None:
    """Each keeps the name and description ``@tool`` gave it, and the model fills only its own parameters."""
    toolkit = StaticAndClassTools()
    functions = _by_name(adapt_toolkit(toolkit).functions)
    names = ["static_marked_first", "static_marked_last", "class_marked_first", "class_marked_last"]

    outcomes = [await execute_tool_call(functions[name], {"text": "hi"}, _CONTEXT, middleware=()) for name in names]

    assert list(toolkit.functions) == names
    assert [functions[name].description for name in names] == [
        "A static method marked before it became one.",
        "A static method marked after it became one.",
        "A class method marked before it became one.",
        "A class method marked after it became one.",
    ]
    assert all(
        functions[name].parameters
        == {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        for name in names
    )
    assert [outcome.content for outcome in outcomes] == [
        "static:hi",
        "static:hi",
        "StaticAndClassTools:hi",
        "StaticAndClassTools:hi",
    ]


def test_only_tool_marks_make_a_class_attribute_a_function() -> None:
    """An attribute that answers any attribute lookup, such as a lazy client proxy, is no tool function."""

    class AnswersEverything:
        def __getattr__(self, name: str) -> str:
            return name

    class ProxyHoldingTools(Toolkit):
        client = AnswersEverything()

        @tool
        def ping(self) -> str:
            return "pong"

    assert list(ProxyHoldingTools().functions) == ["ping"]


class _Callable:
    """An object that is callable without being a function, which no schema can be derived from."""

    def __call__(self, text: str) -> str:
        return text


@pytest.mark.parametrize("entry", [functools.partial(shout, "x"), _Callable()], ids=["partial", "callable_object"])
def test_a_tool_that_is_no_function_or_method_is_refused_when_the_toolkit_is_built(entry: Callable[..., Any]) -> None:
    """A schema and validation come from a function's own signature and annotations, so other callables cannot be tools."""
    with pytest.raises(ValueError, match="takes functions and methods as tools"):
        Toolkit(name="odd", tools=[entry])


def test_a_tool_with_a_positional_only_parameter_is_refused_when_the_toolkit_is_built() -> None:
    """The model passes every argument by name, so a parameter it could only pass by position is refused."""

    class LookupTools(Toolkit):
        @tool
        def lookup(self, key: str, /) -> str:
            return key

    with pytest.raises(ValueError, match="'lookup' takes key only by position"):
        LookupTools()


def test_an_override_without_tool_stops_offering_the_inherited_function() -> None:
    """A subclass stops offering an inherited function by overriding it without ``@tool``."""

    class BaseTools(Toolkit):
        @tool
        def offered(self) -> str:
            return "offered"

        @tool
        def withdrawn(self) -> str:
            return "withdrawn"

    class SubTools(BaseTools):
        def withdrawn(self) -> str:
            return "a helper now"

    assert list(SubTools().functions) == ["offered"]


def test_two_functions_under_one_name_raise() -> None:
    """The model calls a function by its name, so a toolkit offers one function per name."""

    def farewell(name: str) -> str:
        return name

    with pytest.raises(ValueError, match="two functions named 'farewell'"):
        GreeterTools(tools=[farewell])


def test_an_owned_toolkit_adapts_without_agnos_entrypoint_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schemas come from the owned generator, descriptions from ``@tool`` or the docstring, and nothing is injected."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "An owned toolkit must not reach Agno's entrypoint processing"
        raise AssertionError(msg)

    monkeypatch.setattr(Function, "process_entrypoint", refuse)

    adapted = adapt_toolkit(GreeterTools(tools=[shout]), key="greeter")
    functions = _by_name(adapted.functions)

    # Agno's async-run order: the synchronous functions, then the asynchronous ones.
    assert list(functions) == ["farewell", "shout", "greet"]
    instructions = "Greet people by the name they gave."
    assert (adapted.name, adapted.instructions, adapted.guidance) == ("greeter", instructions, instructions)
    assert functions["farewell"].description == "Say goodbye to someone."
    assert functions["shout"].description == "Shout the text."
    assert functions["greet"].parameters == {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The person's name."},
            "times": {"type": "integer", "description": "How often to greet."},
        },
        "required": ["name"],
    }
    assert all(function.injected_params == {} and function.entrypoint_processed for function in adapted.functions)
    assert {function.toolkit for function in adapted.functions} == {"greeter"}


@pytest.mark.asyncio
async def test_an_owned_function_coerces_and_validates_its_arguments() -> None:
    """Arguments reach the body coerced to its annotations, and one that cannot be coerced fails the call."""
    greet = _by_name(adapt_toolkit(GreeterTools()).functions)["greet"]

    coerced = await execute_tool_call(greet, {"name": "Ada", "times": "2"}, _CONTEXT, middleware=())
    invalid = await execute_tool_call(greet, {"name": "Ada", "times": "twice"}, _CONTEXT, middleware=())

    assert (coerced.content, coerced.is_error) == ("Hello, Ada! Hello, Ada!", False)
    assert invalid.is_error
    assert "1 validation error for GreeterTools.greet\ntimes\n  Input should be a valid integer" in invalid.content


@pytest.mark.asyncio
async def test_an_owned_streaming_function_stays_an_async_generator() -> None:
    """A streaming body keeps running on the event loop, with its arguments coerced, and its chunks join."""

    class CounterTools(Toolkit):
        @tool
        async def count_up(self, limit: int) -> AsyncIterator[str]:
            """Count up to a limit."""
            for number in range(1, limit + 1):
                yield str(number)

    [count_up] = adapt_toolkit(CounterTools()).functions

    outcome = await execute_tool_call(count_up, {"limit": "3"}, _CONTEXT, middleware=())

    assert inspect.isasyncgenfunction(count_up.entrypoint)
    assert (outcome.content, outcome.is_error) == ("123", False)


def test_an_owned_function_that_takes_an_agno_framework_object_is_refused() -> None:
    """Nothing hands an owned function its agent or run context, so a parameter expecting one is refused."""

    class SpoofableTools(Toolkit):
        @tool
        def whoami(self, query: str, run_context: object = None) -> str:
            return f"{query}{run_context}"

    with pytest.raises(ValueError, match=r"'whoami' takes run_context.*current_call\(\)"):
        adapt_toolkit(SpoofableTools())


@pytest.mark.asyncio
async def test_an_owned_functions_media_named_parameter_is_the_models() -> None:
    """Only Agno hands a tool the run's media by name; an owned function's ``files`` is an argument like any other."""

    class AttachTools(Toolkit):
        @tool
        def attach(self, files: list[str]) -> str:
            """Attach files.

            Args:
                files: The file names.

            """
            return ",".join(files)

    [attach] = adapt_toolkit(AttachTools()).functions

    outcome = await execute_tool_call(attach, {"files": ["a.txt", "b.txt"]}, _CONTEXT, middleware=())

    assert attach.injected_params == {}
    assert attach.parameters["required"] == ["files"]
    assert outcome.content == "a.txt,b.txt"


@pytest.mark.asyncio
async def test_current_call_names_the_call_a_function_runs_for() -> None:
    """Asynchronous and synchronous bodies read the call's id, toolkit, session, and runtime context."""
    seen: list[CurrentCall | None] = []

    class RecorderTools(Toolkit):
        @tool
        async def record_async(self) -> str:
            seen.append(current_call())
            return "recorded"

        @tool
        def record_sync(self) -> str:
            seen.append(current_call())
            return "recorded"

    adapted = adapt_toolkit(RecorderTools(), key="recorder")
    state = {"visits": 1}
    scope = HistoryScope(kind="team", scope_id="crew")
    runtime = Mock(spec=ToolRuntimeContext)
    context = replace(_CONTEXT, call_id="call-1", session_state=state, history_scope=scope)
    with tool_runtime_context(runtime):
        outcomes = [await execute_tool_call(function, {}, context, middleware=()) for function in adapted.functions]

    expected = CurrentCall(
        call_id="call-1",
        toolkit="recorder",
        session_id="session-1",
        session_state=state,
        history_scope=scope,
        runtime=runtime,
    )
    assert [outcome.content for outcome in outcomes] == ["recorded", "recorded"]
    assert seen == [expected, expected]
    assert all(call is not None and call.session_state is state for call in seen)
    assert current_call() is None


def test_toolkit_functions_returns_one_callable_per_name() -> None:
    """Each function a run offers, by name; of an Agno toolkit, its async variant where it has one."""

    def lookup() -> str:
        return "sync"

    async def alookup() -> str:
        return "async"

    owned = GreeterTools(tools=[shout])
    agno = AgnoToolkit(name="dual", tools=[lookup])
    agno.async_functions["lookup"] = Function(name="lookup", entrypoint=alookup)

    owned_functions = toolkit_functions(owned)
    agno_functions = toolkit_functions(agno)

    # The callables validate their arguments, so each wraps the function it calls.
    assert {name: inspect.unwrap(function) for name, function in owned_functions.items()} == {
        "farewell": owned.bye,
        "shout": shout,
        "greet": owned.greet,
    }
    assert {name: inspect.unwrap(function) for name, function in agno_functions.items()} == {"lookup": alookup}


def test_an_owned_toolkit_adapts_to_what_its_agno_authored_twin_adapts_to() -> None:
    """The same methods give the same functions, flags, and instructions whichever authoring API declares them."""

    class OwnedTwinTools(Toolkit):
        name = "twin"
        instructions = "Count and shout words."

        @tool
        def count_words(self, text: str, limit: int | None = None) -> str:
            """Count the words of a text.

            Args:
                text: The text.
                limit: The most words to count.

            """
            return str(len(text.split()[:limit]))

        @tool
        async def shout_text(self, text: str) -> str:
            """Shout a text.

            Args:
                text: The text.

            """
            return text.upper()

    owned = OwnedTwinTools()
    agno = AgnoToolkit(name="twin", tools=[owned.count_words, owned.shout_text], instructions="Count and shout words.")

    assert support.golden_entry(adapt_toolkit(owned)) == support.golden_entry(adapt_toolkit(agno))


class _CountShoutTools(Toolkit):
    """An owned toolkit registered as a tool for ``get_tool_by_name`` to build."""

    name = "twin"
    instructions = "Count and shout words."

    @tool
    def count_words(self, text: str, limit: int | None = None) -> str:
        """Count the words of a text.

        Args:
            text: The text.
            limit: The most words to count.

        """
        return str(len(text.split()[:limit]))

    @tool
    def shout_text(self, text: str) -> str:
        """Shout a text.

        Args:
            text: The text.

        """
        return text.upper()


class _AgnoCountShoutTools(AgnoToolkit):
    """The Agno-authored twin of ``_CountShoutTools``."""

    def __init__(self) -> None:
        owned = _CountShoutTools()
        super().__init__(name="twin", tools=[owned.count_words, owned.shout_text], instructions=owned.instructions)


@pytest.fixture
def registered_twins(tmp_path: Path) -> Iterator[RuntimePaths]:
    """Register the owned toolkit and its Agno twin as tools, and return the runtime that builds them."""
    snapshot = snapshot_builtin_tools()
    for tool_name, toolkit_class in (("owned_twin", _CountShoutTools), ("agno_twin", _AgnoCountShoutTools)):
        register_tool_with_metadata(
            name=tool_name,
            file_access=ToolFileAccess.NONE,
            display_name=tool_name,
            description="Test-only toolkit for owned authoring.",
            category=ToolCategory.DEVELOPMENT,
        )(lambda toolkit_class=toolkit_class: toolkit_class)
    try:
        yield resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    finally:
        restore_builtin_tools(snapshot)


def test_a_registered_owned_toolkit_builds_with_output_files_as_its_agno_twin(
    registered_twins: RuntimePaths,
    tmp_path: Path,
) -> None:
    """``get_tool_by_name`` builds an owned toolkit with output files, and it adapts to its Agno twin's functions."""
    owned, agno = (
        adapt_toolkit(
            get_tool_by_name(
                tool_name,
                registered_twins,
                tool_output_workspace_root=tmp_path,
                worker_target=None,
                tools=builtin_tool_registry(),
            ),
        )
        for tool_name in ("owned_twin", "agno_twin")
    )

    assert support.golden_entry(owned) == support.golden_entry(agno)
    shout = _by_name(owned.functions)["shout_text"]
    assert shout.parameters["properties"]["text"] == {"type": "string", "description": "The text."}
    assert OUTPUT_PATH_ARGUMENT in shout.parameters["properties"]
    assert shout.strict is False


@pytest.mark.parametrize("tool_name", ["owned_twin", "agno_twin"])
def test_the_function_filters_leave_the_built_toolkit_and_hide_its_functions(
    registered_twins: RuntimePaths,
    tool_name: str,
) -> None:
    """An include filter offers only the functions it names, while the toolkit keeps every function it built."""
    toolkit = get_tool_by_name(
        tool_name,
        registered_twins,
        tool_config_overrides={"include_tools": ["shout_text"]},
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert toolkit_function_names(toolkit) == ("shout_text",)
    assert [function.name for function in adapt_toolkit(toolkit).functions] == ["shout_text"]
    assert toolkit_function(toolkit, "count_words") is None
    assert list(toolkit_functions(toolkit)) == ["shout_text"]
    with pytest.raises(ValueError, match="Included tool"):
        get_tool_by_name(
            tool_name,
            registered_twins,
            tool_config_overrides={"include_tools": ["missing"]},
            worker_target=None,
            tools=builtin_tool_registry(),
        )


@pytest.mark.asyncio
async def test_a_registered_owned_function_writes_its_result_where_the_model_asks(
    registered_twins: RuntimePaths,
    tmp_path: Path,
) -> None:
    """The output files an owned toolkit is built with save a call's result under the workspace."""
    toolkit = get_tool_by_name(
        "owned_twin",
        registered_twins,
        tool_output_workspace_root=tmp_path,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    function = toolkit_function(toolkit, "shout_text")
    assert function is not None

    outcome = await execute_tool_call(
        function,
        {"text": "hi", OUTPUT_PATH_ARGUMENT: "out/shout.txt"},
        _CONTEXT,
        middleware=(),
    )

    assert not outcome.is_error
    assert (tmp_path / "out" / "shout.txt").read_text(encoding="utf-8") == "HI"


_TAG_SCHEMA = {
    "type": "object",
    "properties": {"tag": {"type": "string", "description": "A tag."}},
    "required": ["tag"],
}


class _TaggerTools(Toolkit):
    """A toolkit with one function its schema defines and one its instance fits."""

    name = "tagger"

    def __init__(self, *, room: str) -> None:
        self.room = room
        super().__init__()

    @passthrough_tool(description="Tag the thread.", parameters=_TAG_SCHEMA)
    async def tag(self, **arguments: object) -> str:
        return repr(arguments)

    @tool
    def untag(self, tag: str) -> str:
        """Remove a tag.

        Args:
            tag: The tag.

        """
        return tag

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        if function.name != "untag":
            return function
        return replace(
            function,
            description=f"Remove a tag in {self.room}.",
            parameters={**function.parameters, "required": []},
            requires_confirmation=True,
        )


@pytest.mark.asyncio
async def test_a_passthrough_function_keeps_its_schema_and_takes_its_arguments_as_sent() -> None:
    """The authored schema reaches the model as written, unvalidated, and leaves the flags no option set unset."""
    tag = _by_name(adapt_toolkit(_TaggerTools(room="#lab")).functions)["tag"]

    outcome = await execute_tool_call(tag, {"tag": 7, "extra": True}, _CONTEXT, middleware=())

    assert (tag.description, tag.parameters) == ("Tag the thread.", _TAG_SCHEMA)
    assert tag.parameters is not _TAG_SCHEMA
    assert outcome.content == "{'tag': 7, 'extra': True}"
    assert not tag.entrypoint_processed
    assert (tag.requires_confirmation, tag.external_execution, tag.strict) == (False, False, None)


def test_a_toolkit_fits_a_derived_function_to_its_instance_before_the_output_path_joins(tmp_path: Path) -> None:
    """``offered_function`` replaces what its instance decides, and the output path joins the schema it returns."""
    toolkit = configure_toolkit_functions(
        _TaggerTools(room="#lab"),
        output_files=ToolOutputFilePolicy(workspace_root=tmp_path),
    )

    untag = _by_name(adapt_toolkit(toolkit).functions)["untag"]

    assert untag.description == "Remove a tag in #lab."
    assert untag.requires_confirmation is True
    assert list(untag.parameters["properties"]) == ["tag", OUTPUT_PATH_ARGUMENT]
    assert untag.parameters["required"] == []
    assert untag.parameters["properties"][OUTPUT_PATH_ARGUMENT]["default"] is None
    assert untag.strict is False


def test_an_authored_schema_requires_the_arguments_without_a_default_and_admits_no_other() -> None:
    """A toolkit's schema in place of the derived one is completed the way Agno completes one it validates."""

    def lookup(city: str, country: str = "NL", *args: object, **kwargs: object) -> str:
        return city + country + str(args) + str(kwargs)

    parameters = closed_parameters(
        lookup,
        {"type": "object", "properties": {"city": {"type": "string"}, "country": {"type": "string"}}},
    )

    assert parameters == {
        "type": "object",
        "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }


def test_only_an_agno_toolkit_holds_a_connection_for_its_run() -> None:
    """An owned toolkit never connects; an Agno toolkit connects when it says so."""
    connecting = AgnoToolkit(name="db")
    connecting._requires_connect = True

    assert not requires_connect(GreeterTools())
    assert not requires_connect(AgnoToolkit(name="plain"))
    assert requires_connect(connecting)


def _connecting(toolkit_class: type[AgnoToolkit] = AgnoToolkit) -> AgnoToolkit:
    toolkit = toolkit_class(name="db")
    toolkit._requires_connect = True
    return toolkit


class _PlacingToolkit(AgnoToolkit):
    """Keeps every call of its functions on the primary, as the browser keeps its desktop calls."""

    def runs_on_primary(self, function_name: str, arguments: dict[str, object]) -> bool:  # noqa: ARG002
        return True


def test_a_toolkit_routed_to_a_worker_connects_only_where_some_of_its_calls_run() -> None:
    """A worker-routed toolkit connects in the worker alone, unless it keeps some calls on the primary too."""

    def to_worker(_name: str, entrypoint: Callable[..., object]) -> Callable[..., object]:
        return entrypoint

    assert not requires_connect(configure_toolkit_functions(_connecting(), to_worker=to_worker))
    assert requires_connect(configure_toolkit_functions(_connecting(), offered=()))
    assert requires_connect(configure_toolkit_functions(_connecting(_PlacingToolkit), to_worker=to_worker))
