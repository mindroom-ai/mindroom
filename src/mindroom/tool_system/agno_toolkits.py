"""Adapt toolkits to ``ToolFunction``, and give Agno entrypoints what they expect from Agno at call time.

``tool_schema`` derives the schema of every function its signature gives and validates its calls' arguments, for an
owned toolkit (``authoring.Toolkit``), whose functions take no framework-supplied argument, and for an Agno toolkit,
whose schemas leave those arguments out. Agno's public ``process_entrypoint`` only finishes an Agno function that
authored its schema or asks for a strict one.
At call time this module builds the Agno-shaped arguments an entrypoint declares, and ``read_tool_value`` reads the
Agno ``ToolResult`` a vendor tool returns as the ``ToolValue`` MindRoom's own tools return. Those owned values live in
``tool_function``, so a module that only handles them imports no Agno module.
MindRoom modules outside the tool and toolkit packages import Agno's tool types only from here.
"""

from __future__ import annotations

import inspect
import re
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeGuard, get_type_hints, runtime_checkable
from weakref import WeakKeyDictionary

from agno.run import RunContext
from agno.tools import Toolkit
from agno.tools.function import Function, ToolResult
from agno.utils.schema import (
    AGNO_INJECTED_PARAMS,
    MEDIA_INJECTED_PARAMS,
    annotation_binds,
    is_framework_typed,
    unwrap_annotation,
)

from mindroom.llm.media import Audio, File, Image, Video, named_by_resource
from mindroom.logging_config import get_logger
from mindroom.tool_system import authoring
from mindroom.tool_system.output_files import (
    ToolOutputFilePolicy,
    output_file_entrypoint,
    with_optional_output_path,
)
from mindroom.tool_system.tool_function import AdaptedToolkit, InjectedValue, ToolFunction, ToolValue
from mindroom.tool_system.tool_schema import tool_description, tool_parameters, validated_entrypoint

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from agno.media import Audio as AgnoAudio
    from agno.media import File as AgnoFile
    from agno.media import Image as AgnoImage
    from agno.media import Video as AgnoVideo

    from mindroom.tool_system.invocation import ToolCallContext

logger = get_logger(__name__)

# Agno's default function schema, which marks a function whose signature gives its schema; any other the tool authored.
_AGNO_EMPTY_PARAMETERS: Mapping[str, Any] = {"type": "object", "properties": {}, "required": []}

# What Agno supplies to each parameter it fills by name. It injects ``fc`` (its FunctionCall) too; the owned executor
# has no such object, so that parameter keeps its default, but the model never fills it either way.
_RESERVED_NAMES: Mapping[str, InjectedValue] = {
    **dict.fromkeys(("agent", "team", "_agno_agent", "_agno_team"), InjectedValue.ACTOR),
    **dict.fromkeys(("run_context", "_agno_run_context"), InjectedValue.RUN_CONTEXT),
    **dict.fromkeys(MEDIA_INJECTED_PARAMS, InjectedValue.MEDIA),
    "fc": InjectedValue.DEFAULT,
}
_UNBINDABLE_KINDS = frozenset(
    (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY),
)
# A built toolkit: an owned one, or an Agno one, such as a vendor toolkit, which only this module looks inside.
AnyToolkit = Toolkit | authoring.Toolkit


@dataclass(frozen=True, slots=True)
class _Finishing:
    """How one built toolkit's functions are offered: which ones, with output files, and through worker stand-ins."""

    offered: frozenset[str] | None = None
    output_files: ToolOutputFilePolicy | None = None
    to_worker: Callable[[str, Callable[..., object]], Callable[..., object]] | None = None

    def entrypoint(
        self,
        name: str,
        entrypoint: Callable[..., object],
        parameters: Mapping[str, Any] | None,
    ) -> tuple[Callable[..., object], bool]:
        """Return the entrypoint a function's schema and validation derive from, and whether it takes output files.

        ``parameters`` is the schema the tool authored, if any; a function that takes ``mindroom_output_path`` by
        either keeps its own.
        """
        wrapped = (
            None
            if self.output_files is None
            else output_file_entrypoint(entrypoint, self.output_files, tool_name=name, parameters=parameters)
        )
        entrypoint = wrapped or entrypoint
        if self.to_worker is not None:
            entrypoint = self.to_worker(name, entrypoint)
        return entrypoint, wrapped is not None


