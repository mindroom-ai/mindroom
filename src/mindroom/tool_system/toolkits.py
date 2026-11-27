"""Adapt native toolkits to ``ToolFunction`` and apply configured function filters, output files, and worker routing.

``tool_schema`` derives the schema of every function its signature gives and validates its calls' arguments, for
MindRoom's native ``authoring.Toolkit`` functions. Functions read their call context through ``current_call()`` rather
than framework-injected parameters.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from mindroom.llm.media import File, named_by_resource
from mindroom.logging_config import get_logger
from mindroom.tool_system import authoring
from mindroom.tool_system.declarations import SupportsPrimaryCallPlacement
from mindroom.tool_system.output_files import ToolOutputFilePolicy, with_output_files
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolValue
from mindroom.tool_system.tool_schema import tool_description, tool_parameters, validated_entrypoint

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from mindroom.tool_system.authoring import Toolkit
    from mindroom.tool_system.sandbox_proxy import WorkerStandIns

logger = get_logger(__name__)

# Parameter names that MindRoom never injects and refuses on tool functions so models cannot spoof call identity.
_RESERVED_FRAMEWORK_NAMES = frozenset(
    {"agent", "team", "run_context", "_agno_agent", "_agno_team", "_agno_run_context"},
)


@dataclass(frozen=True, slots=True)
class _Finishing:
    """How one built toolkit's functions are offered: which ones, with output files, and through worker stand-ins."""

    offered: frozenset[str] | None = None
    output_files: ToolOutputFilePolicy | None = None
    to_worker: WorkerStandIns | None = None

    def finished(self, function: ToolFunction) -> ToolFunction:
        """Return ``function``, adapted, with output files and through its worker stand-in where configured."""
        if self.output_files is not None:
            function = with_output_files(function, self.output_files)
        if self.to_worker is not None:
            function = replace(function, entrypoint=self.to_worker(function.name, function.entrypoint))
        return function


# No value holds its toolkit, which would keep the toolkit, and whatever it holds, alive for the whole process.
_FINISHING: WeakKeyDictionary[Toolkit, _Finishing] = WeakKeyDictionary()


def configure_toolkit_functions[T: Toolkit](
    toolkit: T,
    *,
    offered: Collection[str] | None = None,
    output_files: ToolOutputFilePolicy | None = None,
    to_worker: WorkerStandIns | None = None,
) -> T:
    """Record how whoever built ``toolkit`` offers its functions; every adaptation of the toolkit applies it.

    ``offered`` names the functions an agent is offered, None every one, as ``get_tool_by_name``'s function filters
    leave them. With ``output_files``, each function takes ``mindroom_output_path`` too, and ``to_worker``, given
    a function's name and finished entrypoint, returns its stand-in that sends its calls to a worker.
    """
    _FINISHING[toolkit] = _Finishing(None if offered is None else frozenset(offered), output_files, to_worker)
    return toolkit


def toolkit_worker_stand_ins(toolkit: Toolkit) -> WorkerStandIns | None:
    """Return the stand-ins that send ``toolkit``'s calls to a worker, or None when its calls run here."""
    return _FINISHING.get(toolkit, _Finishing()).to_worker


def rename_toolkit_functions(toolkit: Toolkit, aliases: Mapping[str, str]) -> None:
    """Offer each function of ``toolkit`` that ``aliases`` names under its alias, which also names it on the toolkit.

    The original methods stay, so code calling them directly keeps working.
    """
    toolkit.functions = {aliases.get(name, name): function for name, function in toolkit.functions.items()}
    for alias in aliases.values():
        if (entrypoint := toolkit.functions.get(alias)) is not None:
            setattr(toolkit, alias, entrypoint)


def requires_connect(toolkit: Toolkit) -> bool:
    """Whether ``toolkit`` holds a connection this process opens and closes around its calls, as Postgres and Redshift do.

    A toolkit whose calls go to a worker connects there instead, unless it keeps some of its calls here
    (``SupportsPrimaryCallPlacement``).
    """
    if not isinstance(toolkit, authoring.Toolkit) or not toolkit.requires_connect:
        return False
    return toolkit_worker_stand_ins(toolkit) is None or isinstance(toolkit, SupportsPrimaryCallPlacement)


@runtime_checkable
class FunctionScopedInstructions(Protocol):
    """A toolkit whose instructions describe its functions, so they name only the ones an agent is offered."""

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return the toolkit's instructions for an agent offered ``function_names`` of it."""
        ...


def adapt_toolkit(
    toolkit: Toolkit,
    *,
    key: str | None = None,
    registered: str | None = None,
) -> AdaptedToolkit:
    """Adapt the functions ``toolkit`` offers, one per name, in the order offered.

    Each function is offered as its builder configured it (``configure_toolkit_functions``). ``key`` is the authored
    toolkit an agent is offered the functions from, which each one carries as its ``toolkit``; None for a toolkit
    generated for the agent, and for a caller that names the toolkit beside each function, as the MCP gateway's search
    does. ``registered`` is the registered tool they come from, which each one carries as its ``registered_toolkit``;
    it is ``key`` unless an implied or preset tool brings the toolkit in.
    """
    finishing = _FINISHING.get(toolkit, _Finishing())
    names = toolkit_function_names(toolkit)
    functions = [_adapt_function(toolkit, name, finishing) for name in names]
    instructions = toolkit.instructions or None
    return AdaptedToolkit(
        name=toolkit.name,
        functions=tuple(replace(function, toolkit=key, registered_toolkit=registered or key) for function in functions),
        instructions=instructions,
        guidance=instructions,
    )


