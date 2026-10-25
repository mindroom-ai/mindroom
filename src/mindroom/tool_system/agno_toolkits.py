"""Adapt Agno toolkits to ``ToolFunction``, and give their entrypoints what they expect from Agno at call time.

Agno stays the tool authoring library: tools subclass ``agno.tools.Toolkit``, and Agno's public ``process_entrypoint``
derives each schema and wraps each entrypoint in pydantic coercion.
At call time this module builds the Agno-shaped arguments an entrypoint declares and reads the ``ToolResult`` it
returns as owned values, which live in ``tool_function`` so a module that only handles them imports no Agno module.
MindRoom modules outside the tool and toolkit packages import Agno's tool types only from here.
"""

from __future__ import annotations

import inspect
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_type_hints, runtime_checkable

from agno.media import Audio as AgnoAudio
from agno.media import File as AgnoFile
from agno.media import Image as AgnoImage
from agno.media import Video as AgnoVideo
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

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.logging_config import get_logger
from mindroom.tool_system.declarations import finished_tool_schema
from mindroom.tool_system.tool_function import AdaptedToolkit, InjectedValue, ToolFunction, ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from mindroom.tool_system.invocation import ToolCallContext

logger = get_logger(__name__)

# Agno's default function schema: entrypoint processing replaces it with the schema derived from the signature and
# keeps any other schema the tool authored.
AGNO_EMPTY_PARAMETERS: Mapping[str, Any] = {"type": "object", "properties": {}, "required": []}

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


@dataclass(frozen=True, slots=True)
class _Rejected:
    """Fields whose value the owned loop cannot honor: a function for which ``sets`` holds is refused with ``error``.

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
    # Wrapped in Agno's coercion unless processing is skipped; the signature names the injected parameters.
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
    # A toolkit's adapter hands them over as ``function_instructions`` instead, which tool search shows for a
    # deferred toolkit and an agent refuses in an eager one.
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


@runtime_checkable
class FunctionScopedInstructions(Protocol):
    """A toolkit whose instructions describe its functions, so they name only the ones an agent is offered."""

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return the toolkit's instructions for an agent offered ``function_names`` of it."""
        ...


def adapt_toolkit(toolkit: Toolkit, *, key: str | None = None, registered: str | None = None) -> AdaptedToolkit:
    """Adapt the functions of ``toolkit`` in Agno's async-run order, one per name; they share one ``actor_state``.

    ``key`` is the authored toolkit an agent is offered the functions from, which each one carries as its
    ``toolkit``; None for a toolkit generated for the agent, and for a caller that names the toolkit beside each
    function, as the MCP gateway's search does. ``registered`` is the registered tool they come from, which each
    one carries as its ``registered_toolkit``; it is ``key`` unless an implied or preset tool brings the toolkit in.
    A function's prompt instructions are handed over as ``function_instructions``, and a function that sets any
    other field MindRoom refuses is adapted without it and named in ``refused``, so whoever offers the toolkit
    refuses it only for a function it still offers.
    """
    functions = toolkit.get_async_functions().values()
    actor_state: dict[str, Any] = {}
    adapted: list[ToolFunction] = []
    refused: dict[str, str] = {}
    for function in functions:
        offered = _without_instructions(function)
        if (refusal := _refusal(offered)) is not None:
            refused[function.name] = refusal
        adapted.append(
            replace(_adapt(offered), toolkit=key, registered_toolkit=registered or key, actor_state=actor_state),
        )
    instructions, guidance = _prompt_and_guidance(toolkit, toolkit.instructions)
    return AdaptedToolkit(
        name=toolkit.name,
        functions=tuple(adapted),
        instructions=instructions,
        guidance=guidance,
        function_instructions={
            function.name: function.instructions
            for function in functions
            if _FUNCTION_INSTRUCTIONS.sets(function) and function.instructions is not None
        },
        refused=refused,
    )


def with_offered_instructions(toolkit: Toolkit, adapted: AdaptedToolkit) -> AdaptedToolkit:
    """Return ``adapted``, the functions an agent offers of ``toolkit``, with the instructions ``toolkit`` gives for them.

    Only a toolkit whose instructions describe its functions (``FunctionScopedInstructions``) gives other ones.
    """
    if not isinstance(toolkit, FunctionScopedInstructions):
        return adapted
    rendered = toolkit.instructions_for([function.name for function in adapted.functions])
    instructions, guidance = _prompt_and_guidance(toolkit, rendered)
    return replace(adapted, instructions=instructions, guidance=guidance)


def _prompt_and_guidance(toolkit: Toolkit, instructions: str | None) -> tuple[str | None, str | None]:
    """Return what the prompt shows of ``instructions``, only when ``add_instructions`` asks, and the guidance."""
    return (instructions if toolkit.add_instructions else None), instructions or None


