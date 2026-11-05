# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The loop's tool side: the tools a run offers, and one assistant message's tool calls planned, run, and answered."""

from __future__ import annotations

import asyncio
import math
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.llm.events import ToolProgress
from mindroom.llm.messages import Message
from mindroom.llm.runs import FailingTool, ToolExecution
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import callback_event_stream, closing_async_stream
from mindroom.tool_system.invocation import (
    MISSING_TOOL_ERROR_TEXT,
    ToolArgumentError,
    execute_tool_call,
    parse_tool_arguments,
)
from mindroom.tool_system.progress import shown_progress, tool_progress_reporter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence

    from mindroom.agent_loop.spec import RunContext
    from mindroom.llm.provider import ToolSpec
    from mindroom.tool_system.invocation import ToolMiddleware
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolValue

logger = get_logger(__name__)

# The model reads the first for a call past the limit, the second for a call of a tool that keeps failing, and the
# third for a result that carries media.
_LIMIT_ERROR_TEXT = "Tool call limit reached. Tool call {name} not executed. Don't try to execute it again."
_FAILING_TOOL_ERROR_TEXT = (
    "Tool {name} failed in {failures} requests of this turn with no success in between, so this call was not executed. "
    "Its last error: {error}\nDon't call {name} again in this turn. Tell the user what failed instead."
)
MEDIA_FOLLOW_UP_TEXT = "The tool call above generated the attached media."
# The user reads this at the end of a reply whose run the loop ended because the model kept calling a failing tool.
_FAILING_TOOL_STOP_TEXT = (
    "Stopped: tool {name} failed in {failures} requests with no success in between. Its last error: {error}"
)
# A tool whose calls ran and failed in this many batches of one turn, with no success of it in between, is not run
# again in that turn; batches that did not call it leave its count alone. A model that keeps resending a call the tool
# rejects, or retrying an integration that is down, otherwise loops until the per-turn call limit, paying for a model
# request per attempt; a batch counts once, however many of its calls failed, since it cost one request.
MAX_CONSECUTIVE_TOOL_FAILURES = 8
# A failing tool keeps this much of its last error, redacted, for the refusal the model reads and the line that ends a
# reply in the room: enough to say what failed.
_MAX_QUOTED_ERROR = 500
# The model writes the name of a tool it was not offered, so the log keeps only this much of it.
_MAX_LOGGED_TOOL_NAME = 100
# Live frames go out ten times a second; reports a call makes between two of them would never be seen.
_PROGRESS_FLUSH_SECONDS = 0.1


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
    """Return the instructions the prompt shows of ``toolkits``, as ``offered_toolkits`` resolves them, in offer order.

    A toolkit's instructions show whenever it offers a function, so a toolkit whose every function an earlier one
    took shows none.
    """
    return tuple(toolkit.instructions for toolkit in toolkits if toolkit.functions and toolkit.instructions is not None)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedCall:
    """One tool call the loop admitted: it runs now unless its ``execution`` is paused for a decision.

    ``execution`` is the run record's entry for the call, holding its id and arguments (None when the model sent
    ``[]``), which the dispatcher fills in place.
    """

    function: ToolFunction
    execution: ToolExecution


@dataclass(frozen=True, slots=True, kw_only=True)
class RefusedCall:
    """One tool call the loop answered without running it: its run record entry and the failed answer it gets.

    ``counts`` says whether it counts toward the run's tool calls, as a limit refusal does; a call that names no
    offered function or whose arguments cannot be decoded does not.
    """

    execution: ToolExecution
    answer: Message
    counts: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolBatch:
    """One assistant message's tool calls after resolution and limit accounting, in the order the model made them.

    ``refused_for_failures`` names, once each, the tools whose calls this batch refused because they keep failing.
    """

    calls: tuple[PlannedCall | RefusedCall, ...]
    refused_for_failures: tuple[str, ...] = ()

    @property
    def planned(self) -> tuple[PlannedCall, ...]:
        """The admitted calls, in call order."""
        return tuple(call for call in self.calls if isinstance(call, PlannedCall))

    @property
    def paused(self) -> bool:
        """Whether any admitted call waits for a decision, which ends the loop after this batch."""
        return any(call.execution.is_paused for call in self.planned)

    def answers(self, executed: Sequence[Message]) -> list[Message]:
        """Return the batch's answers in call order: each refused call's, and each executed one's from ``executed``."""
        by_call = {message.tool_call_id: message for message in executed}
        return [
            call.answer if isinstance(call, RefusedCall) else by_call[call.execution.tool_call_id]
            for call in self.calls
            if isinstance(call, RefusedCall) or call.execution.tool_call_id in by_call
        ]


