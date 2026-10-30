"""MindRoom's tool authoring API: ``Toolkit``, ``@tool``, ``current_call()``, and ``toolkit_functions()``.

A toolkit groups the functions an agent is offered under one name, with instructions the prompt shows while the agent
offers any of them. Each function's model-facing description and parameter schema come from its docstring and
annotations (``tool_schema``), and every parameter is the model's to fill: a function reads the call it runs for
through ``current_call()`` instead of taking it as a parameter.
The module imports no Agno code, so plugin code that authors tools through ``mindroom.plugin_api`` loads none.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, overload

from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

_TOOL_MARK = "__mindroom_tool__"


@dataclass(frozen=True, slots=True)
class _ToolMark:
    """What ``@tool`` or ``passthrough_tool`` declares about one function."""

    name: str | None
    description: str | None
    # The schema ``passthrough_tool`` gave a function that takes the model's arguments as sent.
    parameters: Mapping[str, Any] | None = None


@overload
def tool[F](function: F, /) -> F: ...


@overload
def tool[F](*, name: str | None = None, description: str | None = None) -> Callable[[F], F]: ...


def tool[F](
    function: F | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
) -> F | Callable[[F], F]:
    """Mark a method as one of its toolkit's functions and return it unchanged, so it stays directly callable.

    ``name`` replaces the function's own name for the model, and ``description`` its docstring's summary and body.
    A static or class method may be marked before or after it becomes one.
    """

    def mark(marked: F) -> F:
        setattr(_function_of(marked), _TOOL_MARK, _ToolMark(name=name, description=description))
        return marked

    return mark if function is None else mark(function)


def passthrough_tool[F](
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, Any],
) -> Callable[[F], F]:
    """Mark a function the model calls with the schema ``parameters`` and that takes the model's arguments as sent.

    Its arguments are not validated against its annotations, as an MCP server's tools take theirs, so its body checks
    them. ``name`` replaces the function's own name, and ``description`` is what the model reads of it.
    """

    def mark(marked: F) -> F:
        setattr(_function_of(marked), _TOOL_MARK, _ToolMark(name=name, description=description, parameters=parameters))
        return marked

    return mark


def _tool_name(function: Callable[..., Any]) -> str:
    """Return the name the model calls ``function`` by: the one ``@tool`` gave it, else its own."""
    mark = _mark(function)
    if mark is not None and mark.name is not None:
        return mark.name
    # A tool is a function or a method (``_require_tool_shape``), so it has a name of its own.
    return function.__name__  # ty: ignore[unresolved-attribute]


def _require_tool_shape(toolkit: str, function: object) -> None:
    """Refuse what cannot be a tool function, raising ``ValueError``.

    A callable other than a function or method has no signature and annotations of its own to derive a schema and
    validation from, and the model passes every argument by name, so a parameter taken only by position is refused too.
    """
    if not (inspect.isfunction(function) or inspect.ismethod(function)):
        msg = f"Toolkit {toolkit!r} takes functions and methods as tools, not {function!r}"
        raise ValueError(msg)
    parameters = inspect.signature(function).parameters.values()
    if positional := [parameter.name for parameter in parameters if parameter.kind is parameter.POSITIONAL_ONLY]:
        msg = (
            f"Tool function {_tool_name(function)!r} takes {', '.join(positional)} only by position, but the model"
            " passes every argument by name"
        )
        raise ValueError(msg)


def authored_description(function: Callable[..., Any]) -> str | None:
    """Return the description ``@tool`` gave ``function``; None leaves its docstring to describe it."""
    mark = _mark(function)
    return None if mark is None else mark.description


def passthrough_parameters(function: Callable[..., Any]) -> Mapping[str, Any] | None:
    """Return the schema ``passthrough_tool`` gave ``function``; None for a function whose signature derives one."""
    mark = _mark(function)
    return None if mark is None else mark.parameters


def _mark(function: object) -> _ToolMark | None:
    mark = getattr(function, _TOOL_MARK, None)
    return mark if isinstance(mark, _ToolMark) else None


def _function_of(member: object) -> object:
    """Return the function a static or class method wraps, which the mark lives on; any other member as it is."""
    return member.__func__ if isinstance(member, staticmethod | classmethod) else member


class Toolkit:
    """A named group of tool functions, with instructions the prompt shows while the agent offers any of them.

    A subclass sets ``name`` and ``instructions`` as class attributes or passes them to the constructor. Its
    functions are its ``@tool`` methods, in the order the class defines them, then the callables passed as ``tools``;
    ``functions`` holds them by the name the model calls each by, as definitions: calling one there skips the
    argument validation, output files, and worker routing a call gets through ``toolkit_functions``. An override
    without ``@tool`` is no function, so a subclass stops offering an inherited one by overriding it plainly. Each
    function's annotations, its return annotation included, must resolve when an agent is built with the toolkit, so
    the types they name are imported at runtime.
    """

    name: str = "toolkit"
    instructions: str | None = None

    def __init__(
        self,
        *,
        name: str | None = None,
        tools: Sequence[Callable[..., Any]] = (),
        instructions: str | None = None,
    ) -> None:
        """Collect the toolkit's functions, raising ``ValueError`` for a tool that cannot be one.

        Two different functions under one name raise, as does a tool that is no function or method or that takes a
        parameter only by position.
        """
        if name is not None:
            self.name = name
        if instructions is not None:
            self.instructions = instructions
        cls = type(self)
        attributes = dict.fromkeys(attribute for base in reversed(cls.__mro__) for attribute in vars(base))
        methods = [
            getattr(self, attribute)
            for attribute in attributes
            if _mark(_function_of(inspect.getattr_static(cls, attribute))) is not None
        ]
        self.functions: dict[str, Callable[..., Any]] = {}
        for function in (*methods, *tools):
            _require_tool_shape(self.name, function)
            function_name = _tool_name(function)
            if self.functions.setdefault(function_name, function) != function:
                msg = f"Toolkit {self.name!r} has two functions named {function_name!r}"
                raise ValueError(msg)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Return ``function``, one of this toolkit's as its definition derives it, as this instance offers it.

        A subclass changes what its instance decides, such as a description that names its configuration or a
        function that requires confirmation.
        """
        return function


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentCall:
    """The tool call a function runs for.

    ``call_id`` is the provider's id of the call when a model made it. ``toolkit`` is the authored toolkit the agent
    was offered the function from; None for a function generated for the agent. ``session_state`` is the session's
    state, which the function may change in place and MindRoom stores after the run. ``history_scope`` names a team's
    stored conversation for a call of a team run. ``runtime`` is the context ``get_tool_runtime_context()`` returns,
    with the room, the requester, and the model the call runs for.
    """

    call_id: str | None
    toolkit: str | None
    session_id: str
    session_state: dict[str, Any]
    history_scope: HistoryScope | None
    runtime: ToolRuntimeContext | None


