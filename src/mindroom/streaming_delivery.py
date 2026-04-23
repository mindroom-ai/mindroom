"""Internal delivery and supervision helpers for streaming responses."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.logging_config import get_logger
from mindroom.tool_system.events import (
    StructuredStreamChunk,
    ToolTraceEntry,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import nio

    from mindroom.tool_system.runtime_context import WorkerProgressEvent, WorkerProgressPump

    from .streaming import StreamingResponse

logger = get_logger(__name__)

StreamInputChunk = str | StructuredStreamChunk | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent
_STREAM_DELIVERY_DRAIN_TIMEOUT_SECONDS = 5.0
_STREAM_DELIVERY_CANCEL_TIMEOUT_SECONDS = 5.0


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


def _merge_prior_delta_at(existing: float | None, incoming: float | None) -> float | None:
    """Keep the oldest unsent delta timestamp across merged delivery requests."""
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    return min(existing, incoming)


@dataclass(frozen=True, slots=True)
class _DeliveryRequest:
    """One non-terminal stream delivery request for the single delivery owner."""

    progress_hint: bool = False
    force_refresh: bool = False
    boundary_refresh: bool = False
    phase_boundary_flush: bool = False
    allow_empty_progress: bool = False
    prior_delta_at: float | None = None
    boundary_refresh_prior_delta_at: float | None = None
    capture_completion: asyncio.Future[None] | None = None


def _raise_progress_delivery_error(error: Exception) -> NoReturn:
    """Raise a stored worker-progress delivery error from a helper."""
    raise error


def _queue_delivery_request(
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    *,
    progress_hint: bool = False,
    force_refresh: bool = False,
    boundary_refresh: bool = False,
    phase_boundary_flush: bool = False,
    allow_empty_progress: bool = False,
    prior_delta_at: float | None = None,
    boundary_refresh_prior_delta_at: float | None = None,
    wait_for_capture: bool = False,
) -> asyncio.Future[None] | None:
    """Queue one non-terminal delivery request for the single delivery owner."""
    capture_completion = asyncio.get_running_loop().create_future() if wait_for_capture else None
    delivery_queue.put_nowait(
        _DeliveryRequest(
            progress_hint=progress_hint,
            force_refresh=force_refresh,
            boundary_refresh=boundary_refresh,
            phase_boundary_flush=phase_boundary_flush,
            allow_empty_progress=allow_empty_progress,
            prior_delta_at=prior_delta_at,
            boundary_refresh_prior_delta_at=(
                (prior_delta_at if boundary_refresh_prior_delta_at is None else boundary_refresh_prior_delta_at)
                if boundary_refresh
                else None
            ),
            capture_completion=capture_completion,
        ),
    )
    return capture_completion


async def _flush_phase_boundary_if_needed(
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Flush any buffered visible text before mutating the next phase."""
    if streaming.chars_since_last_update == 0:
        return
    inflight_capture = streaming.matching_inflight_nonterminal_capture()
    if inflight_capture is not None:
        await inflight_capture
        return
    completion = _queue_delivery_request(
        delivery_queue,
        phase_boundary_flush=True,
        wait_for_capture=True,
    )
    assert completion is not None
    await completion


async def _apply_visible_text_chunk(
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    text_chunk: str,
    *,
    apply_chunk: Callable[[str], None],
    replacement_suffixes: tuple[str, ...] = (),
    force_refresh: bool = False,
    boundary_refresh: bool = False,
    wait_for_capture: bool = False,
) -> None:
    """Apply one visible text-like chunk and queue the matching delivery request."""
    if not text_chunk:
        return

    streaming._warmup_state.clear_terminal_failures()
    prior_delta_at = streaming.last_delta_at
    apply_chunk(text_chunk)
    if replacement_suffixes and streaming.uses_replacement_updates():
        for suffix in replacement_suffixes:
            if suffix not in streaming.accumulated_text:
                streaming._append_incremental_text(suffix)
    completion = _queue_delivery_request(
        delivery_queue,
        force_refresh=force_refresh,
        boundary_refresh=boundary_refresh,
        prior_delta_at=None if force_refresh else prior_delta_at,
        boundary_refresh_prior_delta_at=streaming.last_delta_at if boundary_refresh else None,
        wait_for_capture=wait_for_capture,
    )
    if completion is not None:
        await completion