def count_tool_failures(failing_tools: dict[str, FailingTool], executions: Iterable[ToolExecution]) -> None:
    """Fold one batch's calls that ran into ``failing_tools``, the run's failing tools by name.

    A tool with a call of the batch that succeeded leaves ``failing_tools``. A tool with only failed calls in the batch
    counts one more failure, however many of its calls failed, and keeps the error of the last of them in call order,
    redacted and cut to ``_MAX_QUOTED_ERROR``. A call that has not finished, such as a paused one, changes nothing.

    Pass only calls that ran: a call the loop answered without running it (an unknown tool, undecodable arguments, the
    call limit, a cut-off response, a denied approval, a refusal for failing) says nothing about whether the tool works.
    A call its tool middleware answered with an error, such as a policy declining it, counts as failed, as the model
    reads it.
    """
    errors: dict[str, str | None] = {}
    succeeded: set[str] = set()
    for execution in executions:
        if execution.is_error:
            errors[execution.tool_name] = execution.result
        elif execution.is_error is False:
            succeeded.add(execution.tool_name)
    for name in succeeded:
        failing_tools.pop(name, None)
    for name, error in errors.items():
        if name not in succeeded:
            tool = failing_tools.setdefault(name, FailingTool())
            tool.failures += 1
            tool.last_error = None if error is None else redact_sensitive_text(error, max_length=_MAX_QUOTED_ERROR)


def count_tool_refusals(failing_tools: dict[str, FailingTool], batch: ToolBatch) -> None:
    """Count a refusal for each tool ``batch`` refused for failing; several of one tool in a batch count once."""
    for name in batch.refused_for_failures:
        failing_tools[name].refusals += 1


def ignored_refusal(failing_tools: Mapping[str, FailingTool]) -> str | None:
    """Return a tool refused for failing in two batches, if any: the model called it again after reading its refusal."""
    return next((name for name, tool in failing_tools.items() if tool.refusals > 1), None)


def failing_tool_stop_text(name: str, tool: FailingTool) -> str:
    """Return the line that ends a reply whose run the loop ended because the model kept calling ``name``."""
    return _FAILING_TOOL_STOP_TEXT.format(name=name, failures=tool.failures, error=tool.last_error or "unknown")


def plan_tool_batch(
    tool_calls: Sequence[Mapping[str, Any]],
    functions: Mapping[str, ToolFunction],
    *,
    calls_so_far: int,
    max_tool_calls_per_turn: int | None,
    entity: str | None,
    failing_tools: Mapping[str, FailingTool],
) -> ToolBatch:
    """Resolve an assistant message's tool calls in order, apply the limits, and classify the admitted ones.

    A call naming no offered function, or whose arguments cannot be decoded, is refused with an error answer, logged
    for ``entity`` without its arguments, and never counts toward the limit. Of the rest, each call past
    ``max_tool_calls_per_turn`` is refused with a limit error instead of running, and each call of a tool that
    ``failing_tools`` says failed in ``MAX_CONSECUTIVE_TOOL_FAILURES`` batches since its last success is refused with
    the tool's last error, logged, and named in the batch's ``refused_for_failures``. An admitted call whose function's
    gate pauses its run is planned paused, with the flags its stored execution carries.
    """
    refused_for_failures: dict[str, None] = {}
    calls: list[PlannedCall | RefusedCall] = []
    count = calls_so_far
    for tool_call in tool_calls:
        call_id = tool_call["id"]
        function_data = tool_call.get("function") or {}
        name = function_data.get("name")
        function = functions.get(name) if isinstance(name, str) else None
        if function is None:
            logger.warning("tool_not_offered", entity=entity, tool_name=str(name)[:_MAX_LOGGED_TOOL_NAME])
            calls.append(_refused(call_id, str(name), MISSING_TOOL_ERROR_TEXT, None, counts=False))
            continue
        arguments = parse_tool_arguments(function_data.get("arguments"), function)
        if isinstance(arguments, ToolArgumentError):
            logger.warning("invalid_tool_arguments", entity=entity, tool_name=name, reason=arguments.reason)
            calls.append(_refused(call_id, function.name, arguments.error_text, None, counts=False))
            continue
        if max_tool_calls_per_turn is not None:
            count += 1
            if count > max_tool_calls_per_turn:
                limit_text = _LIMIT_ERROR_TEXT.format(name=function.name)
                calls.append(_refused(call_id, function.name, limit_text, arguments, counts=True))
                continue
        tool = failing_tools.get(function.name)
        if tool is not None and tool.failures >= MAX_CONSECUTIVE_TOOL_FAILURES:
            logger.warning("failing_tool_call_refused", entity=entity, tool_name=function.name)
            text = _FAILING_TOOL_ERROR_TEXT.format(
                name=function.name,
                failures=tool.failures,
                error=tool.last_error or "unknown",
            )
            calls.append(_refused(call_id, function.name, text, arguments, counts=True))
            refused_for_failures[function.name] = None
            continue
        calls.append(PlannedCall(function=function, execution=function.planned_execution(call_id, arguments)))
    return ToolBatch(calls=tuple(calls), refused_for_failures=tuple(refused_for_failures))


def answered_tool_batch(answers: Sequence[Message]) -> ToolBatch:
    """Return the batch of calls a loop hook answered with ``answers``, one per call in call order, instead of running.

    Each call gets a failed run-record entry holding its answer, as a refused call does. Its arguments, which a cut-off
    response may hold only in part, are not recorded, and none counts toward the call limit.
    """
    return ToolBatch(
        calls=tuple(
            RefusedCall(
                execution=ToolExecution(
                    tool_call_id=answer.tool_call_id or "",
                    tool_name=answer.tool_name or "",
                    is_error=True,
                    result=answer.get_content_string(),
                ),
                answer=answer,
                counts=False,
            )
            for answer in answers
        ),
    )


