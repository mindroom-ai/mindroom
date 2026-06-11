"""Streaming response state machine: placeholder, progressive edits, tool traces, cancellation."""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom import interactive
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import build_edit_event_content, edit_message_result, send_message_result
from mindroom.matrix.large_messages import should_send_oversized_nonterminal_streaming_edit
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.orchestration.runtime import (
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    CancelSource,
    cancel_failure_reason,
    classify_cancel_source,
    log_cancelled_response,
)
from mindroom.streaming_warmup import WorkerWarmupState
from mindroom.timing import emit_timing_event
from mindroom.tool_system.events import (
    StreamingToolTracker,
    StructuredStreamChunk,
    complete_pending_tool_block,
)
from mindroom.tool_system.runtime_context import worker_progress_pump_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.message_target import MessageTarget
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import WorkerProgressEvent, WorkerProgressPump

logger = get_logger(__name__)

__all__ = [
    "INTERRUPTED_RESPONSE_NOTE",
    "PROGRESS_PLACEHOLDER",
    "RESTART_INTERRUPTED_RESPONSE_NOTE",
    "SYNC_RESTART_CANCEL_MSG",
    "USER_STOP_CANCEL_MSG",
    "CancelSource",
    "ReplacementStreamingResponse",
    "StreamInputChunk",
    "StreamingDeliveryError",
    "StreamingResponse",
    "build_cancelled_response_update",
    "build_restart_interrupted_body",
    "cancel_failure_reason",
    "clean_partial_reply_text",
    "is_interrupted_partial_reply",
    "send_streaming_response",
    "strip_visible_tool_markers",
]

_PROGRESS_PLACEHOLDER = "Thinking..."
PROGRESS_PLACEHOLDER = _PROGRESS_PLACEHOLDER
_CANCELLED_RESPONSE_NOTE = "**[Response cancelled by user]**"
INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted]**"
_INTERRUPTED_RESPONSE_NOTE = INTERRUPTED_RESPONSE_NOTE
RESTART_INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted by service restart]**"
_STREAM_ERROR_RESPONSE_NOTE = "**[Response interrupted by an error"
_TerminalStreamStatus = Literal["completed", "cancelled", "error"]
_VISIBLE_TOOL_MARKER_LINE_PATTERN = re.compile(r"^\s*🔧 `[^`]+` \[\d+\](?: ⏳)?\s*$")
_VISIBLE_TOOL_MARKER_SEPARATOR_PATTERN = re.compile(r"^\s{0,3}---\s*$")

StreamInputChunk = (
    str | StructuredStreamChunk | RunContentEvent | RunCompletedEvent | ToolCallStartedEvent | ToolCallCompletedEvent
)
_STREAM_DELIVERY_DRAIN_TIMEOUT_SECONDS = 5.0
_STREAM_DELIVERY_CANCEL_TIMEOUT_SECONDS = 5.0


