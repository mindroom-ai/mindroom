"""MindRoom's tool authoring API: ``Toolkit``, ``@tool``, ``current_call()``, and ``toolkit_functions()``.

A toolkit groups the functions an agent is offered under one name, with instructions the prompt shows while the agent
offers any of them. Each function's model-facing description and parameter schema come from its docstring and
annotations (``tool_schema``), and every parameter is the model's to fill: a function reads the call it runs for
through ``current_call()`` instead of taking it as a parameter.
The module imports no Agno code, so plugin code that authors tools through ``mindroom.plugin_api`` loads none.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_TOOL_MARK = "__mindroom_tool__"


@dataclass(frozen=True, slots=True)
class _ToolMark:
    """What ``@tool`` declares about one function."""

    name: str | None
    description: str | None


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
    ``functions`` holds them by the name the model calls each by. An override without ``@tool`` is no function, so a
    subclass stops offering an inherited one by overriding it plainly. Each function's annotations, its return annotation
    included, must resolve when an agent is built with the toolkit, so the types they name are imported at runtime.
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


class _AgnoFunction(Protocol):
    """What ``toolkit_functions`` reads of one function of an Agno toolkit."""

    @property
    def entrypoint(self) -> Callable[..., Any] | None:
        """The callable the function runs."""
        ...


class _AgnoToolkit(Protocol):
    """What ``toolkit_functions`` reads of an Agno toolkit."""

    def get_async_functions(self) -> Mapping[str, _AgnoFunction]:
        """Return the toolkit's functions by name, each one's async variant where it has one."""
        ...


def toolkit_functions(toolkit: Toolkit | _AgnoToolkit) -> dict[str, Callable[..., Any]]:
    """Return the callables of ``toolkit``'s functions by name, one per name, to call directly.

    For an Agno toolkit, such as one ``get_tool_by_name`` returns, a function's async variant wins where it has one,
    as an agent runs it. A call through an async callable is a tool call of its own, so a tool may call one any number
    of times, at once too: the synchronous body or worker request it starts belongs to it, not to the call of the tool
    that makes it, and it returns only once that work has finished, even when cancelled.
    """
    if isinstance(toolkit, Toolkit):
        functions = dict(toolkit.functions)
    else:
        functions = {
            name: function.entrypoint
            for name, function in toolkit.get_async_functions().items()
            if function.entrypoint is not None
        }
    return {name: _own_call(function) for name, function in functions.items()}


def _own_call[F: Callable[..., Any]](function: F) -> F:
    """Return ``function``, an async one run as a tool call of its own, which owns the synchronous work it starts."""
    if not inspect.iscoroutinefunction(function):
        return function

    @functools.wraps(function)
    async def own_call(*args: object, **kwargs: object) -> object:
        # Deferred: they load the tool-call policy and the job runtime, which plugin code importing this module must
        # not; any code running a tool has loaded them already.
        from mindroom.background_tasks import wait_for_future_until_complete  # noqa: PLC0415
        from mindroom.tool_jobs.control import job_owns_execution  # noqa: PLC0415
        from mindroom.tool_system.dedicated_threads import JOB_CALL_EXECUTOR  # noqa: PLC0415
        from mindroom.tool_system.tool_hooks import (  # noqa: PLC0415
            SyncToolCompletionTracker,
            track_sync_tool_completion,
        )

        # A job's call has no time limit, so what it offloads must not hold a shared pool's thread.
        tracker = SyncToolCompletionTracker(executor=JOB_CALL_EXECUTOR if job_owns_execution() else None)
        try:
            with track_sync_tool_completion(tracker):
                return await function(*args, **kwargs)
        finally:
            if (started := tracker.started_task()) is not None:
                await wait_for_future_until_complete(asyncio.gather(started, return_exceptions=True))

    return cast("F", own_call)


__all__ = ["CurrentCall", "Toolkit", "authored_description", "current_call", "tool", "toolkit_functions"]