def _refused(call_id: str, name: str, text: str, arguments: dict[str, Any] | None, *, counts: bool) -> RefusedCall:
    execution = ToolExecution(tool_call_id=call_id, tool_name=name, tool_args=arguments, is_error=True, result=text)
    return RefusedCall(
        execution=execution,
        answer=failed_answer(call_id, name, text, tool_args=arguments),
        counts=counts,
    )


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
    per-turn limit, one of a tool that keeps failing, one the output limit cut off, and one whose approval was denied.
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
) -> AsyncIterator[ToolProgress | tuple[PlannedCall, Message]]:
    """Run the batch's unpaused calls concurrently and yield each with its result message as soon as it finishes.

    The progress a running call reports comes as ``ToolProgress`` before that call's result.

    A call's execution is filled in place with its result, error flag, and stop flag as soon as that call finishes,
    whatever its siblings are doing, so a batch cut short leaves every call that finished recorded as finished. A
    tool's exception is its error text; only cancellation propagates, and no call of the batch is still running when
    it does: a cancelled or closed dispatcher cancels every call and waits for it, and a call cancelled on its own
    lets its siblings finish first, so nothing a call does lands after the run stores its terminal record.
    """
    runnable = [call for call in calls if not call.execution.is_paused]

    async def run_batch(emit: Callable[[object], None]) -> None:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(_run_call(call, run, middleware, emit)) for call in runnable]
        for task in tasks:
            task.result()  # A call cancelled on its own cancels the batch once its siblings finished.

    # The calls' group runs in a task of its own, so this generator never yields inside the group.
    finished = callback_event_stream(run_batch)
    async with closing_async_stream(finished):
        async for item in finished:
            # The batch's own result, None, comes last; everything before it is a call's progress or a finished call.
            if item is not None:
                yield cast("ToolProgress | tuple[PlannedCall, Message]", item)


async def _run_call(
    call: PlannedCall,
    run: RunContext,
    middleware: Sequence[ToolMiddleware],
    emit: Callable[[object], None],
) -> None:
    """Run one call, hand over its progress while it runs, record its outcome, and hand it over with its result."""
    execution = call.execution
    context = run.call_context(execution.tool_call_id)
    loop = asyncio.get_running_loop()
    lock = threading.Lock()
    latest: str | None = None
    flush_scheduled = False
    returned = False
    last_flush = -math.inf

    def flush() -> None:
        nonlocal latest, flush_scheduled, last_flush
        with lock:
            text, latest, flush_scheduled = latest, None, False
        last_flush = loop.time()
        if text is not None:
            # Output can print credentials; mask them as the call's result preview does.
            emit(ToolProgress(execution.tool_call_id, shown_progress(text)))

    def schedule_flush() -> None:
        loop.call_later(max(0.0, last_flush + _PROGRESS_FLUSH_SECONDS - loop.time()), flush)

    def report(text: str) -> None:
        # Any thread may report, and a tool, such as untrusted worker code, may report far faster than anyone can
        # watch: keep only the newest report and flush it at most as often as live frames go out.
        nonlocal latest, flush_scheduled
        with lock:
            if returned:
                return  # A thread that outlives the call cannot report for it.
            latest = text
            if flush_scheduled:
                return
            flush_scheduled = True
        loop.call_soon_threadsafe(schedule_flush)

    try:
        with tool_progress_reporter(report):
            outcome = await execute_tool_call(call.function, execution.tool_args, context, middleware=middleware)
    finally:
        with lock:
            returned = True
        flush()  # The latest report lands before the result.
    execution.result = outcome.content
    execution.is_error = outcome.is_error
    execution.stop_after_call = outcome.stop_after_call
    emit((call, tool_result_message(execution, media=outcome)))


def tool_result_message(execution: ToolExecution, media: ToolValue | None = None) -> Message:
    """Return the tool message that answers ``execution`` with its recorded result.

    ``media`` is the tool value whose media the message carries when the call succeeded; a failed call carries none.
    """
    images = audio = videos = files = None
    if media is not None and not execution.is_error:
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


def is_media_follow_up(message: Message) -> bool:
    """Return whether a stored message is a media follow-up the loop wrote, which no person said."""
    return message.role == "user" and message.content == MEDIA_FOLLOW_UP_TEXT


__all__ = [
    "MAX_CONSECUTIVE_TOOL_FAILURES",
    "MEDIA_FOLLOW_UP_TEXT",
    "PlannedCall",
    "PreparedTools",
    "RefusedCall",
    "ToolBatch",
    "answered_tool_batch",
    "count_tool_failures",
    "count_tool_refusals",
    "dispatch_tool_calls",
    "failed_answer",
    "failing_tool_stop_text",
    "ignored_refusal",
    "is_media_follow_up",
    "media_follow_up",
    "offered_toolkits",
    "plan_tool_batch",
    "sorted_specs",
    "tool_result_message",
    "toolkit_instructions",
]
