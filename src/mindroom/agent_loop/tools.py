# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The tool side of the loop: the tools a run offers, and one assistant message's tool calls planned, run, and answered.

Tool messages, tool executions, and the texts the model reads keep the shapes main stores and sends.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message
from mindroom.llm.usage import ToolCallMetrics
from mindroom.logging_config import get_logger
from mindroom.tool_system.invocation import (
    MISSING_TOOL_ERROR_TEXT,
    ToolArgumentError,
    execute_tool_call,
    parse_tool_arguments,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from mindroom.agent_loop.spec import RunContext
    from mindroom.llm.provider import ToolSpec
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.invocation import ToolMiddleware, ToolOutcome
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolValue

logger = get_logger(__name__)

# The model reads the first for a call past the limit and the second for a result that carries media.
_LIMIT_ERROR_TEXT = "Tool call limit reached. Tool call {name} not executed. Don't try to execute it again."
MEDIA_FOLLOW_UP_TEXT = "The tool call above generated the attached media."


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedTools:
    """The tools one run offers: its functions by name in offer order."""

    functions: Mapping[str, ToolFunction]

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return the model-facing definitions in the order a request sends them (``sorted_specs``)."""
        return sorted_specs(function.definition() for function in self.functions.values())


def sorted_specs(definitions: Iterable[ToolSpec]) -> tuple[ToolSpec, ...]:
    """Return ``definitions`` sorted by name, the order a request sends them in, which is stable for prompt caches."""
    return tuple(sorted(definitions, key=lambda definition: definition.name))


def offered_toolkits(toolkits: Sequence[AdaptedToolkit], *, agent: str) -> tuple[AdaptedToolkit, ...]:
    """Return ``toolkits``, each offering only the functions a run of ``agent`` offers of it, in the same order.

    A run offers the first function of each name, so a build resolves its toolkits once: each later function of a
    taken name is dropped and logged here, and a toolkit whose every function is taken stays, offering none.
    """
    taken: set[str] = set()
    offered: list[AdaptedToolkit] = []
    for toolkit in toolkits:
        functions: list[ToolFunction] = []
        for function in toolkit.functions:
            if function.name in taken:
                logger.warning("duplicate_tool_name", agent=agent, tool_name=function.name, toolkit=toolkit.name)
                continue
            taken.add(function.name)
            functions.append(function)
        offered.append(replace(toolkit, functions=tuple(functions)))
    return tuple(offered)


def toolkit_instructions(toolkits: Iterable[AdaptedToolkit]) -> tuple[str, ...]:
    """Return the instructions of ``toolkits`` the prompt shows, in offer order.

    Each toolkit's instructions are kept once per toolkit identity: its name, its instructions, and the functions it
    was built with, whichever of them a run offers.
    """
    instructions: dict[tuple[str, str, frozenset[str]], str] = {}
    for toolkit in toolkits:
        if toolkit.instructions is not None:
            key = (toolkit.name, toolkit.instructions, frozenset(function.name for function in toolkit.functions))
            instructions.setdefault(key, toolkit.instructions)
    return tuple(instructions.values())


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedCall:
    """One tool call the loop admitted: it runs now unless its ``execution`` is paused for a decision.

    ``execution`` is the run record's entry for the call, holding its id and arguments (None when the model sent
    ``[]``), which the dispatcher fills in place.
    """

    function: ToolFunction
    execution: ToolExecution


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolBatch:
    """One assistant message's tool calls after resolution and limit accounting.

    ``refused`` holds the limit-error results, which precede the executed results in the batch's messages; they and
    the executed results, not the paused calls, count toward the run's tool calls.
    """

    planned: tuple[PlannedCall, ...]
    refused: tuple[Message, ...]

    @property
    def paused(self) -> bool:
        """Whether any admitted call waits for a decision, which ends the loop after this batch."""
        return any(call.execution.is_paused for call in self.planned)


def plan_tool_batch(
    tool_calls: Sequence[Mapping[str, Any]],
    functions: Mapping[str, ToolFunction],
    messages: list[Message],
    *,
    calls_so_far: int,
    max_tool_calls_per_turn: int | None,
) -> ToolBatch:
    """Resolve an assistant message's tool calls, apply the limit, and classify the admitted ones.

    A call naming no offered function, or whose arguments cannot be decoded, is answered with an error tool message
    appended to ``messages`` right away and never counts toward the limit. Of the rest, each call past
    ``max_tool_calls_per_turn`` gets a limit-error result instead of running. An admitted call whose function
    requires confirmation or external execution is planned paused, with the flags its stored execution carries.
    """
    planned: list[PlannedCall] = []
    refused: list[Message] = []
    count = calls_so_far
    for tool_call in tool_calls:
        call_id = tool_call["id"]
        function_data = tool_call.get("function") or {}
        name = function_data.get("name")
        function = functions.get(name) if isinstance(name, str) else None
        if function is None:
            messages.append(failed_answer(call_id, name, MISSING_TOOL_ERROR_TEXT))
            continue
        arguments = parse_tool_arguments(function_data.get("arguments"), function)
        if isinstance(arguments, ToolArgumentError):
            messages.append(failed_answer(call_id, name, arguments.error_text))
            continue
        if max_tool_calls_per_turn is not None:
            count += 1
            if count > max_tool_calls_per_turn:
                limit_text = _LIMIT_ERROR_TEXT.format(name=function.name)
                refused.append(failed_answer(call_id, function.name, limit_text, tool_args=arguments))
                continue
        execution = function.planned_execution(call_id, arguments)
        planned.append(PlannedCall(function=function, execution=execution))
    return ToolBatch(planned=tuple(planned), refused=tuple(refused))


def failed_answer(
    call_id: str | None,
    name: str | None,
    content: str,
    *,
    tool_args: dict[str, Any] | None = None,
    stop_after_tool_call: bool = False,
) -> Message:
    """Return the tool message that answers a call the loop did not run, marked failed so Claude and Gemini read it as one.

    The loop answers this way a call to a tool it does not offer, one whose arguments cannot be decoded, one past the
    per-turn limit, one the output limit cut off, and one whose approval was denied.
    """
    return Message(
        role="tool",
        tool_call_id=call_id,
        tool_name=name,
        content=content,
        tool_args=tool_args,
        tool_call_error=True,
        stop_after_tool_call=stop_after_tool_call,
    )


async def dispatch_tool_calls(
    calls: Sequence[PlannedCall],
    *,
    run: RunContext,
    middleware: Sequence[ToolMiddleware],
) -> list[Message]:
    """Run the batch's unpaused calls concurrently and return their result messages in call order.

    Each call's execution is filled in place with its result, error flag, stop flag, and timing. A tool's exception
    is its error text; only cancellation propagates, and no call of the batch is still running when it does: a
    cancelled dispatcher cancels every call and waits for it, and a call cancelled on its own lets its siblings
    finish first, so nothing a call does lands after the run stores its terminal record.
    """
    runnable = [call for call in calls if not call.execution.is_paused]
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(_timed(call, run, middleware)) for call in runnable]
    return [_complete_call(call, *task.result()) for call, task in zip(runnable, tasks, strict=True)]


async def _timed(call: PlannedCall, run: RunContext, middleware: Sequence[ToolMiddleware]) -> tuple[ToolOutcome, float]:
    started = time.perf_counter()
    outcome = await execute_tool_call(
        call.function,
        call.execution.tool_args,
        run.call_context(call.execution.tool_call_id),
        middleware=middleware,
    )
    return outcome, time.perf_counter() - started


def _complete_call(call: PlannedCall, outcome: ToolOutcome, elapsed: float) -> Message:
    execution = call.execution
    execution.result = outcome.content
    execution.is_error = outcome.is_error
    execution.stop_after_call = outcome.stop_after_call
    ended = time.time()
    execution.metrics = ToolCallMetrics(start_time=ended - elapsed, end_time=ended, duration=elapsed)
    return tool_result_message(execution, media=None if outcome.is_error else outcome)


def tool_result_message(execution: ToolExecution, media: ToolValue | None = None) -> Message:
    """Return the tool message that answers ``execution`` with its recorded result.

    ``media`` is the successful tool value whose media the message carries; a failed call carries none.
    """
    images = audio = videos = files = None
    if media is not None:
        images, audio, videos, files = (
            list(items) or None for items in (media.images, media.audios, media.videos, media.files)
        )
    return Message(
        role="tool",
        content=execution.result,
        tool_call_id=execution.tool_call_id,
        tool_name=execution.tool_name,
        tool_args=execution.tool_args,
        tool_call_error=execution.is_error,
        stop_after_tool_call=execution.stop_after_call,
        images=images,
        audio=audio,
        videos=videos,
        files=files,
    )


def media_follow_up(results: Sequence[Message]) -> Message | None:
    """Move the media of a batch's results onto one user message, since tool messages cannot carry media on every wire."""
    images = [item for message in results for item in message.images or ()]
    videos = [item for message in results for item in message.videos or ()]
    audio = [item for message in results for item in message.audio or ()]
    files = [item for message in results for item in message.files or ()]
    if not (images or videos or audio or files):
        return None
    for message in results:
        message.images = message.videos = message.audio = message.files = None
    return Message(
        role="user",
        content=MEDIA_FOLLOW_UP_TEXT,
        images=images or None,
        videos=videos or None,
        audio=audio or None,
        files=files or None,
    )


__all__ = [
    "MEDIA_FOLLOW_UP_TEXT",
    "PlannedCall",
    "PreparedTools",
    "ToolBatch",
    "dispatch_tool_calls",
    "failed_answer",
    "media_follow_up",
    "offered_toolkits",
    "plan_tool_batch",
    "sorted_specs",
    "tool_result_message",
    "toolkit_instructions",
]