# No value holds its toolkit, which would keep the toolkit, and whatever it holds, alive for the whole process.
_FINISHING: WeakKeyDictionary[AnyToolkit, _Finishing] = WeakKeyDictionary()


def configure_toolkit_functions[T: AnyToolkit](
    toolkit: T,
    *,
    offered: Collection[str] | None = None,
    output_files: ToolOutputFilePolicy | None = None,
    to_worker: Callable[[str, Callable[..., object]], Callable[..., object]] | None = None,
) -> T:
    """Record how whoever built ``toolkit`` offers its functions; every adaptation of the toolkit applies it.

    ``offered`` names the functions an agent is offered, None every one, as ``get_tool_by_name``'s function filters
    leave them. With ``output_files``, each function takes ``mindroom_output_path`` too, and ``to_worker``, given
    a function's name and entrypoint, returns its stand-in that sends its calls to a worker.
    """
    _FINISHING[toolkit] = _Finishing(None if offered is None else frozenset(offered), output_files, to_worker)
    return toolkit


def rename_toolkit_functions(toolkit: AnyToolkit, aliases: Mapping[str, str]) -> None:
    """Offer each function of ``toolkit`` that ``aliases`` names under its alias, which also names it on the toolkit.

    The original methods stay, so code calling them directly keeps working.
    """
    if isinstance(toolkit, authoring.Toolkit):
        toolkit.functions = {aliases.get(name, name): function for name, function in toolkit.functions.items()}
        entrypoints: Mapping[str, Callable[..., Any] | None] = toolkit.functions
    else:
        toolkit.functions = _aliased(toolkit.functions, aliases)
        toolkit.async_functions = _aliased(toolkit.async_functions, aliases)
        entrypoints = {name: function.entrypoint for name, function in toolkit.functions.items()}
    for alias in aliases.values():
        if (entrypoint := entrypoints.get(alias)) is not None:
            setattr(toolkit, alias, entrypoint)


def _aliased(functions: Mapping[str, Function], aliases: Mapping[str, str]) -> dict[str, Function]:
    aliased: dict[str, Function] = {}
    for name, function in functions.items():
        function.name = aliases.get(name, name)
        aliased[function.name] = function
    return aliased


def requires_connect(toolkit: AnyToolkit) -> TypeGuard[Toolkit]:
    """Whether ``toolkit`` holds a connection its run opens and closes, as Agno's Postgres toolkit does."""
    return isinstance(toolkit, Toolkit) and toolkit.requires_connect


@dataclass(frozen=True, slots=True)
class _Rejected:
    """Fields whose value the agent loop cannot honor: a function for which ``sets`` holds is refused with ``error``.

    Rejected rather than dropped, because the tool's author relies on what the value asks for. A function adapted
    alone raises it; a toolkit's adapter names the function in ``AdaptedToolkit.refused`` instead, so an agent refuses
    the toolkit only while it still offers the function (``offered_refusal``).
    """

    sets: Callable[[Function], bool]
    error: str


# Agno adds a function's instructions to the system prompt only with ``add_instructions``.
_FUNCTION_INSTRUCTIONS = _Rejected(
    sets=lambda function: function.add_instructions and function.instructions is not None,
    error="sets instructions for Agno to add to the system prompt, which MindRoom adds only per toolkit; set"
    " them on the toolkit instead",
)