def adapt_callable(entrypoint: Callable[..., Any]) -> ToolFunction:
    """Derive ``entrypoint``, named and described as ``@tool`` marks it, as one function of a toolkit."""
    (function,) = adapt_toolkit(authoring.Toolkit(tools=[entrypoint])).functions
    return function


def toolkit_function_names(toolkit: Toolkit) -> tuple[str, ...]:
    """Return the names of the functions ``toolkit`` offers, in its ``functions`` order."""
    offered = _FINISHING.get(toolkit, _Finishing()).offered
    return tuple(name for name in toolkit.functions if offered is None or name in offered)


def toolkit_function(toolkit: Toolkit, name: str) -> ToolFunction | None:
    """Return the function ``toolkit`` offers under ``name``, adapted and finished alone; None when it offers none."""
    if name not in toolkit_function_names(toolkit):
        return None
    return _adapt_function(toolkit, name, _FINISHING.get(toolkit, _Finishing()))


def with_offered_instructions(
    toolkit: Toolkit,
    adapted: AdaptedToolkit,
) -> AdaptedToolkit:
    """Return ``adapted``, the functions an agent offers of ``toolkit``, with the instructions ``toolkit`` gives for them.

    A toolkit whose instructions describe its functions (``FunctionScopedInstructions``) gives the ones for those
    functions. Instructions set in a tool's settings or returned by any other toolkit stay intact.
    """
    if isinstance(toolkit, FunctionScopedInstructions):
        offered = [function.name for function in adapted.functions]
        instructions = toolkit.instructions_for(offered) or None
        return replace(adapted, instructions=instructions, guidance=instructions)
    return adapted


def _adapt_function(toolkit: Toolkit, name: str, finishing: _Finishing) -> ToolFunction:
    """Derive one function of a toolkit: its description, schema, and entrypoint.

    A function ``passthrough_tool`` marked keeps its schema and takes its arguments unvalidated; any other is derived
    from its signature and docstring and validates its arguments. The toolkit's ``offered_function`` then fits it to
    the instance, before its schema offers ``mindroom_output_path``. Reserved framework parameter names are refused:
    nothing fills them, and a value the model sent there would choose whose identity the tool acts for.
    """
    function = toolkit.functions[name]
    parameters = inspect.signature(function).parameters
    if taken := sorted(parameter for parameter in parameters if parameter in _RESERVED_FRAMEWORK_NAMES):
        msg = (
            f"Tool function {name!r} takes {', '.join(taken)}, which MindRoom does not inject;"
            " read the call through current_call() instead"
        )
        raise ValueError(msg)
    authored = authoring.passthrough_parameters(function)
    derived = authored is None
    offered = toolkit.offered_function(
        ToolFunction(
            name=name,
            description=authoring.authored_description(function) or (tool_description(function) if derived else None),
            parameters=tool_parameters(function) if authored is None else deepcopy(authored),
            entrypoint=validated_entrypoint(function) if derived else function,
        ),
    )
    return finishing.finished(offered)


def model_arguments(arguments: Mapping[str, Any], function: ToolFunction) -> dict[str, Any]:
    """Drop model-sent values for undeclared reserved framework parameter names."""
    properties = function.parameters.get("properties")
    declared = set(properties) if isinstance(properties, Mapping) else set()
    dropped = {name for name in arguments if name in _RESERVED_FRAMEWORK_NAMES and name not in declared}
    if not dropped:
        return dict(arguments)
    logger.warning(
        "Ignored tool-call arguments that name framework-reserved parameters",
        tool_name=function.name,
        dropped=sorted(dropped),
    )
    return {name: value for name, value in arguments.items() if name not in dropped}


@dataclass(slots=True)
class StreamCollector:
    """Accumulate what a generator tool yields: text joins, and the media of each tool value it yields collects."""

    _chunks: list[str] = field(default_factory=list)
    _values: list[ToolValue] = field(default_factory=list)

    def add(self, item: object) -> None:
        """Record one yielded item."""
        value = read_tool_value(item)
        if value is None:
            self._chunks.append(str(item))
            return
        self._chunks.append(value.content)
        self._values.append(value)

    def value(self) -> object:
        """Return the drained value: the joined text, carried by a ``ToolValue`` when any item was one."""
        text = "".join(self._chunks)
        if not self._values:
            return text
        return ToolValue(
            content=text,
            images=tuple(image for value in self._values for image in value.images),
            audios=tuple(audio for value in self._values for audio in value.audios),
            videos=tuple(video for value in self._values for video in value.videos),
            files=tuple(file for value in self._values for file in value.files),
        )


def _normalize_file(file: File) -> File:
    """When inline bytes sit beside a path, keep the path's name and type and drop the path so readers never open it."""
    if file.content is not None and file.filepath is not None:
        return replace(named_by_resource(file, Path(file.filepath).name), filepath=None)
    return file


def read_tool_value(result: object) -> ToolValue | None:
    """Read a tool's return value as a ``ToolValue``; None for a value that is not a tool value."""
    if not isinstance(result, ToolValue):
        return None
    if any(file.content is not None and file.filepath is not None for file in result.files):
        return replace(result, files=tuple(_normalize_file(file) for file in result.files))
    return result


__all__ = [
    "FunctionScopedInstructions",
    "StreamCollector",
    "adapt_callable",
    "adapt_toolkit",
    "configure_toolkit_functions",
    "model_arguments",
    "read_tool_value",
    "rename_toolkit_functions",
    "requires_connect",
    "toolkit_function",
    "toolkit_function_names",
    "toolkit_worker_stand_ins",
    "with_offered_instructions",
]
