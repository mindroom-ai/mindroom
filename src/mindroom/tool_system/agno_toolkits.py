"""Adapt Agno toolkits to ``ToolFunction``, and give their entrypoints what they expect from Agno at call time.

Agno stays the tool authoring library: tools subclass ``agno.tools.Toolkit``, and Agno's public ``process_entrypoint``
derives each schema and wraps each entrypoint in pydantic coercion.
This module is where the owned loop meets those objects, in both directions: it turns a toolkit's functions into
``ToolFunction`` values once, and at call time it builds the Agno-shaped arguments an entrypoint declares and reads
the Agno ``ToolResult`` it returns back into owned values.
Those owned values, ``ToolFunction``, ``AdaptedToolkit``, and ``ToolValue``, live in ``tool_function``, so a module
that only handles them imports no Agno module.
It is also the one place MindRoom modules outside the tool and toolkit packages import Agno's tool types from.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_type_hints

from agno.media import Audio as AgnoAudio
from agno.media import File as AgnoFile
from agno.media import Image as AgnoImage
from agno.media import Video as AgnoVideo
from agno.run import RunContext
from agno.tools import Toolkit
from agno.tools.function import Function, ToolResult
from agno.utils.schema import (
    AGNO_INJECTED_PARAMS,
    FRAMEWORK_INJECTED_PARAMS,
    IDENTITY_INJECTED_PARAMS,
    MEDIA_INJECTED_PARAMS,
    annotation_binds,
    is_framework_typed,
    unwrap_annotation,
)

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.logging_config import get_logger
from mindroom.tool_system.declarations import finished_tool_schema
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.tool_system.invocation import ToolCallContext

logger = get_logger(__name__)

# Agno's default function schema: entrypoint processing replaces it with the schema derived from the signature and
# keeps any other schema the tool authored.
AGNO_EMPTY_PARAMETERS: Mapping[str, Any] = {"type": "object", "properties": {}, "required": []}

# Agno injects ``fc`` (its FunctionCall) by name beside the reserved framework names; the owned executor has no such
# object, so the parameter keeps its default, but the model never fills it either way.
_RESERVED_NAMES = frozenset((*FRAMEWORK_INJECTED_PARAMS, *AGNO_INJECTED_PARAMS, "fc"))
_UNBINDABLE_KINDS = frozenset(
    (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY),
)


def adapt_toolkit(toolkit: Toolkit) -> AdaptedToolkit:
    """Adapt every function of ``toolkit`` in the order Agno prepares them for an async run."""
    functions = toolkit.get_async_functions()
    added = toolkit.add_instructions and toolkit.instructions is not None
    return AdaptedToolkit(
        name=toolkit.name,
        functions=tuple(adapt_function(function) for function in functions.values()),
        instructions=toolkit.instructions if added else None,
        guidance=toolkit.instructions or None,
    )


def adapt_function(function: Function) -> ToolFunction:
    """Process one Agno function once, on a copy, and return it as a ``ToolFunction`` with its finished schema.

    ``process_entrypoint`` rewrites a user-authored schema in place and replaces the entrypoint with its coercing
    wrapper, so the shared ``Function`` stays untouched for whoever else holds it. A MindRoom wrapper that forges its
    entrypoint's signature declares how the schema Agno derives from it is completed.
    """
    if function.entrypoint is None:
        msg = f"Tool function {function.name!r} has no entrypoint"
        raise ValueError(msg)
    reject_agno_hooks(function)
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
        stop_after_tool_call=prepared.stop_after_tool_call,
        entrypoint=prepared.entrypoint,
        injected_params=injected_parameter_names(function.entrypoint),
        entrypoint_processed=not function.skip_entrypoint_processing,
    )


def reject_agno_hooks(function: Function) -> None:
    """Raise when ``function`` sets an Agno ``pre_hook``, ``post_hook``, or ``tool_hooks``, which MindRoom does not run.

    Rejected rather than dropped: such a hook may be a policy gate the tool's author relies on.
    """
    if function.pre_hook is not None or function.post_hook is not None or function.tool_hooks:
        msg = (
            f"Tool function {function.name!r} sets an Agno pre_hook, post_hook, or tool_hooks, which MindRoom does not"
            " run; move that logic into the tool or a `tool:before_call` / `tool:after_call` hook"
        )
        raise ValueError(msg)


def injected_parameter_names(entrypoint: Callable[..., Any]) -> frozenset[str]:
    """Return the entrypoint parameters Agno supplies instead of the model: reserved names and identity-typed ones."""
    parameters = inspect.signature(entrypoint).parameters
    names = {name for name in parameters if name in _RESERVED_NAMES}
    try:
        hints = get_type_hints(entrypoint)
    except Exception:
        # Agno falls back to the reserved names when a hint does not resolve, such as an unimportable Agent.
        return frozenset(names)
    for name, hint in hints.items():
        parameter = parameters.get(name)
        if parameter is None or parameter.kind in _UNBINDABLE_KINDS or name in names:
            continue
        try:
            owned = is_framework_typed(hint)
        except Exception:
            owned = True  # Agno treats an unclassifiable annotation as framework-owned.
        if owned:
            names.add(name)
    return frozenset(names)


# AGNO_COMPAT: Agno toolkits take the calling agent as a parameter.
# Reason: Agno injects its Agent or Team object into entrypoint parameters named ``agent`` or ``team``, or annotated
# with those types, and the owned loop has no Agno agent to inject. Of the toolkits MindRoom registers, only
# DaytonaTools reads the parameter, and it reads and writes ``agent.session_state``, as Agno's AntigravityTools and
# SuperserveTools also do. MindRoom's own tools read their caller from the tool runtime context.
# Upstream issue: Tracking gap; Agno defines no contract for what a toolkit may read from the injected agent, so a
# toolkit can depend on any Agent attribute.
# Upstream PR: None identified.
# Remove when: Agno toolkits read run state from ``run_context`` instead of the agent parameter, or MindRoom drops
# every toolkit that reads the agent parameter; the injected ``run_context`` stays.
# Coverage: tests/tool_system/test_tool_invocation.py::test_agent_and_team_parameters_receive_the_session_state_stand_in;
# tests/tool_system/test_tool_invocation.py::test_identity_typed_parameters_are_injected_by_annotation.
@dataclass(slots=True)
class ToolCallActor:
    """What an Agno toolkit may read from its injected ``agent`` or ``team``: the run's session state."""

    session_state: dict[str, Any] | None