# Every field of Agno's ``Function``, each named by one row: "mapped" fields reach the ``ToolFunction``, a rejected
# field fails the function that sets it, and an "ignored" field is left unread for the reason beside it.
# tests/tool_system/test_agno_toolkits.py fails when Agno adds a field no row names.
_FUNCTION_FIELDS: Mapping[tuple[str, ...], Literal["mapped", "ignored"] | _Rejected] = {
    # The model-facing definition; ``parameters`` becomes the finished schema.
    ("name", "description", "parameters", "strict"): "mapped",
    # Wrapped in argument validation unless processing is skipped; the signature names the injected parameters.
    ("entrypoint", "skip_entrypoint_processing"): "mapped",
    # The pause and stop flags the loop reads.
    (
        "requires_confirmation",
        "approval_type",
        "external_execution",
        "external_execution_silent",
        "stop_after_tool_call",
    ): "mapped",
    ("pre_hook", "post_hook", "tool_hooks"): _Rejected(
        sets=lambda function: (
            function.pre_hook is not None or function.post_hook is not None or bool(function.tool_hooks)
        ),
        error="sets an Agno pre_hook, post_hook, or tool_hooks, which MindRoom does not run; move that logic into"
        " the tool or a `tool:before_call` / `tool:after_call` hook",
    ),
    ("requires_user_input",): _Rejected(
        sets=lambda function: bool(function.requires_user_input),
        error="sets Agno's requires_user_input, but MindRoom never pauses a run to ask its user for input",
    ),
    # A toolkit's adapter hands them over as ``function_instructions`` instead, which a deferred toolkit moves to tool
    # search or the function's description and an agent refuses in an eager one.
    ("instructions", "add_instructions"): _FUNCTION_INSTRUCTIONS,
    # Agno reads these only for a function that requires user input, which is rejected.
    ("user_input_fields", "user_input_schema"): "ignored",
    # Agno also showed the result in the reply text; MindRoom gives a tool's result only to the model.
    ("show_result",): "ignored",
    # Agno's on-disk result cache; every call runs.
    ("cache_results", "cache_dir", "cache_ttl"): "ignored",
    # Metadata for people, Agno's discovery, and MCP clients; the model never sees it.
    ("title", "annotations", "has_side_effects"): "ignored",
    # The adapter's caller names each function's toolkit (``ToolFunction.toolkit``).
    ("owning_toolkit",): "ignored",
    # Set only by Agno's component registry, which MindRoom does not use.
    ("source_toolkit",): "ignored",
}


# AGNO_COMPAT: Agno toolkits' default instructions name functions the agent may not be offered.
# Reason: Agno 3.0.9's ReasoningTools, ZepTools, DaytonaTools, GoogleCalendarTools, and GmailTools build default
# instructions that name functions their enable switches can leave out, and the function filters MindRoom applies after
# construction can remove any named function, so the prompt would tell the model to call a function it does not have.
# Upstream issue: Tracking gap; searches of Agno's issues and PRs for these toolkits' instructions found no match.
# Upstream PR: No matching fix has been verified.
# Remove when: a toolkit's default instructions name only the functions an agent is offered, so its row can go, and
# the check in with_offered_instructions with the last row; instructions set in a tool's settings must still show.
# Coverage: tests/tool_system/test_agno_toolkits.py::test_agno_default_instructions_show_only_while_every_function_they_name_is_offered,
#   tests/agent_loop/test_agent_build.py::test_reasoning_s_default_instructions_show_only_while_both_its_functions_are_offered.
# Each toolkit class, by import path and including subclasses, maps to the functions its default instructions can name.
_DEFAULT_INSTRUCTION_FUNCTIONS: dict[str, frozenset[str]] = {
    "agno.tools.daytona.DaytonaTools": frozenset(
        {
            "change_directory",
            "create_file",
            "delete_file",
            "list_files",
            "read_file",
            "run_code",
            "run_shell_command",
        },
    ),
    "agno.tools.google.calendar.GoogleCalendarTools": frozenset(
        {
            "check_availability",
            "delete_event",
            "get_event",
            "list_events",
            "quick_add_event",
            "search_events",
            "update_event",
        },
    ),
    "agno.tools.google.gmail.GmailTools": frozenset({"create_draft_email", "send_email", "send_email_reply"}),
    "agno.tools.reasoning.ReasoningTools": frozenset({"analyze", "think"}),
    "agno.tools.zep.ZepTools": frozenset({"add_zep_message", "get_zep_memory", "search_zep_memory"}),
}