async def _consume_streaming_chunks(  # noqa: C901, PLR0912, PLR0915
    response_stream: AsyncIterator[StreamInputChunk],
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Consume stream chunks and apply incremental message updates."""
    pending_tools: list[tuple[str, int, str]] = []

    async for chunk in response_stream:
        if isinstance(chunk, str):
            text_chunk = chunk
        elif isinstance(chunk, StructuredStreamChunk):
            text_chunk = chunk.content
            if chunk.tool_trace is not None:
                streaming.tool_trace = _merge_tool_trace(streaming.tool_trace, chunk.tool_trace)
        elif isinstance(chunk, RunContentEvent) and chunk.content:
            text_chunk = str(chunk.content)
        elif isinstance(chunk, ToolCallStartedEvent):
            if not streaming.show_tool_calls:
                await _flush_phase_boundary_if_needed(streaming, delivery_queue)
                if chunk.tool is not None:
                    cleared_terminal_failures = any(
                        warmup.last_event.phase == "failed"
                        for warmup in streaming._warmup_state.active_warmups.values()
                    )
                    streaming._warmup_state.clear_terminal_failures()
                    streaming._ensure_hidden_tool_gap()
                    _queue_delivery_request(
                        delivery_queue,
                        force_refresh=cleared_terminal_failures,
                        progress_hint=not cleared_terminal_failures,
                        allow_empty_progress=cleared_terminal_failures and not streaming.accumulated_text.strip(),
                    )
                    continue
                _queue_delivery_request(delivery_queue, progress_hint=True)
                continue

            if chunk.tool is None:
                await _flush_phase_boundary_if_needed(streaming, delivery_queue)
                continue

            tool_index = len(streaming.tool_trace) + 1
            text_chunk, trace_entry = format_tool_started_event(chunk.tool, tool_index=tool_index)
            if trace_entry is not None:
                streaming.tool_trace.append(trace_entry)
                pending_tools.append((trace_entry.tool_name, tool_index, text_chunk))
            await _apply_visible_text_chunk(
                streaming,
                delivery_queue,
                text_chunk,
                apply_chunk=streaming._append_incremental_text,
                boundary_refresh=True,
                wait_for_capture=(
                    streaming.uses_replacement_updates() and streaming.matching_inflight_nonterminal_capture() is None
                ),
            )
            continue
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
                    _, tool_index, _ = pending_tools.pop(match_pos)
                    prior_delta_at = streaming.last_delta_at
                    previous_text = streaming.accumulated_text
                    streaming.accumulated_text, trace_entry = complete_pending_tool_block(
                        streaming.accumulated_text,
                        tool_name,
                        result,
                        tool_index=tool_index,
                    )
                    text_changed = streaming.accumulated_text != previous_text
                    if text_changed:
                        streaming._mark_nonadditive_text_mutation()
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
                _queue_delivery_request(
                    delivery_queue,
                    prior_delta_at=prior_delta_at if text_changed else None,
                )
                continue
            text_chunk = ""
        else:
            logger.debug("unhandled_streaming_event_type", event_type=type(chunk).__name__)
            continue

        await _apply_visible_text_chunk(
            streaming,
            delivery_queue,
            text_chunk,
            apply_chunk=streaming._update,
            replacement_suffixes=tuple(marker_text for _, _, marker_text in pending_tools),
        )


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


async def _drive_stream_delivery(  # noqa: C901, PLR0912
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
        phase_boundary_capture_completions = (
            [request.capture_completion]
            if request.phase_boundary_flush and request.capture_completion is not None
            else []
        )
        boundary_refresh_capture_completions = (
            [request.capture_completion] if request.boundary_refresh and request.capture_completion is not None else []
        )
        while True:
            try:
                next_request = delivery_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if next_request is None:
                stop_after_current = True
                break
            if next_request.phase_boundary_flush and next_request.capture_completion is not None:
                phase_boundary_capture_completions.append(next_request.capture_completion)
            if next_request.boundary_refresh and next_request.capture_completion is not None:
                boundary_refresh_capture_completions.append(next_request.capture_completion)
            merged_request = _DeliveryRequest(
                progress_hint=merged_request.progress_hint or next_request.progress_hint,
                force_refresh=merged_request.force_refresh or next_request.force_refresh,
                boundary_refresh=merged_request.boundary_refresh or next_request.boundary_refresh,
                phase_boundary_flush=merged_request.phase_boundary_flush or next_request.phase_boundary_flush,
                allow_empty_progress=merged_request.allow_empty_progress or next_request.allow_empty_progress,
                prior_delta_at=_merge_prior_delta_at(
                    merged_request.prior_delta_at,
                    next_request.prior_delta_at,
                ),
                boundary_refresh_prior_delta_at=(
                    next_request.boundary_refresh_prior_delta_at
                    if next_request.boundary_refresh_prior_delta_at is not None
                    else merged_request.boundary_refresh_prior_delta_at
                ),
            )

        try:
            prepared_phase_boundary_flush = None
            if merged_request.phase_boundary_flush and (
                streaming.chars_since_last_update > 0 and streaming.accumulated_text.strip()
            ):
                prepared_phase_boundary_flush = streaming._prepare_delivery(
                    is_final=False,
                    allow_empty_progress=False,
                    stream_status=None,
                )
            if prepared_phase_boundary_flush is None:
                for capture_completion in phase_boundary_capture_completions:
                    if not capture_completion.done():
                        capture_completion.set_result(None)
            if prepared_phase_boundary_flush is not None:
                await streaming._send_prepared_delivery(
                    client,
                    prepared_delivery=prepared_phase_boundary_flush,
                    is_final=False,
                    capture_completions=tuple(phase_boundary_capture_completions),
                )
            if merged_request.force_refresh:
                await streaming._send_or_edit_message(
                    client,
                    allow_empty_progress=merged_request.allow_empty_progress,
                    boundary_refresh=merged_request.boundary_refresh,
                    capture_completions=tuple(boundary_refresh_capture_completions),
                )
            elif merged_request.boundary_refresh:
                current_time = time.time()
                visible_delta_since_last_boundary_refresh = (
                    merged_request.boundary_refresh_prior_delta_at is not None
                    and streaming.last_boundary_refresh_at is not None
                    and merged_request.boundary_refresh_prior_delta_at > streaming.last_boundary_refresh_at
                )
                should_send_boundary_refresh = (
                    streaming.event_id is None
                    or streaming.last_boundary_refresh_at is None
                    or bool(boundary_refresh_capture_completions)
                    or visible_delta_since_last_boundary_refresh
                    or (current_time - streaming.last_boundary_refresh_at) >= streaming.progress_update_interval
                )
                if should_send_boundary_refresh:
                    await streaming._send_or_edit_message(
                        client,
                        allow_empty_progress=merged_request.allow_empty_progress,
                        boundary_refresh=True,
                        capture_completions=tuple(boundary_refresh_capture_completions),
                    )
                else:
                    await streaming._throttled_send(
                        client,
                        progress_hint=True,
                        prior_delta_at=merged_request.prior_delta_at,
                    )
            elif not merged_request.phase_boundary_flush:
                await streaming._throttled_send(
                    client,
                    progress_hint=merged_request.progress_hint,
                    prior_delta_at=merged_request.prior_delta_at,
                )
        except Exception as exc:
            for capture_completion in [
                *phase_boundary_capture_completions,
                *boundary_refresh_capture_completions,
            ]:
                if not capture_completion.done():
                    capture_completion.set_exception(exc)
            raise

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