def framework_arguments(function: ToolFunction, context: ToolCallContext) -> dict[str, Any]:
    """Return the framework-supplied arguments of one call, keyed by the entrypoint parameters that take them."""
    if not function.injected_params:
        return {}
    run_context = RunContext(
        run_id=context.run_id,
        session_id=context.session_id,
        user_id=context.user_id,
        metadata=context.metadata,
        session_state=context.session_state,
    )
    actor = ToolCallActor(session_state=context.session_state)
    by_name: dict[str, Any] = {
        "agent": actor,
        "team": actor,
        "run_context": run_context,
        "_agno_agent": actor,
        "_agno_team": actor,
        "_agno_run_context": run_context,
        # Agno passed the run's input media to these names; the owned loop gives a tool none, so each receives None,
        # as under Agno for a run without media.
        **dict.fromkeys(MEDIA_INJECTED_PARAMS),
    }
    arguments = {name: by_name[name] for name in function.injected_params if name in by_name}
    typed = function.injected_params - set(by_name) - {"fc"}
    if not typed:
        return arguments
    # Agno binds identity objects by annotation too (``ctx: RunContext``, ``owner: Agent``), unions included.
    from agno.agent import Agent  # noqa: PLC0415 - Only identity-typed parameters need Agno's agent types.
    from agno.team import Team  # noqa: PLC0415

    hints = get_type_hints(function.entrypoint)
    for name in typed:
        hint = unwrap_annotation(hints[name])
        if annotation_binds(hint, (Agent, Team)):
            arguments[name] = actor
        elif annotation_binds(hint, (RunContext,)):
            arguments[name] = run_context
    return arguments


def warm_agno_tool_imports() -> None:
    """Import the agent and team types Agno resolves while it builds tool schemas, so later builds skip the cost."""
    from agno.agent import Agent  # noqa: PLC0415, F401 - Imported for its side effect of loading the module.
    from agno.team import Team  # noqa: PLC0415, F401


def model_arguments(arguments: Mapping[str, Any], function: ToolFunction) -> dict[str, Any]:
    """Drop model-sent values for parameters the framework supplies, as Agno does before a call runs.

    A media name the schema declares stays the model's, because a wrapper may expose a wrapped tool's own ``files``
    argument; identity names never do, whatever the schema says.
    """
    properties = function.parameters.get("properties")
    declared = set(properties) if isinstance(properties, dict) else set()
    identity = set(IDENTITY_INJECTED_PARAMS) | (function.injected_params - set(FRAMEWORK_INJECTED_PARAMS))
    dropped = {
        name
        for name in arguments
        if name in AGNO_INJECTED_PARAMS
        or (name in function.injected_params and (name not in declared or name in identity))
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
    """Accumulate what a generator tool yields the way Agno's loop reads it: text joins, ``ToolResult`` media merge."""

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
    """Read a tool's return value as Agno's loop does: a ``ToolResult`` carries text and media, anything else is text.

    A value already in owned terms, as a tool job's saved result or minimal mode's Bash facade returns, stands as it is.
    """
    if isinstance(result, ToolValue):
        return result
    if not isinstance(result, ToolResult):
        return ToolValue(content=str(result))
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


def _filepath(filepath: object) -> str | None:
    return None if filepath is None else str(filepath)


def _owned_image(image: AgnoImage) -> Image:
    return Image(
        **_media_id(image.id),
        url=image.url,
        filepath=_filepath(image.filepath),
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
        filepath=_filepath(audio.filepath),
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
        filepath=_filepath(video.filepath),
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
        filepath=_filepath(file.filepath),
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
    "StreamCollector",
    "ToolCallActor",
    "ToolResult",
    "Toolkit",
    "adapt_function",
    "adapt_toolkit",
    "framework_arguments",
    "injected_parameter_names",
    "model_arguments",
    "reject_agno_hooks",
    "tool_value",
    "warm_agno_tool_imports",
]