def offered_refusal(toolkit: AdaptedToolkit) -> str | None:
    """Return why an agent cannot offer ``toolkit``: the first function it offers that sets a field MindRoom refuses.

    A function's prompt instructions count only while ``function_instructions`` holds them, which a deferred toolkit
    hands to tool search instead.
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


def adapt_function(function: Function) -> ToolFunction:
    """Return one Agno function as a ``ToolFunction``, refusing it when it sets a field MindRoom refuses."""
    if (refusal := _refusal(function)) is not None:
        raise ValueError(refusal)
    return _adapt(function)


def _refusal(function: Function) -> str | None:
    """Return why MindRoom refuses ``function``: the first field it sets that the owned loop cannot honor."""
    for handling in _FUNCTION_FIELDS.values():
        if isinstance(handling, _Rejected) and handling.sets(function):
            return f"Tool function {function.name!r} {handling.error}"
    return None


def _adapt(function: Function) -> ToolFunction:
    """Process one Agno function once, on a copy, and return it as a ``ToolFunction`` with its finished schema.

    ``process_entrypoint`` rewrites a user-authored schema in place and replaces the entrypoint with its coercing
    wrapper, so the shared ``Function`` stays untouched for whoever else holds it. A MindRoom wrapper that forges its
    entrypoint's signature declares how the schema Agno derives from it is completed. A field MindRoom refuses is
    left unread.
    """
    if function.entrypoint is None:
        msg = f"Tool function {function.name!r} has no entrypoint"
        raise ValueError(msg)
    prepared = function.model_copy(deep=False)
    prepared.parameters = deepcopy(function.parameters)
    # An explicit strict setting on the function wins; MindRoom never asks Agno for strict schemas otherwise.
    prepared.process_entrypoint(strict=bool(function.strict))
    assert prepared.entrypoint is not None
    return ToolFunction(
        name=prepared.name,
        description=prepared.description,
        parameters=finished_tool_schema(function.entrypoint, prepared.parameters),
        strict=prepared.strict,
        requires_confirmation=prepared.requires_confirmation,
        approval_type=prepared.approval_type,
        external_execution=prepared.external_execution,
        external_execution_silent=prepared.external_execution_silent,
        stop_after_call=prepared.stop_after_tool_call,
        entrypoint=prepared.entrypoint,
        injected_params=injected_parameters(function.entrypoint),
        entrypoint_processed=not function.skip_entrypoint_processing,
    )


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
# with those types, and the owned loop has no Agno agent to inject. Of the toolkits MindRoom registers, only
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
        # The owned loop gives a tool no run media, so each media name receives None.
        InjectedValue.MEDIA: None,
    }
    return {name: supplied[value] for name, value in function.injected_params.items() if value in supplied}


def warm_agno_tool_imports() -> None:
    """Import the agent and team types Agno resolves while it builds tool schemas, so later builds skip the cost."""
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
    """Accumulate what a generator tool yields: text joins, and ``ToolResult`` media and metadata merge."""

    _chunks: list[str] = field(default_factory=list)
    _merged: ToolResult | None = None

    def add(self, item: object) -> None:
        """Record one yielded item."""
        if not isinstance(item, ToolResult):
            self._chunks.append(str(item))
            return
        self._chunks.append(item.content)
        merged = self._merged if self._merged is not None else ToolResult(content="")
        merged.images = [*(merged.images or []), *(item.images or [])] or None
        merged.videos = [*(merged.videos or []), *(item.videos or [])] or None
        merged.audios = [*(merged.audios or []), *(item.audios or [])] or None
        merged.files = [*(merged.files or []), *(item.files or [])] or None
        merged.metadata = {**(merged.metadata or {}), **(item.metadata or {})} or None
        self._merged = merged

    def value(self) -> object:
        """Return the drained value: the joined text, carried by a ``ToolResult`` when any item had media."""
        text = "".join(self._chunks)
        if self._merged is None:
            return text
        self._merged.content = text
        return self._merged


def tool_value(result: object) -> ToolValue:
    """Read a tool's return value: a ``ToolResult`` or ``ToolValue`` gives its text and media, anything else its text."""
    value = read_tool_value(result)
    return ToolValue(content=str(result)) if value is None else value


def read_tool_value(result: object) -> ToolValue | None:
    """Read a ``ToolResult``'s text and media; None for a value that is neither a ``ToolResult`` nor a ``ToolValue``.

    A value already in owned terms, as a tool job's saved result or minimal mode's Bash facade returns, stands as it is.
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
    return File(
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


__all__ = [
    "AGNO_EMPTY_PARAMETERS",
    "AgnoAudio",
    "AgnoFile",
    "AgnoImage",
    "AgnoVideo",
    "Function",
    "FunctionScopedInstructions",
    "StreamCollector",
    "ToolCallActor",
    "ToolResult",
    "Toolkit",
    "adapt_function",
    "adapt_toolkit",
    "framework_arguments",
    "injected_parameters",
    "model_arguments",
    "offered_refusal",
    "read_tool_value",
    "tool_value",
    "warm_agno_tool_imports",
    "with_offered_instructions",
]