@runtime_checkable
class FunctionScopedInstructions(Protocol):
    """A toolkit whose instructions describe its functions, so they name only the ones an agent is offered."""

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return the toolkit's instructions for an agent offered ``function_names`` of it."""
        ...


def adapt_toolkit(
    toolkit: AnyToolkit,
    *,
    key: str | None = None,
    registered: str | None = None,
) -> AdaptedToolkit:
    """Adapt the functions ``toolkit`` offers, one per name, in Agno's async-run order; they share one ``actor_state``.

    Each function is offered as its builder configured it (``configure_toolkit_functions``). ``key`` is the authored
    toolkit an agent is offered the functions from, which each one carries as its ``toolkit``; None for a toolkit
    generated for the agent, and for a caller that names the toolkit beside each function, as the MCP gateway's search
    does. ``registered`` is the registered tool they come from, which each one carries as its ``registered_toolkit``;
    it is ``key`` unless an implied or preset tool brings the toolkit in. A function's prompt instructions are handed
    over as ``function_instructions``, and a function that sets any other field MindRoom refuses is adapted without it
    and named in ``refused``, so whoever offers the toolkit refuses it only for a function it still offers.
    """
    finishing = _FINISHING.get(toolkit, _Finishing())
    names = toolkit_function_names(toolkit)
    if isinstance(toolkit, authoring.Toolkit):
        functions = [_owned_function(toolkit, name, finishing) for name in names]
        refused: dict[str, str] = {}
        function_instructions: dict[str, str] = {}
    else:
        functions, refused, function_instructions = _agno_functions(toolkit, names, finishing)
    actor_state: dict[str, Any] = {}
    instructions = toolkit.instructions or None
    return AdaptedToolkit(
        name=toolkit.name,
        functions=tuple(
            replace(function, toolkit=key, registered_toolkit=registered or key, actor_state=actor_state)
            for function in functions
        ),
        instructions=instructions,
        guidance=instructions,
        function_instructions=function_instructions,
        refused=refused,
    )


def adapt_callable(entrypoint: Callable[..., Any]) -> ToolFunction:
    """Derive ``entrypoint``, named and described as ``@tool`` marks it, as one function of an owned toolkit."""
    (function,) = adapt_toolkit(authoring.Toolkit(tools=[entrypoint])).functions
    return function


def toolkit_function_names(toolkit: AnyToolkit) -> tuple[str, ...]:
    """Return the names of the functions ``toolkit`` offers, in Agno's async-run order: synchronous ones first."""
    offered = _FINISHING.get(toolkit, _Finishing()).offered
    if isinstance(toolkit, authoring.Toolkit):
        # TODO(simplify): an owned toolkit offers its synchronous functions before its async ones, as Agno's toolkit
        # registers them; the order the class defines them in would reorder the tools of requests.
        names = sorted(toolkit.functions, key=lambda name: inspect.iscoroutinefunction(toolkit.functions[name]))
    else:
        names = list(toolkit.get_async_functions())
    return tuple(name for name in names if offered is None or name in offered)


def toolkit_function(toolkit: AnyToolkit, name: str) -> ToolFunction | None:
    """Return the function ``toolkit`` offers under ``name``, adapted and finished alone; None when it offers none.

    The function names no toolkit; a function that sets a field MindRoom refuses raises ``ValueError``.
    """
    if name not in toolkit_function_names(toolkit):
        return None
    finishing = _FINISHING.get(toolkit, _Finishing())
    if isinstance(toolkit, authoring.Toolkit):
        return _owned_function(toolkit, name, finishing)
    function = toolkit.get_async_functions()[name]
    if (refusal := _refusal(function)) is not None:
        raise ValueError(refusal)
    return _adapt(function, finishing)


