"""Internal delivery and supervision helpers for streaming responses."""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.logging_config import get_logger
from mindroom.tool_system.events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import nio

    from mindroom.tool_system.runtime_context import WorkerProgressEvent, WorkerProgressPump

    from .streaming import StreamingResponse

logger = get_logger(__name__)

StreamInputChunk = (
    str | StructuredStreamChunk | RunContentEvent | RunCompletedEvent | ToolCallStartedEvent | ToolCallCompletedEvent
)
_STREAM_DELIVERY_DRAIN_TIMEOUT_SECONDS = 5.0
_STREAM_DELIVERY_CANCEL_TIMEOUT_SECONDS = 5.0
_VISIBLE_TOOL_MARKER_LINE_PATTERN = re.compile(r"^\s*🔧 `[^`]+` \[\d+\](?: ⏳)?\s*$")


class _NonTerminalDeliveryError(Exception):
    """Internal wrapper for non-terminal delivery failures."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


class _StreamDeliveryShutdownTimeoutError(TimeoutError):
    """Raised when the single non-terminal delivery owner refuses to stop."""


def _longest_common_prefix_len(first: list[ToolTraceEntry], second: list[ToolTraceEntry]) -> int:
    """Return the number of leading tool-trace entries shared by both lists."""
    max_len = min(len(first), len(second))
    index = 0
    while index < max_len and first[index] == second[index]:
        index += 1
    return index


def _merge_tool_trace(existing: list[ToolTraceEntry], incoming: list[ToolTraceEntry]) -> list[ToolTraceEntry]:
    """Merge a trace snapshot without dropping entries when stream styles are mixed."""
    if not existing:
        return incoming.copy()
    if not incoming:
        return existing.copy()

    shared_prefix = _longest_common_prefix_len(existing, incoming)
    if shared_prefix == len(existing):
        return incoming.copy()
    if shared_prefix == len(incoming):
        return existing.copy()
    if len(incoming) >= len(existing):
        return incoming.copy()
    return existing.copy()


def _merge_final_completion_content(accumulated_text: str, final_text: str) -> str:
    """Preserve visible tool markers when a provider emits canonical final content."""
    tool_marker_lines = [
        line for line in accumulated_text.splitlines() if _VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line)
    ]
    if not tool_marker_lines:
        return final_text
    tool_marker_block = "\n\n".join(tool_marker_lines)
    return f"{tool_marker_block}\n\n{final_text}" if final_text else tool_marker_block


@dataclass(frozen=True, slots=True)
class _DeliveryRequest:
    """One non-terminal stream delivery request for the single delivery owner."""

    progress_hint: bool = False
    force_refresh: bool = False
    allow_empty_progress: bool = False


def _raise_progress_delivery_error(error: Exception) -> NoReturn:
    """Raise a stored worker-progress delivery error from a helper."""
    raise error


def _queue_delivery_request(
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    *,
    progress_hint: bool = False,
    force_refresh: bool = False,
    allow_empty_progress: bool = False,
) -> None:
    """Queue one non-terminal delivery request for the single delivery owner."""
    delivery_queue.put_nowait(
        _DeliveryRequest(
            progress_hint=progress_hint,
            force_refresh=force_refresh,
            allow_empty_progress=allow_empty_progress,
        ),
    )


async def _consume_streaming_chunks(  # noqa: C901, PLR0912, PLR0915
    response_stream: AsyncIterator[StreamInputChunk],
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Consume stream chunks and apply incremental message updates."""
    pending_tools: list[tuple[str, int]] = []

    async for chunk in response_stream:
        if isinstance(chunk, str):
            text_chunk = chunk
        elif isinstance(chunk, StructuredStreamChunk):
            text_chunk = chunk.content
            if chunk.tool_trace is not None:
                streaming.tool_trace = _merge_tool_trace(streaming.tool_trace, chunk.tool_trace)
        elif isinstance(chunk, RunContentEvent):
            if chunk.reasoning_content:
                streaming.observed_reasoning_content = True
            if chunk.content:
                text_chunk = str(chunk.content)
            else:
                _queue_delivery_request(delivery_queue, progress_hint=True)
                continue
        elif isinstance(chunk, RunCompletedEvent):
            if chunk.reasoning_content:
                streaming.observed_reasoning_content = True
            if chunk.content is not None:
                streaming.accumulated_text = _merge_final_completion_content(
                    streaming.accumulated_text,
                    str(chunk.content),
                )
            continue
        elif isinstance(chunk, ToolCallStartedEvent):
            if chunk.tool is not None:
                streaming.observed_tool_calls += 1
            if not streaming.show_tool_calls:
                if chunk.tool is not None:
                    streaming._ensure_hidden_tool_gap()
                _queue_delivery_request(delivery_queue, progress_hint=True)
                continue

            tool_index = len(streaming.tool_trace) + 1
            text_chunk, trace_entry = format_tool_started_event(chunk.tool, tool_index=tool_index)
            if trace_entry is not None:
                streaming.tool_trace.append(trace_entry)
                pending_tools.append((trace_entry.tool_name, tool_index))
        elif isinstance(chunk, ToolCallCompletedEvent):
            info = extract_tool_completed_info(chunk.tool)
            if info:
                tool_name, result = info
                if streaming.show_tool_calls:
                    match_pos = next(
                        (pos for pos in range(len(pending_tools) - 1, -1, -1) if pending_tools[pos][0] == tool_name),
                        None,
                    )
                    if match_pos is None:
                        logger.warning(
                            "Missing pending tool start in streaming response; skipping completion marker",
                            tool_name=tool_name,
                        )
                        _queue_delivery_request(delivery_queue, progress_hint=True)
                        continue
                    _, tool_index = pending_tools.pop(match_pos)
                    streaming.accumulated_text, trace_entry = complete_pending_tool_block(
                        streaming.accumulated_text,
                        tool_name,
                        result,
                        tool_index=tool_index,
                    )
                    if 0 < tool_index <= len(streaming.tool_trace):
                        existing_entry = streaming.tool_trace[tool_index - 1]
                        existing_entry.type = "tool_call_completed"
                        existing_entry.result_preview = trace_entry.result_preview
                        existing_entry.truncated = existing_entry.truncated or trace_entry.truncated
                    else:
                        logger.warning(
                            "Missing tool trace slot in streaming response for completion",
                            tool_name=tool_name,
                            tool_index=tool_index,
                            trace_len=len(streaming.tool_trace),
                        )
                else:
                    _queue_delivery_request(delivery_queue, progress_hint=True)
                    continue
                _queue_delivery_request(delivery_queue)
                continue
            text_chunk = ""
        else:
            logger.debug("unhandled_streaming_event_type", event_type=type(chunk).__name__)
            continue

        if text_chunk:
            streaming._warmup_state.clear_terminal_failures()
            streaming._update(text_chunk)
            _queue_delivery_request(delivery_queue)