def current_call() -> CurrentCall | None:
    """Return the tool call this task is running; None outside one, such as when code calls a tool method directly."""
    # Deferred because invocation loads Agno for vendor toolkits, which plugin code importing this module must not;
    # any code running inside a tool call has loaded it already.
    from mindroom.tool_system.invocation import current_tool_call  # noqa: PLC0415

    call = current_tool_call()
    if call is None:
        return None
    return CurrentCall(
        call_id=call.context.call_id,
        toolkit=call.function.toolkit,
        session_id=call.context.session_id,
        session_state=call.context.session_state,
        history_scope=call.context.history_scope,
        runtime=get_tool_runtime_context(),
    )


def toolkit_functions(toolkit: AnyToolkit) -> dict[str, Callable[..., Any]]:
    """Return the callables of ``toolkit``'s functions by name, one per name, as an agent's run calls them.

    Each validates its arguments; for a toolkit ``get_tool_by_name`` built, each also handles ``mindroom_output_path``
    and sends a call routed to a worker there. For an Agno toolkit, a function's async variant wins where it has one.
    A call through one inside an async tool shares that call's ownership of the synchronous work it starts, so the
    tool may call them any number of times, at once too; a tool that calls them must be async.
    """
    # Deferred because the adapter loads Agno for vendor toolkits, which plugin code importing this module must not;
    # code calling a toolkit's functions runs inside MindRoom, which has loaded it already.
    from mindroom.tool_system.agno_toolkits import adapt_toolkit  # noqa: PLC0415

    adapted = adapt_toolkit(toolkit)
    return {function.name: function.entrypoint for function in adapted.functions}


__all__ = [
    "CurrentCall",
    "Toolkit",
    "authored_description",
    "current_call",
    "passthrough_parameters",
    "passthrough_tool",
    "tool",
    "toolkit_functions",
]