def _agno_functions(
    toolkit: Toolkit,
    names: Collection[str],
    finishing: _Finishing,
) -> tuple[list[ToolFunction], dict[str, str], dict[str, str]]:
    """Return the named functions of an Agno toolkit adapted, why MindRoom refuses each, and each one's instructions."""
    available = toolkit.get_async_functions()
    functions = [available[name] for name in names]
    adapted: list[ToolFunction] = []
    refused: dict[str, str] = {}
    for function in functions:
        offered = _without_instructions(function)
        if (refusal := _refusal(offered)) is not None:
            refused[function.name] = refusal
        adapted.append(_adapt(offered, finishing))
    function_instructions = {
        function.name: function.instructions
        for function in functions
        if _FUNCTION_INSTRUCTIONS.sets(function) and function.instructions is not None
    }
    return adapted, refused, function_instructions


def with_offered_instructions(
    toolkit: AnyToolkit,
    adapted: AdaptedToolkit,
    *,
    authored_instructions: bool,
) -> AdaptedToolkit:
    """Return ``adapted``, the functions an agent offers of ``toolkit``, with the instructions ``toolkit`` gives for them.

    A toolkit whose instructions describe its functions (``FunctionScopedInstructions``) gives the ones for those
    functions. Agno's default instructions of a toolkit in ``_DEFAULT_INSTRUCTION_FUNCTIONS`` show only while every
    function they name is offered. ``authored_instructions`` says the toolkit was built with instructions from its
    settings, in ``config.yaml`` or saved for the tool, instead of its defaults; those always show.
    """
    offered = [function.name for function in adapted.functions]
    if isinstance(toolkit, FunctionScopedInstructions):
        instructions = toolkit.instructions_for(offered) or None
        return replace(adapted, instructions=instructions, guidance=instructions)
    if authored_instructions or not adapted.instructions or _default_named(toolkit, adapted.instructions) <= {*offered}:
        return adapted
    return replace(adapted, instructions=None, guidance=None)


def _default_named(toolkit: AnyToolkit, instructions: str) -> frozenset[str]:
    """Return the functions ``instructions``, the default ones of ``toolkit``, name of those its table row lists."""
    for cls in type(toolkit).__mro__:
        if (names := _DEFAULT_INSTRUCTION_FUNCTIONS.get(f"{cls.__module__}.{cls.__qualname__}")) is not None:
            return names.intersection(re.findall(r"\w+", instructions))
    return frozenset()


def offered_refusal(toolkit: AdaptedToolkit) -> str | None:
    """Return why an agent cannot offer ``toolkit``: the first function it offers that sets a field MindRoom refuses.

    A function's prompt instructions count only while ``function_instructions`` holds them, which a deferred toolkit
    moves to tool search or the function's description instead.
    """
    for function in toolkit.functions:
        if (refusal := toolkit.refused.get(function.name)) is not None:
            return refusal
        if function.name in toolkit.function_instructions:
            return f"Tool function {function.name!r} {_FUNCTION_INSTRUCTIONS.error}"
    return None


def _without_instructions(function: Function) -> Function:
    """Return ``function`` without the prompt instructions its toolkit's adapter hands over separately."""
    if not _FUNCTION_INSTRUCTIONS.sets(function):
        return function
    copy = function.model_copy(deep=False)
    copy.instructions = None
    return copy


def _refusal(function: Function) -> str | None:
    """Return why MindRoom refuses ``function``: the first field it sets that the agent loop cannot honor."""
    for handling in _FUNCTION_FIELDS.values():
        if isinstance(handling, _Rejected) and handling.sets(function):
            return f"Tool function {function.name!r} {handling.error}"
    return None