class _NonTerminalDeliveryError(Exception):
    """Internal wrapper for non-terminal delivery failures."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


class _StreamDeliveryShutdownTimeoutError(TimeoutError):
    """Raised when the single non-terminal delivery owner refuses to stop."""


def strip_visible_tool_markers(text: str) -> str:
    """Remove display-only tool marker lines before text re-enters model context."""
    lines = text.splitlines()
    if not any(_VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line) for line in lines):
        return text

    filtered_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _VISIBLE_TOOL_MARKER_LINE_PATTERN.fullmatch(line):
            filtered_lines.append(line)
            index += 1
            continue

        index += 1
        spacer_lines: list[str] = []
        while index < len(lines) and not lines[index].strip():
            spacer_lines.append(lines[index])
            index += 1

        # Tool markers rendered by MindRoom are often followed by a markdown
        # separator. Remove the separator with the marker, but preserve ordinary
        # blank-line spacing so surrounding prose does not get smashed together.
        if index < len(lines) and _VISIBLE_TOOL_MARKER_SEPARATOR_PATTERN.fullmatch(lines[index]):
            filtered_lines.extend(spacer_lines)
            index += 1
            if index < len(lines) and not lines[index].strip():
                index += 1
            continue

        filtered_lines.extend(spacer_lines)
    return "\n".join(filtered_lines).rstrip()


class StreamingDeliveryError(Exception):
    """Preserve the finalized stream state when delivery fails mid-response."""

    def __init__(
        self,
        error: BaseException,
        *,
        event_id: str | None,
        accumulated_text: str,
        tool_trace: list[ToolTraceEntry],
        transport_outcome: StreamTransportOutcome,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.event_id = event_id
        self.accumulated_text = accumulated_text
        self.tool_trace = tool_trace.copy()
        self.transport_outcome = transport_outcome


def _build_streaming_delivery_error(
    streaming: StreamingResponse,
    error: BaseException,
    *,
    failure_reason: str,
    terminal_status: Literal["cancelled", "error"],
    tool_trace_collector: list[ToolTraceEntry] | None,
) -> StreamingDeliveryError:
    """Build one normalized delivery failure from the current committed stream state."""
    if tool_trace_collector is not None:
        tool_trace_collector[:] = streaming.tool_trace
    rendered_body, visible_body_state = streaming._committed_terminal_snapshot()
    canonical_final_body_candidate = streaming.canonical_final_body_candidate
    if canonical_final_body_candidate is None and streaming.accumulated_text.strip():
        canonical_final_body_candidate = streaming.accumulated_text
    return StreamingDeliveryError(
        error,
        event_id=streaming.event_id,
        accumulated_text=streaming.accumulated_text,
        tool_trace=streaming.tool_trace,
        transport_outcome=StreamTransportOutcome(
            last_physical_stream_event_id=streaming.event_id,
            terminal_status=terminal_status,
            rendered_body=rendered_body,
            visible_body_state=visible_body_state,
            canonical_final_body_candidate=canonical_final_body_candidate,
            failure_reason=failure_reason,
            interactive_metadata=streaming._last_committed_interactive_metadata,
        ),
    )


def _raise_nonterminal_delivery_error(error: Exception) -> NoReturn:
    """Raise one wrapped non-terminal delivery error for unified rollback handling."""
    raise _NonTerminalDeliveryError(error) from error


def _complete_capture_completions(capture_completions: tuple[asyncio.Future[None], ...]) -> None:
    for capture_completion in capture_completions:
        if not capture_completion.done():
            capture_completion.set_result(None)


def _format_stream_error_note(error: Exception) -> str:
    """Return a concise user-facing note for stream-time exceptions."""
    normalized_error = " ".join(str(error).split())
    if not normalized_error:
        return f"{_STREAM_ERROR_RESPONSE_NOTE}. Please retry.]**"
    if len(normalized_error) > 220:
        normalized_error = f"{normalized_error[:219]}…"
    return f"{_STREAM_ERROR_RESPONSE_NOTE}: {normalized_error}]**"


def is_interrupted_partial_reply(text: object) -> bool:
    """Return True when text carries a terminal interrupted partial-reply marker."""
    if not isinstance(text, str):
        return False
    trimmed_text = text.rstrip()
    return trimmed_text.endswith(
        (
            _CANCELLED_RESPONSE_NOTE,
            _INTERRUPTED_RESPONSE_NOTE,
            RESTART_INTERRUPTED_RESPONSE_NOTE,
            " [cancelled]",
            " [error]",
        ),
    ) or (_STREAM_ERROR_RESPONSE_NOTE in trimmed_text)


def clean_partial_reply_text(text: str) -> str:
    """Strip partial-reply status notes from persisted text."""
    cleaned = text.rstrip()

    for marker in (
        " [cancelled]",
        " [error]",
        _CANCELLED_RESPONSE_NOTE,
        _INTERRUPTED_RESPONSE_NOTE,
        RESTART_INTERRUPTED_RESPONSE_NOTE,
    ):
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)].rstrip()

    if _STREAM_ERROR_RESPONSE_NOTE in cleaned:
        cleaned = cleaned.split(_STREAM_ERROR_RESPONSE_NOTE, 1)[0].rstrip()

    if cleaned == _PROGRESS_PLACEHOLDER or not cleaned or not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


def build_restart_interrupted_body(text: str) -> str:
    """Return restart-note text for a stale in-progress message body."""
    stripped_text = text.rstrip()
    if not stripped_text or stripped_text == _PROGRESS_PLACEHOLDER:
        return RESTART_INTERRUPTED_RESPONSE_NOTE
    return f"{stripped_text}\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}"


@dataclass(frozen=True)
class _CommittedDeliveryState:
    """One frozen non-terminal stream state that definitely reached Matrix."""

    accumulated_text: str
    tool_trace: list[ToolTraceEntry]
    placeholder_progress_sent: bool
    rendered_body: str
    visible_body_state: Literal["placeholder_only", "visible_body"]
    interactive_metadata: interactive.InteractiveMetadata | None


def _normalize_stream_accumulated_text(text: str) -> str:
    """Normalize whitespace-only placeholder buffers to the committed empty state."""
    return text if text.strip() else ""


def build_cancelled_response_update(
    text: str,
    *,
    cancel_source: Literal["user_stop", "sync_restart", "interrupted"],
) -> tuple[str, _TerminalStreamStatus]:
    """Return the final visible body and stream status for one cancellation source."""
    if cancel_source == "sync_restart":
        return build_restart_interrupted_body(text), STREAM_STATUS_ERROR

    note = _CANCELLED_RESPONSE_NOTE if cancel_source == "user_stop" else _INTERRUPTED_RESPONSE_NOTE
    # Generic interruptions keep their distinct visible note, but reuse an
    # existing terminal wire status so older clients do not misclassify them.
    stream_status = STREAM_STATUS_CANCELLED if cancel_source == "user_stop" else STREAM_STATUS_ERROR
    stripped_text = text.rstrip()
    if not stripped_text or stripped_text == _PROGRESS_PLACEHOLDER:
        return note, stream_status
    return f"{stripped_text}\n\n{note}", stream_status


@dataclass(frozen=True)
class _PreparedStreamingDelivery:
    """One frozen non-terminal delivery attempt."""

    content: dict[str, Any]
    display_text: str
    committed_state: _CommittedDeliveryState
    had_warmup_suffix: bool


@dataclass
class StreamingResponse:
    """Manages a streaming response with incremental message updates."""

    target: MessageTarget
    config: Config
    runtime_paths: RuntimePaths
    room_id: str = field(init=False)
    reply_to_event_id: str | None = field(init=False)
    thread_id: str | None = field(init=False)
    room_mode: bool = field(init=False)
    accumulated_text: str = ""
    event_id: str | None = None  # None until first message sent
    last_update: float = 0.0
    update_interval: float = 5.0
    min_update_interval: float = 0.5
    interval_ramp_seconds: float = 15.0
    update_char_threshold: int = 240
    min_update_char_threshold: int = 48
    min_char_update_interval: float = 0.35
    progress_update_interval: float = 1.0
    max_idle: float = 2.0
    latest_thread_event_id: str | None = None  # For MSC3440 compliance
    show_tool_calls: bool = True  # When False, omit inline tool call text and tool-trace metadata
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    extra_content: dict[str, Any] | None = None
    stream_started_at: float | None = None
    chars_since_last_update: int = 0
    last_delta_at: float | None = None
    last_boundary_refresh_at: float | None = None
    placeholder_progress_sent: bool = False
    pipeline_timing: DispatchPipelineTiming | None = None
    conversation_cache: ConversationCacheProtocol | None = None
    visible_event_id_callback: Callable[[str], None] | None = None
    preserve_existing_visible_on_empty_terminal: bool = False
    canonical_final_body_candidate: str | None = None
    _warmup_state: WorkerWarmupState = field(default_factory=WorkerWarmupState, init=False, repr=False)
    _last_delivered_text: str = field(default="", init=False, repr=False)
    _last_delivered_tool_trace: list[ToolTraceEntry] = field(default_factory=list, init=False, repr=False)
    _last_placeholder_progress_sent: bool = field(default=False, init=False, repr=False)
    _last_committed_rendered_body: str | None = field(default=None, init=False, repr=False)
    _last_committed_interactive_metadata: interactive.InteractiveMetadata | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_committed_visible_body_state: Literal["none", "placeholder_only", "visible_body"] = field(
        default="none",
        init=False,
        repr=False,
    )
    _inflight_nonterminal_capture: asyncio.Future[None] | None = field(default=None, init=False, repr=False)
    _inflight_nonterminal_capture_state: _CommittedDeliveryState | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Derive Matrix delivery fields from the canonical target."""
        self.room_id = self.target.room_id
        self.thread_id = self.target.resolved_thread_id
        self.reply_to_event_id = self.target.reply_to_event_id
        self.room_mode = self.target.is_room_mode

    def _update(self, new_chunk: str) -> None:
        """Append new chunk to accumulated text."""
        self._append_incremental_text(new_chunk)

    def uses_replacement_updates(self) -> bool:
        """Return whether text chunks replace the current visible body."""
        return False

    def _append_incremental_text(self, new_chunk: str) -> None:
        """Append additive streaming text regardless of snapshot replacement mode."""
        self.accumulated_text += new_chunk
        self.chars_since_last_update += len(new_chunk)
        self.last_delta_at = time.time()

    def _mark_nonadditive_text_mutation(self) -> None:
        """Record a visible in-place text change that did not append characters."""
        self.chars_since_last_update = max(1, self.chars_since_last_update)
        self.last_delta_at = time.time()

    def _ensure_hidden_tool_gap(self) -> None:
        """Insert a single placeholder gap for hidden tool calls."""
        if not self.accumulated_text.endswith("\n\n"):
            self._append_incremental_text("\n\n")

    def _current_update_interval(self, current_time: float) -> float:
        """Return the current throttling interval.

        Streaming starts with faster edits, then ramps toward the steady-state
        interval to reduce edit noise for long responses.
        """
        if self.stream_started_at is None or self.interval_ramp_seconds <= 0:
            return self.update_interval

        fast_interval = min(self.min_update_interval, self.update_interval)
        elapsed = max(0.0, current_time - self.stream_started_at)
        if elapsed >= self.interval_ramp_seconds:
            return self.update_interval

        progress = elapsed / self.interval_ramp_seconds
        return fast_interval + (self.update_interval - fast_interval) * progress

    def _current_char_threshold(self, current_time: float) -> int:
        """Return the current character threshold for triggering updates."""
        steady_threshold = max(1, self.update_char_threshold)
        if self.stream_started_at is None or self.interval_ramp_seconds <= 0:
            return steady_threshold

        fast_threshold = max(1, min(self.min_update_char_threshold, self.update_char_threshold))
        elapsed = max(0.0, current_time - self.stream_started_at)
        if elapsed >= self.interval_ramp_seconds:
            return steady_threshold

        progress = elapsed / self.interval_ramp_seconds
        threshold = fast_threshold + (self.update_char_threshold - fast_threshold) * progress
        return max(1, round(threshold))

    def _mark_nonterminal_delivery(
        self,
        committed_state: _CommittedDeliveryState,
        *,
        boundary_refresh: bool = False,
    ) -> None:
        """Advance throttle state after one non-terminal send or edit reached Matrix."""
        now = time.time()
        if self.stream_started_at is None:
            self.stream_started_at = now
        delivery_matches_live_state = (
            _normalize_stream_accumulated_text(self.accumulated_text) == committed_state.accumulated_text
            and self.tool_trace == committed_state.tool_trace
        )
        if delivery_matches_live_state:
            self.last_update = now
            self.last_delta_at = now
            self.last_boundary_refresh_at = now if boundary_refresh else None
            self.chars_since_last_update = 0
            return

        # The outbound payload was captured before newer live state arrived.
        # Keep pending-delta bookkeeping and boundary-refresh eligibility intact
        # so the next request can surface the newer content immediately.
        self.last_boundary_refresh_at = None
        self.chars_since_last_update = max(1, self.chars_since_last_update)

    def matching_inflight_nonterminal_capture(self) -> asyncio.Future[None] | None:
        """Return the current in-flight capture when it already froze the live state."""
        if self._inflight_nonterminal_capture is None or self._inflight_nonterminal_capture_state is None:
            return None
        if (
            _normalize_stream_accumulated_text(self.accumulated_text)
            == self._inflight_nonterminal_capture_state.accumulated_text
            and self.tool_trace == self._inflight_nonterminal_capture_state.tool_trace
        ):
            return self._inflight_nonterminal_capture
        return None

    async def _throttled_send(
        self,
        client: nio.AsyncClient,
        *,
        progress_hint: bool = False,
        prior_delta_at: float | None = None,
        capture_completions: tuple[asyncio.Future[None], ...] = (),
    ) -> None:
        """Send/edit when either time or character thresholds are met."""
        current_time = time.time()
        if self.stream_started_at is None:
            self.stream_started_at = current_time
        current_interval = self._current_update_interval(current_time)
        if progress_hint:
            current_interval = min(current_interval, self.progress_update_interval)

        elapsed_since_last_update = current_time - self.last_update
        time_triggered = elapsed_since_last_update >= current_interval
        char_triggered = (
            self.chars_since_last_update >= self._current_char_threshold(current_time)
            and elapsed_since_last_update >= self.min_char_update_interval
        )
        idle_reference_delta_at = prior_delta_at if prior_delta_at is not None else self.last_delta_at
        idle_triggered = (
            self.chars_since_last_update > 0
            and idle_reference_delta_at is not None
            and (current_time - idle_reference_delta_at) >= self.max_idle
            and elapsed_since_last_update >= self.min_char_update_interval
        )
        should_send = time_triggered or char_triggered or idle_triggered
        allow_empty_progress = progress_hint and not self.accumulated_text.strip()
        if should_send and (self.accumulated_text.strip() or allow_empty_progress):
            await self._send_or_edit_message(
                client,
                allow_empty_progress=allow_empty_progress,
                capture_completions=capture_completions,
            )

    async def update_content(self, new_chunk: str, client: nio.AsyncClient) -> None:
        """Add new content and potentially update the message."""
        self._warmup_state.clear_terminal_failures()
        previous_last_delta_at = self.last_delta_at
        self._update(new_chunk)
        await self._throttled_send(client, prior_delta_at=previous_last_delta_at)

    def _prepare_terminal_text_and_status(
        self,
        *,
        cancelled: bool,
        restart_interrupted: bool,
        cancel_source: Literal["user_stop", "sync_restart", "interrupted"] | None,
        error: Exception | None,
    ) -> _TerminalStreamStatus:
        """Apply terminal text adjustments and return the terminal stream status."""
        resolved_cancel_source = cancel_source
        if resolved_cancel_source is None:
            if restart_interrupted:
                resolved_cancel_source = "sync_restart"
            elif cancelled:
                resolved_cancel_source = "user_stop"
        if error is not None:
            stripped_text = self.accumulated_text.rstrip()
            error_note = _format_stream_error_note(error)
            self.accumulated_text = f"{stripped_text}\n\n{error_note}" if stripped_text else error_note
            return STREAM_STATUS_ERROR
        if resolved_cancel_source is not None:
            self.accumulated_text, stream_status = build_cancelled_response_update(
                self.accumulated_text,
                cancel_source=resolved_cancel_source,
            )
            return stream_status
        return STREAM_STATUS_COMPLETED

    async def finalize(  # noqa: C901, PLR0911, PLR0912
        self,
        client: nio.AsyncClient,
        *,
        cancelled: bool = False,
        restart_interrupted: bool = False,
        cancel_source: Literal["user_stop", "sync_restart", "interrupted"] | None = None,
        error: Exception | None = None,
    ) -> StreamTransportOutcome:
        """Send the terminal update and return immutable transport facts."""
        self._warmup_state.clear_for_terminal_transition()
        canonical_final_body_candidate = self.canonical_final_body_candidate
        if canonical_final_body_candidate is None and self.accumulated_text.strip():
            canonical_final_body_candidate = self.accumulated_text
        resolved_cancel_source = cancel_source
        if resolved_cancel_source is None:
            if restart_interrupted:
                resolved_cancel_source = "sync_restart"
            elif cancelled:
                resolved_cancel_source = "user_stop"
        had_body_before_terminal = bool(self.accumulated_text.strip())
        final_stream_status = self._prepare_terminal_text_and_status(
            cancelled=cancelled,
            restart_interrupted=restart_interrupted,
            cancel_source=cancel_source,
            error=error,
        )
        terminal_status = "cancelled" if resolved_cancel_source is not None else final_stream_status
        cancellation_failure_reason = (
            cancel_failure_reason(resolved_cancel_source) if resolved_cancel_source is not None else None
        )
        # When a placeholder message exists but no real text arrived,
        # still edit the message to finalize the stream status.
        has_placeholder = (
            self.event_id is not None and self.placeholder_progress_sent and not self.accumulated_text.strip()
        )
        text_to_send = self.accumulated_text
        if (
            final_stream_status == STREAM_STATUS_COMPLETED
            and not text_to_send.strip()
            and canonical_final_body_candidate is not None
            and self._last_committed_visible_body_state != "visible_body"
        ):
            committed_rendered_body, committed_visible_body_state = self._committed_terminal_snapshot()
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=committed_rendered_body,
                visible_body_state=committed_visible_body_state,
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason,
                interactive_metadata=self._last_committed_interactive_metadata,
            )
        if not text_to_send.strip() and final_stream_status == STREAM_STATUS_COMPLETED:
            text_to_send = canonical_final_body_candidate or ""
        if not text_to_send.strip() and final_stream_status == STREAM_STATUS_COMPLETED and not has_placeholder:
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=None,
                visible_body_state="none",
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason,
            )
        if (
            final_stream_status != STREAM_STATUS_COMPLETED
            and self.event_id is not None
            and self.preserve_existing_visible_on_empty_terminal
            and not self.placeholder_progress_sent
            and self._last_committed_visible_body_state == "none"
            and not had_body_before_terminal
        ):
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=None,
                visible_body_state="none",
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason,
            )
        if not text_to_send.strip():
            text_to_send = _PROGRESS_PLACEHOLDER
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=True)
        attempted_rendered_body = (
            response.formatted_text
            if (self.accumulated_text.strip() or has_placeholder or response.formatted_text.strip())
            else None
        )
        attempted_visible_body_state: Literal["none", "placeholder_only", "visible_body"]
        if attempted_rendered_body is None:
            attempted_visible_body_state = "none"
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=None,
                visible_body_state=attempted_visible_body_state,
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason,
            )
        attempted_visible_body_state = (
            "placeholder_only" if attempted_rendered_body == _PROGRESS_PLACEHOLDER else "visible_body"
        )
        try:
            retry_terminal_update = final_stream_status == STREAM_STATUS_COMPLETED
            retry_terminal_update_immediately = (
                final_stream_status != STREAM_STATUS_COMPLETED
                and not restart_interrupted
                and cancel_source != "sync_restart"
            )
            send_succeeded = await self._send_or_edit_message(
                client,
                is_final=True,
                allow_empty_progress=has_placeholder,
                stream_status=final_stream_status,
                retry_on_failure=retry_terminal_update,
                retry_without_backoff=retry_terminal_update_immediately,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Terminal streaming update was cancelled before it landed",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
                exc_info=True,
            )
            (
                committed_rendered_body,
                committed_visible_body_state,
            ) = self._committed_terminal_snapshot()
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=committed_rendered_body,
                visible_body_state=committed_visible_body_state,
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason or "terminal_update_cancelled",
                interactive_metadata=self._last_committed_interactive_metadata,
            )
        except Exception as exc:
            logger.warning(
                "Terminal streaming update raised after retries",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
                reason=str(exc),
                exc_info=True,
            )
            (
                committed_rendered_body,
                committed_visible_body_state,
            ) = self._committed_terminal_snapshot()
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=committed_rendered_body,
                visible_body_state=committed_visible_body_state,
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason or f"terminal_update_exception:{exc.__class__.__name__}",
                interactive_metadata=self._last_committed_interactive_metadata,
            )
        if not send_succeeded:
            logger.warning(
                "Failed to persist terminal stream status",
                event_id=self.event_id,
                room_id=self.room_id,
                stream_status=final_stream_status,
            )
            (
                committed_rendered_body,
                committed_visible_body_state,
            ) = self._committed_terminal_snapshot()
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                terminal_status=terminal_status,
                rendered_body=committed_rendered_body,
                visible_body_state=committed_visible_body_state,
                canonical_final_body_candidate=canonical_final_body_candidate,
                failure_reason=cancellation_failure_reason or "terminal_update_failed",
                interactive_metadata=self._last_committed_interactive_metadata,
            )
        return StreamTransportOutcome(
            last_physical_stream_event_id=self.event_id,
            terminal_status=terminal_status,
            rendered_body=attempted_rendered_body,
            visible_body_state=attempted_visible_body_state,
            canonical_final_body_candidate=canonical_final_body_candidate,
            failure_reason=cancellation_failure_reason,
            interactive_metadata=response.interactive_metadata,
        )

    async def _send_or_edit_message(
        self,
        client: nio.AsyncClient,
        is_final: bool = False,
        *,
        allow_empty_progress: bool = False,
        stream_status: str | None = None,
        retry_on_failure: bool = False,
        retry_without_backoff: bool = False,
        boundary_refresh: bool = False,
        capture_completions: tuple[asyncio.Future[None], ...] = (),
    ) -> bool:
        """Send new message or edit existing one."""
        prepared_delivery = self._prepare_delivery(
            is_final=is_final,
            allow_empty_progress=allow_empty_progress,
            stream_status=stream_status,
        )
        if prepared_delivery is None:
            return True

        return await self._send_prepared_delivery(
            client,
            prepared_delivery=prepared_delivery,
            is_final=is_final,
            boundary_refresh=boundary_refresh,
            capture_completions=capture_completions,
            retry_on_failure=retry_on_failure and is_final,
            retry_without_backoff=retry_without_backoff and is_final,
        )

    async def _send_prepared_delivery(
        self,
        client: nio.AsyncClient,
        *,
        prepared_delivery: _PreparedStreamingDelivery,
        is_final: bool,
        boundary_refresh: bool = False,
        capture_completions: tuple[asyncio.Future[None], ...] = (),
        retry_on_failure: bool = False,
        retry_without_backoff: bool = False,
    ) -> bool:
        """Send one already-prepared non-terminal or terminal payload."""
        is_initial_send = self.event_id is None
        if not is_final and not is_initial_send and not self._should_send_prepared_nonterminal_edit(prepared_delivery):
            _complete_capture_completions(capture_completions)
            return True
        capture = None
        if not is_final:
            capture = asyncio.get_running_loop().create_future()
            self._inflight_nonterminal_capture = capture
            self._inflight_nonterminal_capture_state = prepared_delivery.committed_state
            capture.set_result(None)
            _complete_capture_completions(capture_completions)
        try:
            send_succeeded = await self._send_content(
                client,
                content=prepared_delivery.content,
                display_text=prepared_delivery.display_text,
                retry_on_failure=retry_on_failure,
                retry_without_backoff=retry_without_backoff,
            )
        finally:
            if self._inflight_nonterminal_capture is capture:
                self._inflight_nonterminal_capture = None
                self._inflight_nonterminal_capture_state = None
        if not send_succeeded:
            if not is_final:
                action = "send initial" if is_initial_send else "edit"
                msg = f"Failed to {action} streaming message"
                raise RuntimeError(msg)
            return False

        if not is_final:
            self._warmup_state.note_nonterminal_delivery(
                had_warmup_suffix=prepared_delivery.had_warmup_suffix,
            )
            self._mark_delivery_committed(prepared_delivery.committed_state)
            self._mark_nonterminal_delivery(
                prepared_delivery.committed_state,
                boundary_refresh=boundary_refresh,
            )
        else:
            self.placeholder_progress_sent = False
        return True

    def _should_send_prepared_nonterminal_edit(
        self,
        prepared_delivery: _PreparedStreamingDelivery,
    ) -> bool:
        """Return whether a prepared in-progress edit should hit Matrix now."""
        assert self.event_id is not None
        edit_content = build_edit_event_content(
            event_id=self.event_id,
            new_content=prepared_delivery.content,
            new_text=prepared_delivery.display_text,
        )
        return should_send_oversized_nonterminal_streaming_edit(
            room_id=self.room_id,
            original_event_id=self.event_id,
            edit_content=edit_content,
        )

    def _prepare_delivery(
        self,
        *,
        is_final: bool,
        allow_empty_progress: bool,
        stream_status: str | None,
    ) -> _PreparedStreamingDelivery | None:
        """Freeze one exact outbound payload before awaiting Matrix I/O."""
        warmup_suffix_lines = self._warmup_state.render_lines(show_tool_calls=self.show_tool_calls)
        if not self.accumulated_text.strip() and not allow_empty_progress and not warmup_suffix_lines:
            return None

        assert self.target is not None
        effective_thread_id = self.target.resolved_thread_id

        text_to_send = self.accumulated_text if self.accumulated_text.strip() else _PROGRESS_PLACEHOLDER

        # Format the text (handles interactive questions if present)
        response = interactive.parse_and_format_interactive(text_to_send, extract_mapping=True)
        display_text = response.formatted_text

        # Only use latest_thread_event_id for the initial message (not edits)
        latest_for_message = self.latest_thread_event_id if self.event_id is None and not self.room_mode else None
        stream_status = self._resolve_stream_status(is_final=is_final, stream_status=stream_status)
        extra_content = dict(self.extra_content or {})
        extra_content[STREAM_STATUS_KEY] = stream_status

        content = format_message_with_mentions(
            config=self.config,
            runtime_paths=self.runtime_paths,
            text=display_text,
            thread_event_id=effective_thread_id,
            reply_to_event_id=self.target.reply_to_event_id,
            latest_thread_event_id=latest_for_message,
            tool_trace=self.tool_trace if self.show_tool_calls else None,
            extra_content=extra_content,
        )
        canonical_visible_body = content["body"]
        if warmup_suffix_lines:
            content[STREAM_VISIBLE_BODY_KEY] = canonical_visible_body
            warmup_suffix = "\n".join(line.text for line in warmup_suffix_lines)
            content[STREAM_WARMUP_SUFFIX_KEY] = warmup_suffix
            display_text = f"{display_text}\n\n{warmup_suffix}" if display_text else warmup_suffix
            content["body"] = f"{content['body']}\n\n{warmup_suffix}"
            suffix_html = "".join(f"<p>{line.html}</p>" for line in warmup_suffix_lines)
            content["formatted_body"] = f"{content['formatted_body']}{suffix_html}"

        return _PreparedStreamingDelivery(
            content=content,
            display_text=display_text,
            committed_state=_CommittedDeliveryState(
                accumulated_text=_normalize_stream_accumulated_text(self.accumulated_text),
                tool_trace=deepcopy(self.tool_trace),
                placeholder_progress_sent=not self.accumulated_text.strip(),
                rendered_body=canonical_visible_body,
                visible_body_state=(
                    "placeholder_only" if canonical_visible_body == _PROGRESS_PLACEHOLDER else "visible_body"
                ),
                interactive_metadata=response.interactive_metadata,
            ),
            had_warmup_suffix=bool(warmup_suffix_lines),
        )

    def _mark_delivery_committed(self, committed_state: _CommittedDeliveryState) -> None:
        """Snapshot the last non-terminal text/tool-trace state that actually reached Matrix."""
        self._last_delivered_text = committed_state.accumulated_text
        self._last_delivered_tool_trace = deepcopy(committed_state.tool_trace)
        self._last_placeholder_progress_sent = committed_state.placeholder_progress_sent
        self._last_committed_rendered_body = committed_state.rendered_body
        self._last_committed_visible_body_state = committed_state.visible_body_state
        self._last_committed_interactive_metadata = committed_state.interactive_metadata
        self.placeholder_progress_sent = committed_state.placeholder_progress_sent

    def _committed_terminal_snapshot(
        self,
    ) -> tuple[str | None, Literal["none", "placeholder_only", "visible_body"]]:
        """Return the last visible body that definitely reached Matrix."""
        if self._last_committed_visible_body_state != "none":
            return (
                self._last_committed_rendered_body,
                self._last_committed_visible_body_state,
            )
        if self.event_id is not None and self.placeholder_progress_sent:
            return _PROGRESS_PLACEHOLDER, "placeholder_only"
        return None, "none"

    def restore_last_delivered_state(self) -> None:
        """Discard buffered state that never reached Matrix after a delivery failure."""
        self.accumulated_text = self._last_delivered_text
        self.tool_trace = deepcopy(self._last_delivered_tool_trace)
        self.chars_since_last_update = 0
        self.placeholder_progress_sent = self._last_placeholder_progress_sent

    def _resolve_stream_status(self, *, is_final: bool, stream_status: str | None) -> str:
        """Return the content status for the current send or edit."""
        if stream_status is not None:
            return stream_status
        if is_final:
            return STREAM_STATUS_COMPLETED
        if self.event_id is None:
            return STREAM_STATUS_PENDING
        return STREAM_STATUS_STREAMING

    async def _record_streaming_send(self, event_id: str, content_sent: dict[str, Any]) -> None:
        """Persist one just-sent streaming message into the conversation cache."""
        if self.conversation_cache is None:
            return
        self.conversation_cache.notify_outbound_message(self.room_id, event_id, content_sent)

    async def _record_streaming_edit(
        self,
        edit_event_id: str,
        *,
        content_sent: dict[str, Any],
    ) -> None:
        """Persist one just-sent streaming edit into the conversation cache."""
        if self.conversation_cache is None or self.event_id is None:
            return
        self.conversation_cache.notify_outbound_message(self.room_id, edit_event_id, content_sent)

    def _mark_first_visible_reply_if_needed(self) -> None:
        """Mark first visible reply timing once visible text exists."""
        if self.pipeline_timing is not None and self.accumulated_text.strip():
            self.pipeline_timing.mark_first_visible_reply("stream_update")

    async def _send_initial_content(self, client: nio.AsyncClient, *, content: dict[str, Any]) -> bool:
        """Send the initial streaming event."""
        delivered = await send_message_result(client, self.room_id, content, config=self.config)
        if delivered is None:
            return False
        self.event_id = delivered.event_id
        if self.visible_event_id_callback is not None:
            self.visible_event_id_callback(delivered.event_id)
        await self._record_streaming_send(delivered.event_id, delivered.content_sent)
        self._mark_first_visible_reply_if_needed()
        logger.debug("Initial streaming message sent", event_id=self.event_id)
        return True

    async def _edit_existing_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
    ) -> bool:
        """Send one streaming edit event for the existing message."""
        assert self.event_id is not None
        delivered = await edit_message_result(
            client,
            self.room_id,
            self.event_id,
            content,
            display_text,
            config=self.config,
        )
        if delivered is None:
            return False
        await self._record_streaming_edit(delivered.event_id, content_sent=delivered.content_sent)
        self._mark_first_visible_reply_if_needed()
        return True

    async def _send_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
        retry_on_failure: bool = False,
        retry_without_backoff: bool = False,
    ) -> bool:
        """Send a new event or edit the existing one."""
        total_attempts = 2 if retry_on_failure or retry_without_backoff else 1
        for attempt in range(1, total_attempts + 1):
            try:
                if self.event_id is None:
                    logger.debug("Sending initial streaming message", attempt=attempt)
                    if await self._send_initial_content(client, content=content):
                        return True
                    logger.error("Failed to send initial streaming message", attempt=attempt)
                else:
                    logger.debug("Editing streaming message", event_id=self.event_id, attempt=attempt)
                    if await self._edit_existing_content(client, content=content, display_text=display_text):
                        return True
                    logger.error("Failed to edit streaming message", attempt=attempt)
            except Exception:
                logger.warning(
                    "Streaming update attempt raised an exception",
                    attempt=attempt,
                    event_id=self.event_id,
                    room_id=self.room_id,
                    exc_info=True,
                )
                if attempt == total_attempts:
                    raise
            if attempt < total_attempts:
                logger.warning(
                    "Retrying failed terminal streaming update immediately",
                    attempt=attempt,
                    event_id=self.event_id,
                    room_id=self.room_id,
                )
        return False


