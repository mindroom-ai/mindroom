"""MindRoom's tool authoring API: ``Toolkit``, ``@tool``, and ``current_call()``.

A toolkit groups the functions an agent is offered under one name, with instructions the prompt shows while the agent
offers any of them. Each function's model-facing description and parameter schema come from its docstring and
annotations (``tool_schema``), and every parameter is the model's to fill: a function reads the call it runs for
through ``current_call()`` instead of taking it as a parameter.
The module keeps its import surface lean so plugin code importing ``mindroom.plugin_api`` stays fast.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, overload

from mindroom.tool_system.context_scope import CurrentCall, current_call

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.tool_system.construction import ToolConstruction
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.toolkits import ToolkitFinishing

_TOOL_MARK = "__mindroom_tool__"


@dataclass(frozen=True, slots=True)
class ToolMark:
    """What ``@tool`` or ``passthrough_tool`` declares about one function."""

    name: str | None
    description: str | None
    # The schema ``passthrough_tool`` gave a function that takes the model's arguments as sent.
    parameters: Mapping[str, Any] | None = None
    offer_background_after: bool | None = None
    offer_output_path: bool | None = None


def _marking[F](mark: ToolMark) -> Callable[[F], F]:
    def apply(marked: F) -> F:
        setattr(_function_of(marked), _TOOL_MARK, mark)
        return marked

    return apply


@overload
def tool[F](function: F, /) -> F: ...


@overload
def tool[F](
    *,
    name: str | None = None,
    description: str | None = None,
    offer_background_after: bool | None = None,
    offer_output_path: bool | None = None,
) -> Callable[[F], F]: ...


def tool[F](
    function: F | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    offer_background_after: bool | None = None,
    offer_output_path: bool | None = None,
) -> F | Callable[[F], F]:
    """Mark a method as one of its toolkit's functions and return it unchanged, so it stays directly callable.

    ``name`` replaces the function's own name for the model, and ``description`` its docstring's summary and body.
    ``offer_background_after`` and ``offer_output_path`` override the toolkit's defaults for advertising those reserved
    parameters in the schema. A static or class method may be marked before or after it becomes one.
    """
    mark: Callable[[F], F] = _marking(
        ToolMark(
            name=name,
            description=description,
            offer_background_after=offer_background_after,
            offer_output_path=offer_output_path,
        ),
    )
    return mark if function is None else mark(function)


def passthrough_tool[F](
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, Any],
    offer_background_after: bool | None = None,
    offer_output_path: bool | None = None,
) -> Callable[[F], F]:
    """Mark a function the model calls with the schema ``parameters`` and that takes the model's arguments as sent.

    Its arguments are not validated against its annotations, as an MCP server's tools take theirs, so its body checks
    them. ``name`` replaces the function's own name, and ``description`` is what the model reads of it.
    ``offer_background_after`` and ``offer_output_path`` override the toolkit's defaults for advertising those reserved
    parameters in the schema.
    """
    return _marking(
        ToolMark(
            name=name,
            description=description,
            parameters=parameters,
            offer_background_after=offer_background_after,
            offer_output_path=offer_output_path,
        ),
    )


def _tool_name(function: Callable[..., Any]) -> str:
    """Return the name the model calls ``function`` by: the one ``@tool`` gave it, else its own."""
    mark = tool_mark(function)
    if mark is not None and mark.name is not None:
        return mark.name
    # A tool is a function or a method (``_require_tool_shape``), so it has a name of its own.
    return function.__name__  # ty: ignore[missing-attribute]


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


def tool_mark(function: object) -> ToolMark | None:
    """Return the ``@tool`` or ``passthrough_tool`` mark on ``function``, or None if unmarked."""
    mark = getattr(function, _TOOL_MARK, None)
    return mark if isinstance(mark, ToolMark) else None


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
    requires_connect: bool = False
    offer_background_after: bool = True
    offer_output_path: bool = True
    # MindRoom-owned metadata set during toolkit configuration/construction; plugin subclasses should not redefine.
    function_finishing: ToolkitFinishing | None = None
    tool_construction: ToolConstruction | None = None

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
        methods = [getattr(self, attribute) for attribute in tool_attributes(type(self))]
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

    def connect(self) -> None:
        """Open a run-scoped connection when ``requires_connect`` is True."""

    def close(self) -> None:
        """Close a run-scoped connection when ``requires_connect`` is True."""


def tool_attributes(cls: type[Toolkit]) -> tuple[str, ...]:
    """Return the attributes of ``cls`` that ``@tool`` marks as its functions, in the order the class defines them."""
    attributes = dict.fromkeys(attribute for base in reversed(cls.__mro__) for attribute in vars(base))
    return tuple(
        attribute
        for attribute in attributes
        if tool_mark(_function_of(inspect.getattr_static(cls, attribute))) is not None
    )


__all__ = [
    "CurrentCall",
    "ToolMark",
    "Toolkit",
    "current_call",
    "passthrough_tool",
    "tool",
    "tool_attributes",
    "tool_mark",
]