def _adapt(function: Function, finishing: _Finishing) -> ToolFunction:
    """Return one Agno function as a ``ToolFunction`` with its finished schema, leaving the shared ``Function`` alone.

    A function whose signature gives its schema is derived as an owned toolkit's is (``tool_schema``), without the
    parameters the framework fills, and validates its calls' arguments, unless it takes the calling agent, whose
    stand-in no ``Agent`` annotation admits. Agno's ``process_entrypoint``, run on a copy because it rewrites a schema
    in place, finishes any other function: it completes a schema the tool authored or derives a strict one, and wraps
    the entrypoint in its own coercion unless the tool skips that processing. A field MindRoom refuses is left unread.
    """
    if function.entrypoint is None:
        msg = f"Tool function {function.name!r} has no entrypoint"
        raise ValueError(msg)
    authored = function.skip_entrypoint_processing or function.parameters != _AGNO_EMPTY_PARAMETERS
    entrypoint, output_files = finishing.entrypoint(
        function.name,
        function.entrypoint,
        function.parameters if authored else None,
    )
    # An explicit strict setting on the function wins; MindRoom never asks for strict schemas otherwise.
    strict = False if output_files else function.strict
    injected = injected_parameters(function.entrypoint)
    if authored or strict:
        prepared = function.model_copy(deep=False)
        prepared.entrypoint = entrypoint
        prepared.parameters = deepcopy(function.parameters)
        prepared.process_entrypoint(strict=bool(strict))
        assert prepared.entrypoint is not None
        description, parameters, entrypoint = prepared.description, prepared.parameters, prepared.entrypoint
    else:
        description = function.description or tool_description(entrypoint)
        parameters = tool_parameters(entrypoint, filled=injected)
        if InjectedValue.ACTOR not in injected.values():
            entrypoint = validated_entrypoint(entrypoint)
    return ToolFunction(
        name=function.name,
        description=description,
        parameters=with_optional_output_path(parameters) if output_files else parameters,
        strict=strict,
        requires_confirmation=function.requires_confirmation,
        approval_type=function.approval_type,
        external_execution=function.external_execution,
        external_execution_silent=function.external_execution_silent,
        stop_after_call=function.stop_after_tool_call,
        entrypoint=entrypoint,
        injected_params=injected,
    )


def _owned_function(toolkit: authoring.Toolkit, name: str, finishing: _Finishing) -> ToolFunction:
    """Derive one function of an owned toolkit without Agno: its description, schema, and entrypoint.

    A function ``passthrough_tool`` marked keeps its schema and takes its arguments unvalidated; any other is derived
    from its signature and docstring and validates its arguments. The toolkit's ``offered_function`` then fits it to
    the instance, before its schema offers ``mindroom_output_path``. A parameter Agno would hand its agent, team, or
    run context, by name or by type, is refused: nothing fills it, and a value the model sent there would choose whose
    identity the tool acts for.
    """
    function = toolkit.functions[name]
    framework_objects = (InjectedValue.ACTOR, InjectedValue.RUN_CONTEXT)
    injected = injected_parameters(function)
    if taken := sorted(parameter for parameter, value in injected.items() if value in framework_objects):
        msg = (
            f"Tool function {name!r} takes {', '.join(taken)}, which only Agno toolkits are handed;"
            " read the call through current_call() instead"
        )
        raise ValueError(msg)
    authored = authoring.passthrough_parameters(function)
    entrypoint, output_files = finishing.entrypoint(name, function, authored)
    derived = authored is None
    offered = toolkit.offered_function(
        ToolFunction(
            name=name,
            description=authoring.authored_description(function) or (tool_description(entrypoint) if derived else None),
            parameters=tool_parameters(entrypoint) if authored is None else deepcopy(authored),
            # The flags Agno's registration sets for a function no toolkit option asks anything of.
            requires_confirmation=False,
            external_execution=False,
            entrypoint=validated_entrypoint(entrypoint) if derived else entrypoint,
        ),
    )
    if not output_files:
        return offered
    return replace(offered, parameters=with_optional_output_path(offered.parameters), strict=False)


def injected_parameters(entrypoint: Callable[..., Any]) -> dict[str, InjectedValue]:
    """Return what Agno supplies to each parameter of ``entrypoint`` the model does not fill, by name or by type."""
    parameters = inspect.signature(entrypoint).parameters
    injected = {name: _RESERVED_NAMES[name] for name in parameters if name in _RESERVED_NAMES}
    try:
        hints = get_type_hints(entrypoint)
    except Exception:
        # Agno falls back to the reserved names when a hint does not resolve, such as an unimportable Agent.
        return injected
    for name, hint in hints.items():
        parameter = parameters.get(name)
        if parameter is None or parameter.kind in _UNBINDABLE_KINDS or name in injected:
            continue
        try:
            owned = is_framework_typed(hint)
        except Exception:
            owned = True  # Agno treats an unclassifiable annotation as framework-owned.
        if owned:
            injected[name] = _bound_by_annotation(hint)
    return injected


