# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The tool side of the loop: the tools a run offers, and one assistant message's tool calls planned, run, and answered.

Resolution, the tool-call limit, pause classification, result messages, and the media follow-up reproduce what
Agno 3.0.9's model loop did, so stored tool messages and tool executions keep their shapes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message
from mindroom.llm.runs import ToolExecution
from mindroom.llm.usage import ToolCallMetrics
from mindroom.logging_config import get_logger
from mindroom.tool_system.invocation import (
    MISSING_TOOL_ERROR_TEXT,
    ToolArgumentError,
    execute_tool_call,
    parse_tool_arguments,
)
from mindroom.tool_system.tool_function import first_of_each_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from mindroom.agent_loop.spec import AgentSpec, RunContext
    from mindroom.llm.provider import ToolSpec
    from mindroom.tool_system.invocation import ToolMiddleware, ToolOutcome
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolValue

logger = get_logger(__name__)

# Agno's texts: the model reads the first for a call past the limit and the second for a result that carries media.
_LIMIT_ERROR_TEXT = "Tool call limit reached. Tool call {name} not executed. Don't try to execute it again."
MEDIA_FOLLOW_UP_TEXT = "The tool call above generated the attached media."


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedTools:
    """The tools one run offers: its functions by name in offer order, and the toolkit instructions for its prompt."""

    functions: Mapping[str, ToolFunction]
    instructions: tuple[str, ...]

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return the model-facing definitions sorted by name, the order Agno sent them in for prompt-cache stability."""
        return tuple(sorted((function.definition() for function in self.functions.values()), key=lambda s: s.name))


def offered_functions(spec: AgentSpec) -> Iterator[tuple[AdaptedToolkit, ToolFunction]]:
    """Yield each function a run of ``spec`` offers, in offer order, with the toolkit it belongs to.

    The functions are those ``first_of_each_name`` selects over the toolkits; each one it drops is logged.
    """

    def log_dropped(toolkit: AdaptedToolkit, function: ToolFunction) -> None:
        logger.warning("duplicate_tool_name", agent=spec.id, tool_name=function.name, toolkit=toolkit.name)

    return first_of_each_name(
        ((toolkit, function) for toolkit in spec.toolkits for function in toolkit.functions),
        log_dropped,
    )


def prepare_run_tools(spec: AgentSpec) -> PreparedTools:
    """Flatten the agent's toolkits into the functions a run offers, as ``offered_functions`` selects them.

    A toolkit's instructions are kept whenever Agno would have added them to the prompt, once per toolkit identity,
    which Agno keyed by name, instructions, and function surface.
    """
    functions = {function.name: function for _toolkit, function in offered_functions(spec)}
    instructions: dict[tuple[str, str, frozenset[str]], str] = {}
    for toolkit in spec.toolkits:
        if toolkit.instructions is not None:
            key = (toolkit.name, toolkit.instructions, frozenset(function.name for function in toolkit.functions))
            instructions.setdefault(key, toolkit.instructions)
    return PreparedTools(functions=functions, instructions=tuple(instructions.values()))


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedCall:
    """One tool call the loop admitted: it runs now unless its ``execution`` is paused for a decision.

    ``arguments`` is None when the model sent ``[]``; ``execution`` is the run record's entry for the call, which
    the dispatcher fills in place.
    """

    call_id: str | None
    function: ToolFunction
    arguments: dict[str, Any] | None
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
    tool_call_limit: int | None,
) -> ToolBatch:
    """Resolve an assistant message's tool calls, apply the limit, and classify the admitted ones.

    A call naming no offered function, or whose arguments cannot be decoded, is answered with an error tool message
    appended to ``messages`` right away and never counts toward the limit. Of the rest, each call past
    ``tool_call_limit`` gets a limit-error result instead of running. An admitted call whose function requires
    confirmation or external execution is planned paused, with the flags its stored execution carries.
    """
    planned: list[PlannedCall] = []
    refused: list[Message] = []
    count = calls_so_far
    for tool_call in tool_calls:
        call_id = tool_call.get("id")
        function_data = tool_call.get("function") or {}
        name = function_data.get("name")
        function = functions.get(name) if isinstance(name, str) else None
        if function is None:
            messages.append(Message(role="tool", tool_call_id=call_id, tool_name=name, content=MISSING_TOOL_ERROR_TEXT))
            continue
        arguments = parse_tool_arguments(function_data.get("arguments"), function)
        if isinstance(arguments, ToolArgumentError):
            messages.append(Message(role="tool", tool_call_id=call_id, tool_name=name, content=arguments.error_text))
            continue
        if tool_call_limit is not None:
            count += 1
            if count > tool_call_limit:
                refused.append(
                    Message(
                        role="tool",
                        content=_LIMIT_ERROR_TEXT.format(name=function.name),
                        tool_call_id=call_id,
                        tool_name=function.name,
                        tool_args=arguments,
                        tool_call_error=True,
                    ),
                )
                continue
        execution = _planned_execution(call_id, function, arguments)
        planned.append(PlannedCall(call_id=call_id, function=function, arguments=arguments, execution=execution))
    return ToolBatch(planned=tuple(planned), refused=tuple(refused))


def _planned_execution(call_id: str | None, function: ToolFunction, arguments: dict[str, Any] | None) -> ToolExecution:
    execution = ToolExecution(tool_call_id=call_id, tool_name=function.name, tool_args=arguments)
    if function.requires_confirmation:
        execution.requires_confirmation = True
    if function.external_execution:
        execution.external_execution_required = True
    if execution.is_paused:
        execution.approval_type = function.approval_type
        execution.external_execution_silent = function.external_execution_silent
    return execution


async def dispatch_tool_calls(
    calls: Sequence[PlannedCall],
    *,
    run: RunContext,
    middleware: Sequence[ToolMiddleware] = (),
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
        call.arguments,
        run.call_context(call.call_id),
        middleware=middleware,
    )
    return outcome, time.perf_counter() - started


def _complete_call(call: PlannedCall, outcome: ToolOutcome, elapsed: float) -> Message:
    execution = call.execution
    execution.result = outcome.content
    execution.tool_call_error = outcome.is_error
    execution.stop_after_tool_call = outcome.stop_after_call
    ended = time.time()
    execution.metrics = ToolCallMetrics(start_time=ended - elapsed, end_time=ended, duration=elapsed)
    return tool_result_message(execution, media=None if outcome.is_error else outcome)


def tool_result_message(execution: ToolExecution, media: ToolValue | None = None) -> Message:
    """Return the tool message that answers ``execution`` with its recorded result, as Agno built it.

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
        tool_call_error=execution.tool_call_error,
        stop_after_tool_call=execution.stop_after_tool_call,
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
    "media_follow_up",
    "offered_functions",
    "plan_tool_batch",
    "prepare_run_tools",
    "tool_result_message",
]