class ReplacementStreamingResponse(StreamingResponse):
    """StreamingResponse variant that replaces content instead of appending.

    Useful for structured live rendering where the full document is rebuilt
    on each tick and we want the message to reflect the latest full view,
    not incremental concatenation.
    """

    def uses_replacement_updates(self) -> bool:
        """Return whether each visible chunk replaces the current body."""
        return True

    def _update(self, new_chunk: str) -> None:
        """Replace accumulated text with new chunk."""
        self.accumulated_text = new_chunk
        self.chars_since_last_update += len(new_chunk)
        self.last_delta_at = time.time()


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
    emit_timing_event(
        "Dispatch tool delivery timing",
        phase="queued",
        queue_size=delivery_queue.qsize(),
        progress_hint=progress_hint,
        force_refresh=force_refresh,
        boundary_refresh=boundary_refresh,
        phase_boundary_flush=phase_boundary_flush,
        allow_empty_progress=allow_empty_progress,
        wait_for_capture=wait_for_capture,
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
    tool_tracker = StreamingToolTracker()

    async for chunk in response_stream:
        if isinstance(chunk, str):
            text_chunk = chunk
        elif isinstance(chunk, StructuredStreamChunk):
            text_chunk = chunk.content
            if chunk.tool_trace is not None:
                streaming.tool_trace = _merge_tool_trace(streaming.tool_trace, chunk.tool_trace)
        elif isinstance(chunk, RunContentEvent):
            if chunk.content:
                text_chunk = str(chunk.content)
            else:
                _queue_delivery_request(delivery_queue, progress_hint=True)
                continue
        elif isinstance(chunk, RunCompletedEvent):
            if chunk.content is not None:
                streaming.canonical_final_body_candidate = str(chunk.content)
            continue
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
            text_chunk, trace_entry = tool_tracker.start(chunk.tool, tool_index=tool_index)
            if trace_entry is not None:
                streaming.tool_trace.append(trace_entry)
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
            completion = tool_tracker.complete(chunk.tool)
            if completion is not None:
                tool_name, result, pending_tool, completed_trace = completion
                if streaming.show_tool_calls:
                    if pending_tool is None or pending_tool.visible_tool_index is None:
                        logger.warning(
                            "Missing pending tool start in streaming response; skipping completion marker",
                            tool_name=tool_name,
                        )
                        _queue_delivery_request(delivery_queue, progress_hint=True)
                        continue
                    tool_index = pending_tool.visible_tool_index
                    prior_delta_at = streaming.last_delta_at
                    previous_text = streaming.accumulated_text
                    streaming.accumulated_text, _ = complete_pending_tool_block(
                        streaming.accumulated_text,
                        tool_name,
                        result,
                        tool_index=tool_index,
                    )
                    text_changed = streaming.accumulated_text != previous_text
                    if text_changed:
                        streaming._mark_nonadditive_text_mutation()
                    if not tool_tracker.update_visible_trace_entry(streaming.tool_trace, pending_tool, completed_trace):
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
            replacement_suffixes=tuple(
                pending.visible_text for pending in tool_tracker.pending_tools if pending.visible_text
            ),
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
                    and merged_request.boundary_refresh_prior_delta_at >= streaming.last_boundary_refresh_at
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


async def send_streaming_response(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    target: MessageTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    response_stream: AsyncIterator[StreamInputChunk],
    *,
    streaming_cls: type[StreamingResponse] = StreamingResponse,
    header: str | None = None,
    existing_event_id: str | None = None,
    adopt_existing_placeholder: bool = False,
    show_tool_calls: bool = True,
    extra_content: dict[str, Any] | None = None,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    visible_event_id_callback: Callable[[str], None] | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
    preserve_existing_visible_on_empty_terminal: bool = False,
) -> StreamTransportOutcome:
    """Stream chunks to a Matrix room and return the canonical transport outcome."""
    sc = config.defaults.streaming
    streaming = streaming_cls(
        target=target,
        config=config,
        runtime_paths=runtime_paths,
        latest_thread_event_id=latest_thread_event_id,
        show_tool_calls=show_tool_calls,
        extra_content=extra_content,
        update_interval=sc.update_interval,
        min_update_interval=sc.min_update_interval,
        interval_ramp_seconds=sc.interval_ramp_seconds,
        max_idle=sc.max_idle,
        pipeline_timing=pipeline_timing,
        conversation_cache=conversation_cache,
        visible_event_id_callback=visible_event_id_callback,
        preserve_existing_visible_on_empty_terminal=preserve_existing_visible_on_empty_terminal,
    )

    # Ensure the first chunk triggers an initial send immediately
    streaming.last_update = float("-inf")

    if existing_event_id:
        streaming.event_id = existing_event_id
        if visible_event_id_callback is not None:
            visible_event_id_callback(existing_event_id)
        streaming.accumulated_text = ""
        streaming.placeholder_progress_sent = adopt_existing_placeholder

    if header:
        await streaming.update_content(header, client)

    worker_progress_queue: asyncio.Queue[WorkerProgressEvent] = asyncio.Queue()
    delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
    progress_task: asyncio.Task[None] | None = None
    delivery_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()
    transport_outcome: StreamTransportOutcome | None = None
    with worker_progress_pump_scope(loop, worker_progress_queue) as pump:
        delivery_task = asyncio.create_task(_drive_stream_delivery(client, streaming, delivery_queue))
        progress_task = asyncio.create_task(
            _drain_worker_progress_events(streaming, worker_progress_queue, pump, delivery_queue),
        )
        try:
            await _consume_stream_with_progress_supervision(
                response_stream,
                streaming,
                progress_task,
                delivery_task,
                delivery_queue,
            )
            progress_error = await _shutdown_worker_progress_drain(pump, progress_task)
            if progress_error is None:
                progress_task = None
            delivery_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            if delivery_error is None:
                delivery_task = None
            if progress_error is not None:
                _raise_progress_delivery_error(progress_error)
            if delivery_error is not None:
                _raise_nonterminal_delivery_error(delivery_error)
        except asyncio.CancelledError as exc:
            cancel_source = classify_cancel_source(exc)
            cleanup_error = await _shutdown_worker_progress_drain(pump, progress_task)
            if cleanup_error is None:
                progress_task = None
            delivery_cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            if delivery_cleanup_error is None:
                delivery_task = None
            if cleanup_error is not None:
                logger.warning(
                    "Worker progress drain raised during cancellation cleanup",
                    error=str(cleanup_error),
                )
            if delivery_cleanup_error is not None:
                logger.warning(
                    "Stream delivery controller raised during cancellation cleanup",
                    error=str(delivery_cleanup_error),
                )
                if isinstance(delivery_cleanup_error, _StreamDeliveryShutdownTimeoutError):
                    streaming.restore_last_delivered_state()
                    raise _build_streaming_delivery_error(
                        streaming,
                        delivery_cleanup_error,
                        failure_reason=cancel_failure_reason(cancel_source),
                        terminal_status="cancelled",
                        tool_trace_collector=tool_trace_collector,
                    ) from delivery_cleanup_error
            log_cancelled_response(
                logger,
                exc=exc,
                message_id=streaming.event_id,
                restart_message="Streaming response interrupted by sync restart",
                user_stop_message="Streaming response cancelled by user",
                interrupted_message="Streaming response interrupted — traceback for diagnosis",
            )
            transport_outcome = await streaming.finalize(client, cancel_source=cancel_source)
            raise StreamingDeliveryError(
                exc,
                event_id=streaming.event_id,
                accumulated_text=streaming.accumulated_text,
                tool_trace=streaming.tool_trace,
                transport_outcome=transport_outcome,
            ) from exc
        except Exception as exc:
            delivery_error = exc.error if isinstance(exc, _NonTerminalDeliveryError) else exc
            logger.exception("Streaming response failed", error=str(delivery_error))
            cleanup_error = await _shutdown_worker_progress_drain(pump, progress_task)
            if cleanup_error is None:
                progress_task = None
            delivery_cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            if delivery_cleanup_error is None:
                delivery_task = None
            if cleanup_error is not None and cleanup_error is not delivery_error:
                logger.warning(
                    "Worker progress drain raised during error cleanup",
                    error=str(cleanup_error),
                )
            if delivery_cleanup_error is not None and delivery_cleanup_error is not delivery_error:
                logger.warning(
                    "Stream delivery controller raised during error cleanup",
                    error=str(delivery_cleanup_error),
                )
            shutdown_timeout = None
            if isinstance(delivery_error, _StreamDeliveryShutdownTimeoutError):
                shutdown_timeout = delivery_error
            elif isinstance(delivery_cleanup_error, _StreamDeliveryShutdownTimeoutError):
                shutdown_timeout = delivery_cleanup_error
            if shutdown_timeout is not None:
                streaming.restore_last_delivered_state()
                raise _build_streaming_delivery_error(
                    streaming,
                    shutdown_timeout,
                    failure_reason=str(shutdown_timeout),
                    terminal_status="error",
                    tool_trace_collector=tool_trace_collector,
                ) from shutdown_timeout
            if isinstance(exc, _NonTerminalDeliveryError):
                streaming.restore_last_delivered_state()
            transport_outcome = await streaming.finalize(client, error=delivery_error)
            raise StreamingDeliveryError(
                delivery_error,
                event_id=streaming.event_id,
                accumulated_text=streaming.accumulated_text,
                tool_trace=streaming.tool_trace,
                transport_outcome=transport_outcome,
            ) from delivery_error
        else:
            transport_outcome = await streaming.finalize(client)
        finally:
            cleanup_error = await _shutdown_worker_progress_drain(pump, progress_task)
            if cleanup_error is not None:
                logger.warning(
                    "Worker progress drain raised during final cleanup",
                    error=str(cleanup_error),
                )
            delivery_cleanup_error = await _shutdown_stream_delivery(delivery_queue, delivery_task)
            if delivery_cleanup_error is not None:
                logger.warning(
                    "Stream delivery controller raised during final cleanup",
                    error=str(delivery_cleanup_error),
                )
            if tool_trace_collector is not None:
                tool_trace_collector[:] = streaming.tool_trace

    assert transport_outcome is not None
    return transport_outcome