def _bound_by_annotation(hint: object) -> InjectedValue:
    """Return what Agno binds to an identity-typed parameter (``ctx: RunContext``, ``owner: Agent``), unions included."""
    from agno.agent import Agent  # noqa: PLC0415 - Only identity-typed parameters need Agno's agent types.
    from agno.team import Team  # noqa: PLC0415

    # Agno binds no parameter by annotation when classifying one fails.
    with suppress(Exception):
        hint = unwrap_annotation(hint)
        if annotation_binds(hint, (Agent, Team)):
            return InjectedValue.ACTOR
        if annotation_binds(hint, (RunContext,)):
            return InjectedValue.RUN_CONTEXT
    return InjectedValue.DEFAULT


# AGNO_COMPAT: Agno toolkits take the calling agent as a parameter.
# Reason: Agno injects its Agent or Team object into entrypoint parameters named ``agent`` or ``team``, or annotated
# with those types, and the agent loop has no Agno agent to inject. Of the toolkits MindRoom registers, only
# DaytonaTools reads the parameter, and it reads and writes ``agent.session_state``, as Agno's AntigravityTools and
# SuperserveTools also do. That attribute is the Agno agent's own state, not the run's stored session state, and
# MindRoom built one agent per reply, so the stand-in carries the adapted toolkit's ``actor_state``, which is never
# stored. MindRoom's own tools read their caller from the tool runtime context.
# Upstream issue: Tracking gap; Agno defines no contract for what a toolkit may read from the injected agent, so a
# toolkit can depend on any Agent attribute.
# Upstream PR: None identified.
# Remove when: Agno toolkits read run state from ``run_context`` instead of the agent parameter, or MindRoom drops
# every toolkit that reads the agent parameter; the injected ``run_context`` stays.
# Coverage: tests/tool_system/test_tool_invocation.py::test_agent_and_team_parameters_share_the_toolkits_own_state_stand_in;
# tests/tool_system/test_tool_invocation.py::test_identity_typed_parameters_are_injected_by_annotation.
@dataclass(slots=True)
class ToolCallActor:
    """What an Agno toolkit may read from its injected ``agent`` or ``team``: the state its toolkit keeps there."""

    session_state: dict[str, Any]


def framework_arguments(function: ToolFunction, context: ToolCallContext) -> dict[str, Any]:
    """Return the framework-supplied arguments of one call, keyed by the entrypoint parameters that take them."""
    if not function.injected_params:
        return {}
    supplied: dict[InjectedValue, object] = {
        InjectedValue.ACTOR: ToolCallActor(session_state=function.actor_state),
        InjectedValue.RUN_CONTEXT: RunContext(
            run_id=context.run_id,
            session_id=context.session_id,
            user_id=context.user_id,
            metadata=context.metadata,
            session_state=context.session_state,
        ),
        # The agent loop gives a tool no run media, so each media name receives None.
        InjectedValue.MEDIA: None,
    }
    return {name: supplied[value] for name, value in function.injected_params.items() if value in supplied}


def warm_agno_tool_imports() -> None:
    """Import the agent and team types Agno resolves to classify tool parameters, so later builds skip the cost."""
    from agno.agent import Agent  # noqa: PLC0415, F401 - Imported for its side effect of loading the module.
    from agno.team import Team  # noqa: PLC0415, F401