async def _drain_worker_progress_events(
    streaming: StreamingResponse,
    queue: asyncio.Queue[WorkerProgressEvent],
    pump: WorkerProgressPump,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Apply worker progress events to side-band state and refresh the current stream body."""
    while True:
        event = await queue.get()
        if pump.shutdown.is_set():
            return
        if streaming._warmup_state.apply_event(event):
            if pump.shutdown.is_set():
                return
            if streaming._warmup_state.needs_warmup_clear_edit:
                _queue_delivery_request(
                    delivery_queue,
                    force_refresh=True,
                    allow_empty_progress=not streaming.accumulated_text.strip(),
                )
                continue
            should_refresh = (
                bool(streaming.accumulated_text.strip())
                or bool(streaming._warmup_state.active_warmups)
                or event.progress.phase == "failed"
            )
            if not should_refresh:
                continue
            if pump.shutdown.is_set():
                return
            _queue_delivery_request(delivery_queue, progress_hint=True)


async def _shutdown_worker_progress_drain(
    pump: WorkerProgressPump,
    progress_task: asyncio.Task[None] | None,
) -> Exception | None:
    """Stop the worker-progress drain before terminal stream finalization."""
    pump.shutdown.set()
    if progress_task is None:
        return None
    if not progress_task.done():
        progress_task.cancel()
    try:
        await asyncio.wait_for(progress_task, timeout=0.5)
    except (asyncio.CancelledError, TimeoutError):
        return None
    except Exception as exc:
        return exc
    return None


async def _drive_stream_delivery(
    client: nio.AsyncClient,
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Own all non-terminal stream sends and edits from one supervised task."""
    stop_after_current = False

    while True:
        request = await delivery_queue.get()
        if request is None:
            return

        merged_request = request
        while True:
            try:
                next_request = delivery_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if next_request is None:
                stop_after_current = True
                break
            merged_request = _DeliveryRequest(
                progress_hint=merged_request.progress_hint or next_request.progress_hint,
                force_refresh=merged_request.force_refresh or next_request.force_refresh,
                allow_empty_progress=merged_request.allow_empty_progress or next_request.allow_empty_progress,
            )

        if merged_request.force_refresh:
            sent = await streaming._send_or_edit_message(
                client,
                allow_empty_progress=merged_request.allow_empty_progress,
            )
            if sent:
                streaming.last_update = time.time()
                streaming.chars_since_last_update = 0
        else:
            await streaming._throttled_send(client, progress_hint=merged_request.progress_hint)

        if stop_after_current:
            return


async def _shutdown_stream_delivery(
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    delivery_task: asyncio.Task[None] | None,
    *,
    drain_timeout_seconds: float = _STREAM_DELIVERY_DRAIN_TIMEOUT_SECONDS,
    cancel_timeout_seconds: float = _STREAM_DELIVERY_CANCEL_TIMEOUT_SECONDS,
) -> Exception | None:
    """Stop the single delivery owner before terminal stream finalization."""
    if delivery_task is None:
        return None
    if not delivery_task.done():
        delivery_queue.put_nowait(None)
    done, _pending = await asyncio.wait({delivery_task}, timeout=drain_timeout_seconds)
    if delivery_task not in done:
        delivery_task.cancel()
        done, _pending = await asyncio.wait({delivery_task}, timeout=cancel_timeout_seconds)
        if delivery_task not in done:
            return _StreamDeliveryShutdownTimeoutError("Timed out shutting down stream delivery controller")
    if delivery_task.cancelled():
        return None
    task_error = delivery_task.exception()
    if task_error is None:
        return None
    if isinstance(task_error, Exception):
        return task_error
    return None


async def _cancel_stream_consumer(stream_task: asyncio.Task[None]) -> None:
    """Cancel chunk consumption after a progress-delivery failure wins ownership."""
    if stream_task.done():
        with suppress(asyncio.CancelledError, Exception):
            await stream_task
        return
    stream_task.cancel()
    with suppress(asyncio.CancelledError, TimeoutError, Exception):
        await asyncio.wait_for(stream_task, timeout=0.5)


async def _handle_auxiliary_task_completion(
    done_tasks: set[asyncio.Task[None]],
    task: asyncio.Task[None] | None,
    *,
    stream_task: asyncio.Task[None],
    monitored_tasks: set[asyncio.Task[None]],
    delivery_task: asyncio.Task[None] | None,
) -> bool:
    """Surface one auxiliary-task failure through the normal streaming contract."""
    if task is None or task not in done_tasks:
        return False

    if task.cancelled():
        await _cancel_stream_consumer(stream_task)
        raise asyncio.CancelledError

    task_error = task.exception()
    if task_error is not None:
        await _cancel_stream_consumer(stream_task)
        if not isinstance(task_error, Exception):
            raise task_error
        if task is delivery_task:
            raise _NonTerminalDeliveryError(task_error) from task_error
        _raise_progress_delivery_error(task_error)

    monitored_tasks.discard(task)
    return True


async def _consume_stream_with_progress_supervision(
    response_stream: AsyncIterator[StreamInputChunk],
    streaming: StreamingResponse,
    progress_task: asyncio.Task[None] | None,
    delivery_task: asyncio.Task[None] | None,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Abort chunk consumption as soon as the worker-progress drain fails."""
    stream_task = asyncio.create_task(_consume_streaming_chunks(response_stream, streaming, delivery_queue))
    monitored_tasks: set[asyncio.Task[None]] = {stream_task}
    if progress_task is not None:
        monitored_tasks.add(progress_task)
    if delivery_task is not None:
        monitored_tasks.add(delivery_task)

    try:
        while True:
            done, _pending = await asyncio.wait(monitored_tasks, return_when=asyncio.FIRST_COMPLETED)

            if await _handle_auxiliary_task_completion(
                done,
                progress_task,
                stream_task=stream_task,
                monitored_tasks=monitored_tasks,
                delivery_task=delivery_task,
            ):
                progress_task = None

            if await _handle_auxiliary_task_completion(
                done,
                delivery_task,
                stream_task=stream_task,
                monitored_tasks=monitored_tasks,
                delivery_task=delivery_task,
            ):
                delivery_task = None

            if stream_task in done:
                await stream_task
                return
    finally:
        await _cancel_stream_consumer(stream_task)