def model_arguments(arguments: Mapping[str, Any], function: ToolFunction) -> dict[str, Any]:
    """Drop model-sent values for parameters the framework supplies.

    A media name the schema declares stays the model's, because a wrapper may expose a wrapped tool's own ``files``
    argument; identity names never do, whatever the schema says.
    """
    properties = function.parameters.get("properties")
    declared = set(properties) if isinstance(properties, dict) else set()
    injected = function.injected_params
    dropped = {
        name
        for name in arguments
        if name in AGNO_INJECTED_PARAMS
        or (name in injected and (name not in declared or injected[name] != InjectedValue.MEDIA))
    }
    if not dropped:
        return dict(arguments)
    logger.warning(
        "Ignored tool-call arguments that name framework-injected parameters",
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


def read_tool_value(result: object) -> ToolValue | None:
    """Read a tool's return value as a ``ToolValue``; None for a value that is not a tool value.

    This is the one place a vendor tool's Agno ``ToolResult`` becomes MindRoom's, its text and its media as MindRoom's
    media. A ``ToolValue``, as MindRoom's own tools and a tool job's saved result return, stands as it is.
    """
    if isinstance(result, ToolValue):
        return result
    if not isinstance(result, ToolResult):
        return None
    return ToolValue(
        content=result.content,
        images=tuple(_owned_image(image) for image in result.images or ()),
        audios=tuple(_owned_audio(audio) for audio in result.audios or ()),
        videos=tuple(_owned_video(video) for video in result.videos or ()),
        files=tuple(_owned_file(file) for file in result.files or ()),
    )


def _media_id(media_id: str | None) -> dict[str, str]:
    # Agno assigns an id while validating; media built around validation has none, and the owned default stands.
    return {"id": media_id} if media_id is not None else {}


def _filepath(filepath: object, content: object) -> str | None:
    # Bytes the tool returned stand, so a path that worker code can later swap for a link is never read in their place.
    return None if filepath is None or content is not None else str(filepath)


def _owned_image(image: AgnoImage) -> Image:
    return Image(
        **_media_id(image.id),
        url=image.url,
        filepath=_filepath(image.filepath, image.content),
        content=image.content,
        format=image.format,
        mime_type=image.mime_type,
        detail=image.detail,
        original_prompt=image.original_prompt,
        revised_prompt=image.revised_prompt,
        alt_text=image.alt_text,
        metadata=image.metadata,
    )


def _owned_audio(audio: AgnoAudio) -> Audio:
    return Audio(
        **_media_id(audio.id),
        url=audio.url,
        filepath=_filepath(audio.filepath, audio.content),
        content=audio.content,
        format=audio.format,
        mime_type=audio.mime_type,
        duration=audio.duration,
        sample_rate=audio.sample_rate,
        channels=audio.channels,
        transcript=audio.transcript,
        expires_at=audio.expires_at,
        metadata=audio.metadata,
    )


def _owned_video(video: AgnoVideo) -> Video:
    return Video(
        **_media_id(video.id),
        url=video.url,
        filepath=_filepath(video.filepath, video.content),
        content=video.content,
        format=video.format,
        mime_type=video.mime_type,
        duration=video.duration,
        width=video.width,
        height=video.height,
        fps=video.fps,
        eta=video.eta,
        original_prompt=video.original_prompt,
        revised_prompt=video.revised_prompt,
        metadata=video.metadata,
    )


def _owned_file(file: AgnoFile) -> File:
    content = file.content if isinstance(file.content, bytes | str) else None
    owned = File(
        **_media_id(file.id),
        url=file.url,
        filepath=_filepath(file.filepath, content),
        content=content,
        mime_type=file.mime_type,
        file_type=file.file_type,
        filename=file.filename,
        size=file.size,
        format=file.format,
        name=file.name,
        metadata=file.metadata,
    )
    if file.filepath is None or owned.filepath is not None:
        return owned
    # The bytes stand in for the path, unread, so the file keeps the name and type the path gives it.
    return named_by_resource(owned, Path(file.filepath).name)


__all__ = [
    "AnyToolkit",
    "Function",
    "FunctionScopedInstructions",
    "StreamCollector",
    "ToolCallActor",
    "Toolkit",
    "adapt_callable",
    "adapt_toolkit",
    "configure_toolkit_functions",
    "framework_arguments",
    "injected_parameters",
    "model_arguments",
    "offered_refusal",
    "read_tool_value",
    "rename_toolkit_functions",
    "requires_connect",
    "toolkit_function",
    "toolkit_function_names",
    "warm_agno_tool_imports",
    "with_offered_instructions",
]
