"""Streaming response state machine: placeholder, progressive edits, tool traces, cancellation."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn

from mindroom import interactive
from mindroom.constants import (
    REASONING_CONTENT_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
    STREAM_WARMUP_SUFFIX_KEY,
    runtime_matrix_homeserver,
)
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.legacy_streaming import has_legacy_terminal_suffix, strip_legacy_terminal_suffixes
from mindroom.live_frames.frames import LIVE_CHECKPOINT_KEY, LiveDocs
from mindroom.live_frames.publisher import LivePublisher
from mindroom.live_frames.transport import homeserver_relays_live_frames
from mindroom.llm.events import ReasoningDelta, RunFinished, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import build_edit_event_content, edit_message_result
from mindroom.matrix.large_messages import should_send_oversized_nonterminal_streaming_edit
from mindroom.matrix.mentions import format_message_with_mentions, visible_markdown
from mindroom.matrix.message_builder import markdown_to_html
from mindroom.orchestration.runtime import (
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    CancelSource,
    current_task_is_process_shutdown,
    log_cancelled_response,
    request_task_cancel,
)
from mindroom.streaming_warmup import RenderedWarmupLine, WorkerWarmupState
from mindroom.timing import emit_timing_event
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import (
    FailedReply,
    StreamingToolTracker,
    StructuredStreamChunk,
    complete_pending_tool_block,
    remap_visible_tool_marker_indices,
    serialize_tool_trace,
    strip_visible_tool_markers,
    tool_markers_match_trace,
)
from mindroom.tool_system.runtime_context import worker_progress_pump_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_delivery import DeliveredMatrixEvent
    from mindroom.message_target import MessageTarget
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import WorkerProgressEvent, WorkerProgressPump

logger = get_logger(__name__)

__all__ = [
    "CANCELLED_RESPONSE_NOTE",
    "INTERRUPTED_RESPONSE_NOTE",
    "PROGRESS_PLACEHOLDER",
    "RESTART_INTERRUPTED_RESPONSE_NOTE",
    "SYNC_RESTART_CANCEL_MSG",
    "TEAM_PROGRESS_PLACEHOLDER",
    "USER_STOP_CANCEL_MSG",
    "CancelSource",
    "FinalTextTransform",
    "PausePresentationError",
    "ProgressDelivered",
    "ProgressPermission",
    "ProgressPublisher",
    "ProgressState",
    "ProgressWriteAhead",
    "ReplacementStreamingResponse",
    "StreamInputChunk",
    "StreamingDeliveryError",
    "StreamingLifecycleSuspensionError",
    "StreamingPresentation",
    "StreamingResponse",
    "TerminalEdit",
    "TerminalSend",
    "UnfinishedStreamedReply",
    "build_restart_interrupted_body",
    "clean_partial_reply_text",
    "current_task_is_process_shutdown",
    "format_stream_error_note",
    "interactive_response_for_visible_body",
    "is_interrupted_partial_reply",
    "send_streaming_response",
    "stream_progress_edits",
    "strip_matching_visible_tool_markers",
]

_PROGRESS_PLACEHOLDER = "Thinking..."
TEAM_PROGRESS_PLACEHOLDER = "🤝 Team Response: Thinking..."
_IN_PROGRESS_STREAM_STATUSES = frozenset({STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING})


@dataclass(frozen=True, slots=True)
class StreamingPresentation:
    """Ordered response state known to have reached Matrix before suspension."""

    response_text: str
    rendered_response_text: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    state: dict[str, object] | None = None


class StreamingLifecycleSuspensionError(Exception):
    """A response-stream exit that must reach the lifecycle owner unchanged."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.presentation: StreamingPresentation | None = None

    def capture_presentation(self, presentation: StreamingPresentation) -> None:
        """Attach the last transport-committed presentation before handoff."""
        self.presentation = presentation


PROGRESS_PLACEHOLDER = _PROGRESS_PLACEHOLDER
CANCELLED_RESPONSE_NOTE = "**[Response cancelled by user]**"
INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted]**"
_INTERRUPTED_RESPONSE_NOTE = INTERRUPTED_RESPONSE_NOTE
RESTART_INTERRUPTED_RESPONSE_NOTE = "**[Response interrupted by service restart]**"
_STREAM_ERROR_RESPONSE_NOTE = "**[Response interrupted by an error"

StreamInputChunk = (
    str
    | StructuredStreamChunk
    | TextDelta
    | ReasoningDelta
    | FailedReply
    | RunFinished
    | ToolStarted
    | ToolProgress
    | ToolCompleted
)
# How long queued and in-flight progress edits get to land before stopping the stream cancels them.
_STREAM_DELIVERY_DRAIN_TIMEOUT_SECONDS = 5.0


class _NonTerminalDeliveryError(Exception):
    """Internal wrapper for non-terminal delivery failures."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def strip_matching_visible_tool_markers(text: str, tool_trace: Sequence[ToolTraceEntry]) -> str:
    """Strip display-only markers only when they exactly represent the structured trace."""
    if not tool_markers_match_trace(text, tool_trace):
        return text
    return strip_visible_tool_markers(text)


class StreamingDeliveryError(Exception):
    """A stream that stopped on a failure, without writing an ending: the reply's span exit writes it from ``error``."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class PausePresentationError(StreamingDeliveryError):
    """The stream paused for approval but could not show the paused presentation."""


def _raise_nonterminal_delivery_error(error: Exception) -> NoReturn:
    """Raise one wrapped non-terminal delivery error for unified rollback handling."""
    raise _NonTerminalDeliveryError(error) from error


def _complete_capture_completions(capture_completions: tuple[asyncio.Future[None], ...]) -> None:
    for capture_completion in capture_completions:
        if not capture_completion.done():
            capture_completion.set_result(None)


def format_stream_error_note(error: Exception | str) -> str:
    """Return a concise user-facing note for stream-time exceptions."""
    normalized_error = " ".join(str(error).split())
    if not normalized_error:
        return f"{_STREAM_ERROR_RESPONSE_NOTE}. Please retry.]**"
    if len(normalized_error) > 220:
        normalized_error = f"{normalized_error[:219]}…"
    return f"{_STREAM_ERROR_RESPONSE_NOTE}: {normalized_error}]**"


# The notes that end a reply a Stop, a restart, or an interruption cut short, in the order their stripping checks them.
_PARTIAL_REPLY_NOTES = (CANCELLED_RESPONSE_NOTE, _INTERRUPTED_RESPONSE_NOTE, RESTART_INTERRUPTED_RESPONSE_NOTE)


def is_interrupted_partial_reply(text: object) -> bool:
    """Return True when text carries a terminal interrupted partial-reply marker."""
    if not isinstance(text, str):
        return False
    trimmed_text = text.rstrip()
    return (
        has_legacy_terminal_suffix(trimmed_text)
        or trimmed_text.endswith(_PARTIAL_REPLY_NOTES)
        or (_STREAM_ERROR_RESPONSE_NOTE in trimmed_text)
    )


def clean_partial_reply_text(text: str) -> str:
    """Strip partial-reply status notes from persisted text."""
    cleaned = strip_legacy_terminal_suffixes(text.rstrip())

    for marker in _PARTIAL_REPLY_NOTES:
        if cleaned.endswith(marker):
            cleaned = cleaned[: -len(marker)].rstrip()

    if _STREAM_ERROR_RESPONSE_NOTE in cleaned:
        cleaned = cleaned.split(_STREAM_ERROR_RESPONSE_NOTE, 1)[0].rstrip()

    if cleaned in {_PROGRESS_PLACEHOLDER, TEAM_PROGRESS_PLACEHOLDER} or not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


def build_restart_interrupted_body(text: str) -> str:
    """Return restart-note text for a stale in-progress message body."""
    stripped_text = text.rstrip()
    if not stripped_text or stripped_text == _PROGRESS_PLACEHOLDER:
        return RESTART_INTERRUPTED_RESPONSE_NOTE
    return f"{stripped_text}\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}"


@dataclass(frozen=True)
class UnfinishedStreamedReply:
    """What one reply showed when the process streaming it stopped before finishing it."""

    # The visible text as the user saw it, tool markers included.
    visible_text: str
    tool_trace: tuple[ToolTraceEntry, ...]

    @property
    def partial_text(self) -> str:
        """Return the prose alone, without the display-only tool markers."""
        return strip_visible_tool_markers(self.visible_text).strip()

    @property
    def resumed_text(self) -> str:
        """Return the text a continuation streams below: what was shown, then the restart note."""
        return f"{build_restart_interrupted_body(self.visible_text)}\n\n"


@dataclass(frozen=True)
class _CommittedDeliveryState:
    """One frozen stream state that definitely reached Matrix."""

    accumulated_text: str
    tool_trace: list[ToolTraceEntry]
    presentation_state: dict[str, object] | None
    placeholder_progress_sent: bool
    rendered_body: str
    visible_body_state: Literal["placeholder_only", "visible_body"]
    interactive_metadata: interactive.InteractiveMetadata | None
    stream_status: str
    # The text before the final transform reshaped it, when it did.
    untransformed_text: str | None = None


def _normalize_stream_accumulated_text(text: str) -> str:
    """Normalize whitespace-only placeholder buffers to the committed empty state."""
    return text if text.strip() else ""


def _tool_trace_identity(entry: ToolTraceEntry) -> tuple[object, ...]:
    """Return every field that determines one trace entry's durable identity."""
    return (
        entry.type,
        entry.tool_name,
        entry.args_preview,
        entry.result_preview,
        entry.truncated,
        entry.tool_call_id,
        entry.member_id,
        entry.parent_bash_call_id,
        entry.toolkit_name,
        entry.is_error,
    )


def _tool_traces_match(
    left: list[ToolTraceEntry] | tuple[ToolTraceEntry, ...],
    right: list[ToolTraceEntry] | tuple[ToolTraceEntry, ...],
) -> bool:
    """Compare trace presentation and continuation identity together."""
    return tuple(map(_tool_trace_identity, left)) == tuple(map(_tool_trace_identity, right))


def interactive_response_for_visible_body(
    visible_body: str,
    *,
    canonical_body_candidate: str | None,
    stream_interactive_metadata: interactive.InteractiveMetadata | None = None,
) -> interactive._InteractiveResponse:
    """Return interactive metadata only when it belongs to the visible body."""
    if stream_interactive_metadata is not None:
        return interactive._InteractiveResponse(visible_body, stream_interactive_metadata)

    visible_response = interactive.parse_and_format_interactive(visible_body, extract_mapping=True)
    if visible_response.interactive_metadata is not None:
        return visible_response

    if canonical_body_candidate is None or canonical_body_candidate == visible_body:
        return visible_response

    canonical_response = interactive.parse_and_format_interactive(
        canonical_body_candidate,
        extract_mapping=True,
    )
    if canonical_response.interactive_metadata is not None and canonical_response.formatted_text == visible_body:
        return canonical_response

    return visible_response


@dataclass(frozen=True, slots=True)
class _PreparedStreamingDelivery:
    """One frozen delivery attempt, non-terminal or terminal."""

    content: dict[str, Any]
    display_text: str
    committed_state: _CommittedDeliveryState
    had_warmup_suffix: bool
    # The stream's pending-character count when this delivery was prepared.
    chars_since_last_update: int


@dataclass(frozen=True, slots=True)
class _StreamingDeliverySnapshot:
    """Immutable state needed to format one outbound streaming payload."""

    config: Config
    runtime_paths: RuntimePaths
    accumulated_text: str
    event_id: str | None
    effective_thread_id: str | None
    reply_to_event_id: str | None
    latest_thread_event_id: str | None
    room_mode: bool
    show_tool_calls: bool
    tool_trace: tuple[ToolTraceEntry, ...]
    presentation_state: dict[str, object] | None
    extra_content: Mapping[str, Any] | None
    warmup_suffix_lines: tuple[RenderedWarmupLine, ...]
    stream_status: str
    interactive_creator_agent: str | None
    interactive_source_event_id: str | None
    markdown_renderer: Callable[[str], str]
    chars_since_last_update: int
    untransformed_text: str | None = None


def _progress_state(committed: _CommittedDeliveryState) -> ProgressState:
    return ProgressState(
        text=committed.accumulated_text,
        tool_trace=tuple(committed.tool_trace),
        presentation_state=deepcopy(committed.presentation_state),
        placeholder_only=committed.visible_body_state == "placeholder_only",
        untransformed_text=committed.untransformed_text,
    )


def _visible_text(
    accumulated_text: str,
    *,
    in_progress: bool,
) -> tuple[str, interactive.InteractiveMetadata | None]:
    """Return the text a reply shows for its accumulated text, and the interactive question it carries.

    Progress edits and live frames both show this text, so they never disagree about what is visible.
    """
    text_to_send = accumulated_text if accumulated_text.strip() else _PROGRESS_PLACEHOLDER
    # A block still arriving is unfinished, not malformed, so only the final text warns about unparsed blocks.
    response = interactive.parse_and_format_interactive(
        text_to_send,
        extract_mapping=True,
        warn_unparsed=not in_progress,
    )
    display_text = response.formatted_text
    if in_progress:
        # A cut-off mention can be another valid user ID (@grace:matrix of @grace:matrix.localhost), so it waits.
        last_word = display_text.rsplit(maxsplit=1)[-1] if display_text[-1:].strip() else ""
        if "@" in last_word:
            display_text = display_text.removesuffix(last_word).rstrip()
        # An interactive block still arriving is raw JSON; its question renders once the block closes.
        display_text = interactive.hide_unfinished_interactive(display_text) or _PROGRESS_PLACEHOLDER
    return display_text, response.interactive_metadata


def _prepare_delivery_from_snapshot(snapshot: _StreamingDeliverySnapshot) -> _PreparedStreamingDelivery:
    """Format one outbound payload from immutable stream state."""
    in_progress = snapshot.stream_status in _IN_PROGRESS_STREAM_STATUSES
    display_text, interactive_metadata = _visible_text(snapshot.accumulated_text, in_progress=in_progress)

    latest_for_message = (
        snapshot.latest_thread_event_id if snapshot.event_id is None and not snapshot.room_mode else None
    )
    extra_content = dict(snapshot.extra_content or {})
    extra_content[STREAM_STATUS_KEY] = snapshot.stream_status
    if (
        snapshot.stream_status == STREAM_STATUS_COMPLETED
        and interactive_metadata is not None
        and snapshot.interactive_creator_agent is not None
        and snapshot.interactive_source_event_id is not None
    ):
        extra_content.update(
            interactive.build_prompt_content(
                interactive_metadata,
                creator_agent=snapshot.interactive_creator_agent,
                source_event_id=snapshot.interactive_source_event_id,
            ),
        )
    tool_trace = list(snapshot.tool_trace)

    content = format_message_with_mentions(
        config=snapshot.config,
        runtime_paths=snapshot.runtime_paths,
        text=display_text,
        thread_event_id=snapshot.effective_thread_id if snapshot.event_id is None else None,
        reply_to_event_id=snapshot.reply_to_event_id if snapshot.event_id is None else None,
        latest_thread_event_id=latest_for_message,
        tool_trace=tool_trace if snapshot.show_tool_calls else None,
        extra_content=extra_content,
        markdown_renderer=snapshot.markdown_renderer,
    )
    if in_progress:
        # Matrix suppresses m.notice before evaluating mention rules. Streaming
        # updates may already contain mentions, so using m.text here would make
        # every progressive edit eligible for a push notification.
        content["msgtype"] = "m.notice"
    canonical_visible_body = content["body"]
    if snapshot.warmup_suffix_lines:
        content[STREAM_VISIBLE_BODY_KEY] = canonical_visible_body
        warmup_suffix = "\n".join(line.text for line in snapshot.warmup_suffix_lines)
        content[STREAM_WARMUP_SUFFIX_KEY] = warmup_suffix
        display_text = f"{display_text}\n\n{warmup_suffix}" if display_text else warmup_suffix
        content["body"] = f"{content['body']}\n\n{warmup_suffix}"
        suffix_html = "".join(f"<p>{line.html}</p>" for line in snapshot.warmup_suffix_lines)
        content["formatted_body"] = f"{content['formatted_body']}{suffix_html}"

    return _PreparedStreamingDelivery(
        content=content,
        display_text=display_text,
        committed_state=_CommittedDeliveryState(
            accumulated_text=_normalize_stream_accumulated_text(snapshot.accumulated_text),
            tool_trace=tool_trace,
            presentation_state=deepcopy(snapshot.presentation_state),
            placeholder_progress_sent=not snapshot.accumulated_text.strip(),
            rendered_body=canonical_visible_body,
            visible_body_state=(
                "placeholder_only" if canonical_visible_body == _PROGRESS_PLACEHOLDER else "visible_body"
            ),
            interactive_metadata=interactive_metadata,
            stream_status=snapshot.stream_status,
            untransformed_text=snapshot.untransformed_text,
        ),
        had_warmup_suffix=bool(snapshot.warmup_suffix_lines),
        chars_since_last_update=snapshot.chars_since_last_update,
    )


@dataclass(frozen=True, slots=True)
class ProgressState:
    """What one direct progress edit of a reply shows."""

    text: str
    tool_trace: tuple[ToolTraceEntry, ...]
    presentation_state: dict[str, object] | None
    placeholder_only: bool
    # The whole reply before the final transform reshaped it into ``text``, when it did.
    untransformed_text: str | None = None


class ProgressPermission(Enum):
    """What a reply's records allow for one direct progress edit."""

    SEND = "send"
    # An earlier durable write of the reply is unresolved; a later edit shows this progress.
    DEFER = "defer"
    REFUSE = "refuse"


type ProgressWriteAhead = Callable[[ProgressState], Awaitable[ProgressPermission]]
# Reports the progress edit that landed on the reply's event.
type ProgressDelivered = Callable[[ProgressState, str], None]
type TerminalEdit = Callable[..., Awaitable[DeliveredMatrixEvent | None]]
# The same contract for a stream whose answer is its first visible event: no
# placeholder was sent, so the terminal update is a send rather than an edit.
type TerminalSend = Callable[..., Awaitable[DeliveredMatrixEvent | None]]
# The answer text once the stream has ended, in and transformed out.
type FinalTextTransform = Callable[[str], Awaitable[str]]
# One complete presentation of a response whose terminal delivery has another owner.
type ProgressPublisher = Callable[[StructuredStreamChunk], Awaitable[None]]


@dataclass
class StreamingResponse:
    """Manages a streaming response with incremental message updates."""

    # Whether a text chunk replaces the reply's body, as a team's whole documents do, instead of continuing it.
    replaces_text: ClassVar[bool] = False
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
    presentation_state: dict[str, object] | None = None
    extra_content: Mapping[str, Any] | None = None
    interactive_creator_agent: str | None = None
    interactive_source_event_id: str | None = None
    stream_started_at: float | None = None
    chars_since_last_update: int = 0
    last_delta_at: float | None = None
    last_boundary_refresh_at: float | None = None
    placeholder_progress_sent: bool = False
    pipeline_timing: DispatchPipelineTiming | None = None
    visible_event_id_callback: Callable[[str], None] | None = None
    visible_progress_callback: Callable[[str, Sequence[ToolTraceEntry]], None] | None = None
    # The stream writes its reply through the reply's records. Each direct
    # progress edit is recorded before it is sent: ``progress_write_ahead``
    # defers an edit the records hold back, and refuses one they reject, which
    # fails the stream like a refused edit; ``progress_delivered`` reports the
    # edit that landed.
    progress_write_ahead: ProgressWriteAhead = field(kw_only=True)
    progress_delivered: ProgressDelivered = field(kw_only=True)
    # The first visible create and the terminal update are the reply's rows.
    # A progress-only stream edits an existing reply whose terminal update its
    # caller owns, so it has none of these.
    initial_send: TerminalSend | None = None
    terminal_send: TerminalSend | None = None
    terminal_edit: TerminalEdit | None = None
    # A caller may still be deciding whether this turn should become visible.
    # Terminal cleanup can finish an owned event, but must not invent a reply.
    allow_new_terminal_message: Callable[[], bool] | None = None
    # Applied to the answer text once, before the terminal payload is built.
    # Run afterwards it would need a second edit, outside the outbox and after
    # the durable row was acknowledged, leaving the row holding one body while
    # the room shows another. Formatted text, mentions, the canonical visible
    # body and the warmup suffix all derive from this one string, so
    # transforming it here keeps them consistent with nothing to rebuild.
    final_text_transform: FinalTextTransform | None = None
    # Whether direct transport still speaks for a membership the bot is in.
    # Progressive edits are not a turn's answer, so they never reach the
    # outbox and nothing else would stop them writing into a conversation the
    # fence has already invalidated. The terminal delivery is exempt: it goes
    # through the outbox, which knows the difference between refusing an
    # answer and stranding one the homeserver may already hold.
    transport_is_current: Callable[[], Awaitable[bool]] | None = None
    canonical_final_body_candidate: str | None = None
    # What a stopped attempt at this reply showed, which stays above the
    # continuation: its text ends in the restart note, and its tool calls keep
    # the first trace slots so the continuation's markers number after them.
    resumed_text: str = ""
    resumed_tool_trace: tuple[ToolTraceEntry, ...] = ()
    _warmup_state: WorkerWarmupState = field(default_factory=WorkerWarmupState, init=False, repr=False)
    # Reuse only the last Markdown render; mentions and delivery metadata stay fresh.
    _render_markdown: Callable[[str], str] = field(
        default_factory=lambda: lru_cache(maxsize=1)(markdown_to_html),
        init=False,
        repr=False,
    )
    _last_delivered_text: str = field(default="", init=False, repr=False)
    _last_delivered_tool_trace: list[ToolTraceEntry] = field(default_factory=list, init=False, repr=False)
    _last_delivered_presentation_state: dict[str, object] | None = field(default=None, init=False, repr=False)
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
    # The model's reasoning this stream showed, which the terminal update keeps, and how long it was observed.
    reasoning_text: str = field(default="", init=False)
    _reasoning_segment_started_at: float | None = field(default=None, init=False, repr=False)
    _reasoning_seconds: float = field(default=0.0, init=False, repr=False)
    # The latest output each running tool call reported, by call ID; only live frames show it.
    tool_progress: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    # This reply's live frames, once the homeserver relays them; durable edits are then checkpoints.
    live: LivePublisher | None = field(default=None, init=False, repr=False)
    # Frames start once the reply's event shows this stream: its adopted placeholder or its first progress write.
    _shows_stream: bool = field(default=False, init=False, repr=False)
    _transport_ended: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Derive Matrix delivery fields from the canonical target."""
        self.room_id = self.target.room_id
        self.thread_id = self.target.resolved_thread_id
        self.reply_to_event_id = self.target.reply_to_event_id
        self.room_mode = self.target.is_room_mode
        if self.resumed_text or self.resumed_tool_trace:
            # The stopped attempt's part is already visible in Matrix, so a failed
            # edit rolls back to it, and no terminal path treats it as a placeholder.
            self.accumulated_text = self._last_delivered_text = self.resumed_text
            self.tool_trace = list(self.resumed_tool_trace)
            self._last_delivered_tool_trace = list(self.resumed_tool_trace)
            self._last_committed_rendered_body = self.resumed_text.rstrip()
            self._last_committed_visible_body_state = "visible_body"

    def _update(self, new_chunk: str) -> None:
        """Append new chunk to accumulated text."""
        self._append_incremental_text(new_chunk)

    def add_reasoning(self, text: str, *, started_at: float | None = None) -> None:
        """Append model reasoning, starting a new segment after other output at ``started_at``, or now when unknown."""
        if self._reasoning_segment_started_at is None:
            self._reasoning_segment_started_at = time.monotonic() if started_at is None else started_at
            if self.reasoning_text and not self.reasoning_text.endswith("\n"):
                self.reasoning_text += "\n\n"
        self.reasoning_text += text

    def end_reasoning_segment(self) -> None:
        """Close the current reasoning segment, if any, adding its observed time."""
        if self._reasoning_segment_started_at is not None:
            self._reasoning_seconds += time.monotonic() - self._reasoning_segment_started_at
            self._reasoning_segment_started_at = None

    @property
    def live_active(self) -> bool:
        """Whether live frames currently carry this reply's progress, making durable edits checkpoints."""
        return (
            self.live is not None
            and self.live.running
            and self._shows_stream
            and not self._transport_ended
            and self.event_id is not None
        )

    def live_state(self) -> tuple[str, LiveDocs] | None:
        """Return the reply's event id and what a live frame shows now, or None while frames must wait."""
        if not self.live_active:
            return None
        assert self.event_id is not None
        display_text, _ = _visible_text(self.accumulated_text, in_progress=True)
        md = (
            ""
            if display_text == _PROGRESS_PLACEHOLDER
            else visible_markdown(self.config, self.runtime_paths, display_text)
        )
        warmup_lines = self._warmup_state.render_lines(show_tool_calls=self.show_tool_calls)
        tools = serialize_tool_trace(self.tool_trace) if self.show_tool_calls else ()
        if tools and self.tool_progress:
            tools = tuple(
                {**entry, "progress": self.tool_progress[trace.tool_call_id]}
                if trace.tool_call_id in self.tool_progress
                else entry
                for trace, entry in zip(self.tool_trace, tools, strict=True)
            )
        return self.event_id, LiveDocs(
            md=md,
            reasoning=self.reasoning_text,
            tools=tools,
            status=self._resolve_stream_status(is_final=False),
            activity="\n".join(line.text for line in warmup_lines),
        )

    def apply_tool_trace_snapshot(self, incoming: list[ToolTraceEntry]) -> None:
        """Merge one trace snapshot after the stopped attempt's calls, which keep the first slots."""
        resumed_count = len(self.resumed_tool_trace)
        self.tool_trace = [
            *self.tool_trace[:resumed_count],
            *_merge_tool_trace(self.tool_trace[resumed_count:], incoming),
        ]

    def _append_incremental_text(self, new_chunk: str) -> None:
        """Append additive streaming text regardless of snapshot replacement mode."""
        self.accumulated_text += new_chunk
        self.chars_since_last_update += len(new_chunk)
        self.last_delta_at = time.time()

    def _mark_nonadditive_text_mutation(self) -> None:
        """Count a visible in-place text change that did not append characters as one received character."""
        self.chars_since_last_update += 1
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
        prepared_delivery: _PreparedStreamingDelivery,
        *,
        boundary_refresh: bool = False,
    ) -> None:
        """Advance throttle state after one non-terminal send or edit reached Matrix."""
        now = time.time()
        if self.stream_started_at is None:
            self.stream_started_at = now
        self.last_update = now
        if self._live_state_matches(prepared_delivery.committed_state):
            self.last_delta_at = now
            self.last_boundary_refresh_at = now if boundary_refresh else None
            self.chars_since_last_update = 0
            return

        # The outbound payload was captured before newer live state arrived.
        # What the stream received after the capture stays pending, and
        # boundary-refresh eligibility stays intact, so a later request
        # delivers it when the throttle allows. The single delivery owner
        # captures and lands each edit, so the counter only grows in between.
        # A tool-trace or presentation change alone adds nothing here, just as
        # it adds nothing when it arrives between edits.
        self.last_boundary_refresh_at = None
        self.chars_since_last_update -= prepared_delivery.chars_since_last_update

    def _live_state_matches(self, captured: _CommittedDeliveryState) -> bool:
        """Return whether the live text, tool trace, and presentation still equal one captured state."""
        return (
            _normalize_stream_accumulated_text(self.accumulated_text) == captured.accumulated_text
            and _tool_traces_match(self.tool_trace, captured.tool_trace)
            and self.presentation_state == captured.presentation_state
        )

    def matching_inflight_nonterminal_capture(self) -> asyncio.Future[None] | None:
        """Return the current in-flight capture when it already froze the live state."""
        if self._inflight_nonterminal_capture is None or self._inflight_nonterminal_capture_state is None:
            return None
        if self._live_state_matches(self._inflight_nonterminal_capture_state):
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
        if self.live_active:
            # Frames carry progress; a durable edit is a checkpoint at the steady interval.
            if elapsed_since_last_update >= self.update_interval:
                await self._send_or_edit_message(
                    client,
                    # A finished warm-up line is cleared even before any text arrived.
                    allow_empty_progress=self._warmup_state.needs_warmup_clear_edit,
                    capture_completions=capture_completions,
                )
            else:
                _complete_capture_completions(capture_completions)
            return
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

    def _place_final_only_continuation(self, canonical_final_body_candidate: str | None) -> None:
        """Put a continuation that arrived only as its final event below the stopped attempt it resumes."""
        if (
            canonical_final_body_candidate is not None
            and self.resumed_text
            and not self.accumulated_text.removeprefix(self.resumed_text).strip()
        ):
            self.accumulated_text = self.resumed_text + canonical_final_body_candidate

    async def finalize(self, client: nio.AsyncClient) -> StreamTransportOutcome:
        """Send a finished stream's terminal update and return immutable transport facts.

        Only a stream that ran to its end finalizes; a cancelled or failed one writes nothing, and its reply's span
        exit ends the reply.
        """
        self._warmup_state.clear_for_terminal_transition()
        self.end_reasoning_segment()
        canonical_final_body_candidate = self.canonical_final_body_candidate
        if canonical_final_body_candidate is None and self.accumulated_text.strip():
            canonical_final_body_candidate = self.accumulated_text
        if (
            self.event_id is None
            and self.allow_new_terminal_message is not None
            and not self.allow_new_terminal_message()
        ):
            return StreamTransportOutcome(
                last_physical_stream_event_id=None,
                rendered_body=None,
                visible_body_state="none",
            )
        # When a placeholder message exists but no real text arrived,
        # still edit the message to finalize the stream status.
        has_placeholder = (
            self.event_id is not None and self.placeholder_progress_sent and not self.accumulated_text.strip()
        )
        self._place_final_only_continuation(self.canonical_final_body_candidate)
        if (
            not self.accumulated_text.strip()
            and canonical_final_body_candidate is not None
            and self._last_committed_visible_body_state != "visible_body"
        ):
            return self._committed_outcome(canonical_final_body_candidate)
        if not self.accumulated_text.strip() and not has_placeholder:
            # Nothing to send: no text arrived and no placeholder waits for an ending.
            return StreamTransportOutcome(
                last_physical_stream_event_id=self.event_id,
                rendered_body=None,
                visible_body_state="none",
                canonical_final_body_candidate=canonical_final_body_candidate,
            )
        try:
            final_delivery = await self._prepare_delivery_async(is_final=True, allow_empty_progress=has_placeholder)
            # A stream with text, or a placeholder it may end with nothing, always has a final payload.
            assert final_delivery is not None
            send_succeeded = await self._send_prepared_delivery(
                client,
                prepared_delivery=final_delivery,
                is_final=True,
                retry_on_failure=True,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Terminal streaming update was cancelled before it landed",
                event_id=self.event_id,
                room_id=self.room_id,
                exc_info=not current_task_is_process_shutdown(),
            )
            # The span exit notes the cancellation; whatever this update did is in the reply's records.
            raise
        except Exception as exc:
            logger.warning(
                "Terminal streaming update raised after retries",
                event_id=self.event_id,
                room_id=self.room_id,
                reason=str(exc),
                exc_info=True,
            )
            return self._committed_outcome(canonical_final_body_candidate, failed=True)
        if not send_succeeded:
            logger.warning("Failed to persist terminal stream status", event_id=self.event_id, room_id=self.room_id)
            return self._committed_outcome(canonical_final_body_candidate, failed=True)
        # What the room shows is the payload that landed, which the final text transform may have reshaped.
        shown = final_delivery.committed_state
        return StreamTransportOutcome(
            last_physical_stream_event_id=self.event_id,
            rendered_body=shown.rendered_body,
            visible_body_state=shown.visible_body_state,
            canonical_final_body_candidate=canonical_final_body_candidate,
            interactive_metadata=shown.interactive_metadata,
        )

    async def _send_or_edit_message(
        self,
        client: nio.AsyncClient,
        *,
        allow_empty_progress: bool = False,
        boundary_refresh: bool = False,
        force_nonterminal_delivery: bool = False,
        capture_completions: tuple[asyncio.Future[None], ...] = (),
    ) -> bool:
        """Send one progress update: the stream's first message, or an edit of it; ``finalize`` sends the terminal one."""
        prepared_delivery = await self._prepare_delivery_async(
            is_final=False,
            allow_empty_progress=allow_empty_progress,
        )
        if prepared_delivery is None:
            return True

        return await self._send_prepared_delivery(
            client,
            prepared_delivery=prepared_delivery,
            is_final=False,
            boundary_refresh=boundary_refresh,
            force_nonterminal_delivery=force_nonterminal_delivery,
            capture_completions=capture_completions,
        )

    async def _send_prepared_delivery(
        self,
        client: nio.AsyncClient,
        *,
        prepared_delivery: _PreparedStreamingDelivery,
        is_final: bool,
        boundary_refresh: bool = False,
        force_nonterminal_delivery: bool = False,
        capture_completions: tuple[asyncio.Future[None], ...] = (),
        retry_on_failure: bool = False,
    ) -> bool:
        """Send one already-prepared non-terminal or terminal payload."""
        is_initial_send = self.event_id is None
        if (
            not is_final
            and not is_initial_send
            and not force_nonterminal_delivery
            and not self._should_send_prepared_nonterminal_edit(prepared_delivery)
        ):
            _complete_capture_completions(capture_completions)
            return True
        if not is_final and not await self._direct_transport_allowed():
            # Nothing owed and nothing failed. The room this stream is writing
            # into is not one this bot is in any more, so there is no delivery
            # left to make and no failure to report about not making it --
            # reporting one would only produce another notice for the same
            # room. The answer's own edit is exempt: it goes through the
            # outbox, which can tell refusing an answer apart from stranding
            # one the homeserver may already hold.
            _complete_capture_completions(capture_completions)
            return True
        if (
            not is_final
            and not is_initial_send
            and not await self._progress_permitted(
                prepared_delivery,
                capture_completions,
            )
        ):
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
                is_final=is_final,
                progress=_progress_state(prepared_delivery.committed_state),
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

        self._mark_first_visible_reply_if_needed(prepared_delivery.committed_state)
        if not is_final:
            self._shows_stream = True
            if not is_initial_send and self.event_id is not None:
                self.progress_delivered(_progress_state(prepared_delivery.committed_state), self.event_id)
            self._warmup_state.note_nonterminal_delivery(
                had_warmup_suffix=prepared_delivery.had_warmup_suffix,
            )
            self._mark_delivery_committed(prepared_delivery.committed_state)
            self._mark_nonterminal_delivery(prepared_delivery, boundary_refresh=boundary_refresh)
        else:
            self.placeholder_progress_sent = False
        return True

    async def _progress_permitted(
        self,
        prepared_delivery: _PreparedStreamingDelivery,
        capture_completions: tuple[asyncio.Future[None], ...],
    ) -> bool:
        """Ask the reply's records about one progress edit: ``False`` defers it, and a refusal fails the stream."""
        permission = await self.progress_write_ahead(_progress_state(prepared_delivery.committed_state))
        if permission is ProgressPermission.SEND:
            return True
        _complete_capture_completions(capture_completions)
        if permission is ProgressPermission.REFUSE:
            msg = "The reply's records refused this progress edit"
            raise RuntimeError(msg)
        return False

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

    def _delivery_snapshot(
        self,
        *,
        is_final: bool,
        allow_empty_progress: bool,
    ) -> _StreamingDeliverySnapshot | None:
        """Freeze all mutable stream state needed for one formatting pass."""
        warmup_suffix_lines = self._warmup_state.render_lines(show_tool_calls=self.show_tool_calls)
        if not self.accumulated_text.strip() and not allow_empty_progress and not warmup_suffix_lines:
            return None

        return _StreamingDeliverySnapshot(
            config=self.config,
            runtime_paths=self.runtime_paths,
            accumulated_text=self.accumulated_text,
            event_id=self.event_id,
            effective_thread_id=self.target.resolved_thread_id,
            reply_to_event_id=self.target.reply_to_event_id,
            latest_thread_event_id=self.latest_thread_event_id,
            room_mode=self.room_mode,
            show_tool_calls=self.show_tool_calls,
            tool_trace=tuple(deepcopy(self.tool_trace)),
            presentation_state=deepcopy(self.presentation_state),
            extra_content=self._delivery_extra_content(is_final=is_final),
            warmup_suffix_lines=tuple(warmup_suffix_lines),
            stream_status=self._resolve_stream_status(is_final=is_final),
            interactive_creator_agent=self.interactive_creator_agent,
            interactive_source_event_id=self.interactive_source_event_id,
            markdown_renderer=self._render_markdown,
            chars_since_last_update=self.chars_since_last_update,
        )

    def _delivery_extra_content(self, *, is_final: bool) -> dict[str, Any]:
        """Return the extra content of one durable write: the caller's, the live watermark, and the final reasoning."""
        extra_content = dict(deepcopy(self.extra_content)) if self.extra_content is not None else {}
        checkpoint = self.live.checkpoint() if self.live is not None else None
        if checkpoint is not None:
            extra_content[LIVE_CHECKPOINT_KEY] = checkpoint
        if is_final and self.reasoning_text.strip():
            extra_content[REASONING_CONTENT_KEY] = {
                "text": self.reasoning_text,
                "duration_ms": round(self._reasoning_seconds * 1000),
            }
        return extra_content

    async def _prepare_delivery_async(
        self,
        *,
        is_final: bool,
        allow_empty_progress: bool,
    ) -> _PreparedStreamingDelivery | None:
        """Prepare one outbound payload without blocking the event loop on formatting."""
        snapshot = self._delivery_snapshot(
            is_final=is_final,
            allow_empty_progress=allow_empty_progress,
        )
        if snapshot is None:
            return None
        if is_final and self.final_text_transform is not None and snapshot.accumulated_text.strip():
            # A transform that fails must not cost the answer. The streamed text
            # is already correct and already visible; shaping it is an
            # improvement, not a precondition, so a failure here degrades to
            # delivering what the stream produced rather than delivering
            # nothing. Cancellation is left alone: that is the turn ending.
            try:
                transformed = await self.final_text_transform(snapshot.accumulated_text)
            except Exception:
                logger.exception("final_response_transform_failed_preserving_streamed_text")
            else:
                if transformed.strip() and transformed != snapshot.accumulated_text:
                    snapshot = replace(
                        snapshot,
                        accumulated_text=transformed,
                        untransformed_text=snapshot.accumulated_text,
                    )
        return await asyncio.to_thread(_prepare_delivery_from_snapshot, snapshot)

    def _mark_delivery_committed(self, committed_state: _CommittedDeliveryState) -> None:
        """Snapshot the last non-terminal text/tool-trace state that actually reached Matrix."""
        self._last_delivered_text = committed_state.accumulated_text
        self._last_delivered_tool_trace = deepcopy(committed_state.tool_trace)
        self._last_delivered_presentation_state = deepcopy(committed_state.presentation_state)
        self._last_placeholder_progress_sent = committed_state.placeholder_progress_sent
        self._last_committed_rendered_body = committed_state.rendered_body
        self._last_committed_visible_body_state = committed_state.visible_body_state
        self._last_committed_interactive_metadata = committed_state.interactive_metadata
        self.placeholder_progress_sent = committed_state.placeholder_progress_sent
        if self.visible_progress_callback is not None:
            # Hidden tool calls leave no markers, so the text keeps its marker-shaped lines.
            public_trace = tuple(committed_state.tool_trace) if self.show_tool_calls else ()
            self.visible_progress_callback(committed_state.rendered_body, public_trace)

    def _committed_outcome(
        self,
        canonical_final_body_candidate: str | None,
        *,
        failed: bool = False,
    ) -> StreamTransportOutcome:
        """Report what definitely reached Matrix, for a terminal update that was skipped or did not land."""
        rendered_body: str | None = None
        visible_body_state: Literal["none", "placeholder_only", "visible_body"] = "none"
        if self._last_committed_visible_body_state != "none":
            rendered_body = self._last_committed_rendered_body
            visible_body_state = self._last_committed_visible_body_state
        elif self.event_id is not None and self.placeholder_progress_sent:
            rendered_body, visible_body_state = _PROGRESS_PLACEHOLDER, "placeholder_only"
        return StreamTransportOutcome(
            last_physical_stream_event_id=self.event_id,
            rendered_body=rendered_body,
            visible_body_state=visible_body_state,
            canonical_final_body_candidate=canonical_final_body_candidate,
            failed=failed,
            interactive_metadata=self._last_committed_interactive_metadata,
        )

    def restore_last_delivered_state(self) -> None:
        """Discard buffered state that never reached Matrix after a delivery failure."""
        self.accumulated_text = self._last_delivered_text
        self.tool_trace = deepcopy(self._last_delivered_tool_trace)
        self.presentation_state = deepcopy(self._last_delivered_presentation_state)
        self.chars_since_last_update = 0
        self.placeholder_progress_sent = self._last_placeholder_progress_sent

    def has_uncommitted_presentation(self) -> bool:
        """Return whether buffered ordered presentation state has not reached Matrix."""
        return (
            _normalize_stream_accumulated_text(self.accumulated_text)
            != _normalize_stream_accumulated_text(self._last_delivered_text)
            or not _tool_traces_match(self.tool_trace, self._last_delivered_tool_trace)
            or self.presentation_state != self._last_delivered_presentation_state
        )

    def require_committed_presentation(self) -> None:
        """Raise unless the current ordered presentation was acknowledged."""
        if self.has_uncommitted_presentation():
            msg = "Stream suspension presentation was not acknowledged"
            raise RuntimeError(msg)

    def committed_presentation(self) -> StreamingPresentation:
        """Return the text and trace carried by the latest successful update."""
        response_text = _normalize_stream_accumulated_text(self._last_delivered_text).rstrip()
        return StreamingPresentation(
            response_text=response_text,
            rendered_response_text=self._last_committed_rendered_body if response_text else None,
            tool_trace=tuple(deepcopy(self._last_delivered_tool_trace)),
            state=deepcopy(self._last_delivered_presentation_state),
        )

    def _resolve_stream_status(self, *, is_final: bool) -> str:
        """Return the content status for the current send or edit: only a stream that finished writes a final one."""
        if is_final:
            return STREAM_STATUS_COMPLETED
        if self.event_id is None:
            return STREAM_STATUS_PENDING
        return STREAM_STATUS_STREAMING

    def _mark_first_visible_reply_if_needed(self, delivered_state: _CommittedDeliveryState) -> None:
        """Mark first visible reply timing from the payload acknowledged by Matrix."""
        if self.pipeline_timing is None:
            return
        if delivered_state.visible_body_state == "placeholder_only":
            self.pipeline_timing.mark_first_visible_reply("placeholder")
            return
        self.pipeline_timing.mark_first_visible_reply("stream_update", substantive=True)

    async def _send_initial_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
        is_final: bool,
        progress: ProgressState,
    ) -> bool:
        """Send the initial streaming event.

        This is the terminal update too when no placeholder was ever sent --
        a suppressed one, or one whose own send failed. The answer is then the
        stream's first visible event, and it has to become durable here or it
        never does.
        """
        send = self.terminal_send if is_final else self.initial_send
        assert send is not None, "only a stream that owns its reply's create and terminal update sends a new event"
        delivered = await send(client, self.room_id, content, display_text, progress=progress)
        if delivered is None:
            return False
        self.event_id = delivered.event_id
        if self.visible_event_id_callback is not None:
            self.visible_event_id_callback(delivered.event_id)
        logger.debug("Initial streaming message sent", event_id=self.event_id)
        return True

    async def _edit_existing_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
        is_final: bool,
        progress: ProgressState,
    ) -> bool:
        """Send one streaming edit event for the existing message."""
        assert self.event_id is not None
        if is_final:
            assert self.terminal_edit is not None, "only a stream that owns its reply's terminal update finalizes it"
            delivered = await self.terminal_edit(
                client,
                self.room_id,
                self.event_id,
                content,
                display_text,
                progress=progress,
            )
        else:
            delivered = await edit_message_result(
                client,
                self.room_id,
                self.event_id,
                content,
                display_text,
            )
        return delivered is not None

    async def _direct_transport_allowed(self) -> bool:
        """Return whether an edit that is not a turn's answer may still be sent."""
        if self.transport_is_current is None:
            return True
        if await self.transport_is_current():
            return True
        self._transport_ended = True
        logger.info(
            "streaming_transport_stopped_for_ended_membership",
            room_id=self.room_id,
            event_id=self.event_id,
        )
        return False

    async def _send_content(
        self,
        client: nio.AsyncClient,
        *,
        content: dict[str, Any],
        display_text: str,
        is_final: bool,
        progress: ProgressState,
        retry_on_failure: bool = False,
    ) -> bool:
        """Send a new event or edit the existing one.

        ``is_final`` selects the terminal update, the turn's answer; every
        other edit is a direct progress edit.
        """
        total_attempts = 2 if retry_on_failure else 1
        for attempt in range(1, total_attempts + 1):
            try:
                if self.event_id is None:
                    logger.debug("Sending initial streaming message", attempt=attempt)
                    if await self._send_initial_content(
                        client,
                        content=content,
                        display_text=display_text,
                        is_final=is_final,
                        progress=progress,
                    ):
                        return True
                    logger.error("Failed to send initial streaming message", attempt=attempt)
                else:
                    logger.debug("Editing streaming message", event_id=self.event_id, attempt=attempt)
                    if await self._edit_existing_content(
                        client,
                        content=content,
                        display_text=display_text,
                        is_final=is_final,
                        progress=progress,
                    ):
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

    replaces_text: ClassVar[bool] = True

    def _update(self, new_chunk: str) -> None:
        """Replace the text below any stopped attempt's text with new chunk, numbering its tools after that attempt's."""
        resumed_count = len(self.resumed_tool_trace)
        if resumed_count:
            new_chunk = remap_visible_tool_marker_indices(
                new_chunk,
                {index: index + resumed_count for index in range(1, len(self.tool_trace) - resumed_count + 1)},
            )
        self.accumulated_text = self.resumed_text + new_chunk
        self.chars_since_last_update += len(new_chunk)
        self.last_delta_at = time.time()


def _merge_tool_trace(existing: list[ToolTraceEntry], incoming: list[ToolTraceEntry]) -> list[ToolTraceEntry]:
    """Merge a trace snapshot without dropping entries when stream styles are mixed: the longer one wins, ties incoming."""
    return (incoming if len(incoming) >= len(existing) else existing).copy()


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


_STREAM_DELIVERY_PENDING_OPTIONAL_REQUEST_LIMIT = 32


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
    required_delivery = (
        wait_for_capture or force_refresh or boundary_refresh or phase_boundary_flush or allow_empty_progress
    )
    queue_size = delivery_queue.qsize()
    if not required_delivery and queue_size >= _STREAM_DELIVERY_PENDING_OPTIONAL_REQUEST_LIMIT:
        emit_timing_event(
            "Dispatch tool delivery timing",
            phase="dropped_superseded",
            queue_size=queue_size,
            progress_hint=progress_hint,
            force_refresh=force_refresh,
            boundary_refresh=boundary_refresh,
            phase_boundary_flush=phase_boundary_flush,
            allow_empty_progress=allow_empty_progress,
            wait_for_capture=False,
        )
        return None
    capture_completion = asyncio.get_running_loop().create_future() if wait_for_capture else None
    request = _DeliveryRequest(
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
    )
    delivery_queue.put_nowait(request)
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
    if replacement_suffixes and streaming.replaces_text:
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

    # The stream is closed here, in this task, however consumption ends: a Stop or a shutdown that lands
    # while a write is in flight reaches the turn driver as this task's, not at garbage collection.
    async with closing_async_stream(response_stream):
        async for chunk in response_stream:
            if isinstance(chunk, ReasoningDelta):
                # Reasoning is shown live and kept by the terminal update; no progress edit carries it.
                streaming.add_reasoning(chunk.text, started_at=chunk.started_at)
                continue
            if isinstance(chunk, ToolProgress):
                # Only live frames show a running call's output; no write carries it.
                streaming.tool_progress[chunk.tool_call_id] = chunk.text
                continue
            streaming.end_reasoning_segment()
            if isinstance(chunk, str):
                text_chunk = chunk
            elif isinstance(chunk, StructuredStreamChunk):
                text_chunk = chunk.content
                if chunk.tool_trace is not None:
                    streaming.apply_tool_trace_snapshot(chunk.tool_trace)
                if chunk.presentation_state is not None:
                    streaming.presentation_state = deepcopy(chunk.presentation_state)
            elif isinstance(chunk, TextDelta):
                if chunk.text:
                    text_chunk = chunk.text
                else:
                    _queue_delivery_request(delivery_queue, progress_hint=True)
                    continue
            elif isinstance(chunk, FailedReply):
                text_chunk = chunk.text
            elif isinstance(chunk, RunFinished):
                if chunk.record.content is not None:
                    streaming.canonical_final_body_candidate = chunk.record.content
                continue
            elif isinstance(chunk, ToolStarted):
                if not streaming.show_tool_calls:
                    await _flush_phase_boundary_if_needed(streaming, delivery_queue)
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

                tool_index = len(streaming.tool_trace) + 1
                text_chunk, trace_entry = tool_tracker.start(chunk.call, tool_index=tool_index, origin=chunk.origin)
                if trace_entry is not None:
                    streaming.tool_trace.append(trace_entry)
                await _apply_visible_text_chunk(
                    streaming,
                    delivery_queue,
                    text_chunk,
                    apply_chunk=streaming._append_incremental_text,
                    boundary_refresh=True,
                    wait_for_capture=(
                        streaming.replaces_text and streaming.matching_inflight_nonterminal_capture() is None
                    ),
                )
                continue
            elif isinstance(chunk, ToolCompleted):
                streaming.tool_progress.pop(chunk.call.tool_call_id, None)
                tool_name = chunk.call.tool_name
                pending_tool, completed_trace = tool_tracker.complete(chunk.call, origin=chunk.origin)
                if not streaming.show_tool_calls:
                    _queue_delivery_request(delivery_queue, progress_hint=True)
                    continue
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
                streaming.accumulated_text = complete_pending_tool_block(
                    streaming.accumulated_text,
                    tool_name,
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
                _queue_delivery_request(delivery_queue, prior_delta_at=prior_delta_at if text_changed else None)
                continue
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
            should_refresh = (
                streaming._warmup_state.needs_warmup_clear_edit
                or bool(streaming.accumulated_text.strip())
                or bool(streaming._warmup_state.active_warmups)
                or event.progress.phase == "failed"
            )
            if not should_refresh:
                continue
            # Shown at once rather than throttled: a backend reports only a few phases, so a notice the throttle
            # dropped would stay hidden until the next phase, seconds later.
            _queue_delivery_request(
                delivery_queue,
                force_refresh=True,
                allow_empty_progress=not streaming.accumulated_text.strip(),
            )


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
                prepared_phase_boundary_flush = await streaming._prepare_delivery_async(
                    is_final=False,
                    allow_empty_progress=False,
                )
            if prepared_phase_boundary_flush is None:
                _complete_capture_completions(tuple(phase_boundary_capture_completions))
            if prepared_phase_boundary_flush is not None:
                await streaming._send_prepared_delivery(
                    client,
                    prepared_delivery=prepared_phase_boundary_flush,
                    is_final=False,
                    capture_completions=tuple(phase_boundary_capture_completions),
                )
            # While frames carry progress, an immediate refresh or tool boundary waits for the next checkpoint, unless
            # a writer waits for its capture.
            live_checkpoints_only = streaming.live_active and not boundary_refresh_capture_completions
            if merged_request.force_refresh and not live_checkpoints_only:
                await streaming._send_or_edit_message(
                    client,
                    allow_empty_progress=merged_request.allow_empty_progress,
                    boundary_refresh=merged_request.boundary_refresh,
                    capture_completions=tuple(boundary_refresh_capture_completions),
                )
            elif merged_request.boundary_refresh and not live_checkpoints_only:
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


async def _await_task_exit(task: asyncio.Task[None]) -> asyncio.CancelledError | None:
    """Wait until one task has exited, returning a cancellation of the waiter observed meanwhile."""
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as error:
            cancelled = error
            task.cancel()
    return cancelled


def _task_error(task: asyncio.Task[None]) -> Exception | None:
    """Return the error one exited task raised, or ``None`` when it returned or was cancelled."""
    if task.cancelled():
        return None
    error = task.exception()
    return error if isinstance(error, Exception) else None


async def _stop_progress_tasks(
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    delivery_task: asyncio.Task[None],
    progress_task: asyncio.Task[None],
    pump: WorkerProgressPump,
    *,
    live_task: asyncio.Task[None] | None,
    event_id: str | None,
    room_id: str,
) -> tuple[Exception | None, Exception | None]:
    """End both progress tasks before the caller's next write, and return the progress drain's and delivery's errors.

    Queued and in-flight progress edits get until the drain deadline to land, so
    they land before the terminal update. A cancellation of the caller, or a
    drain past its deadline, cancels them instead, and the wait continues until
    both tasks have really exited: an edit still in flight could otherwise land
    after the caller's terminal update or note. Live frames stop first, without
    draining: they are best effort, and none may follow the terminal update. Waiting for that exit is safe:
    nothing either task awaits swallows cancellation, and the longest a
    cancelled task takes to exit is a Matrix request already in flight, which
    times out, or a journal statement already running, which the journal lets
    finish before the cancellation propagates. A cancellation of the caller
    seen meanwhile is re-raised once both tasks exited, after logging the
    errors the caller then never sees.
    """
    cancelled = None
    if live_task is not None:
        live_task.cancel()
        cancelled = await _await_task_exit(live_task)
        if (live_error := _task_error(live_task)) is not None:
            logger.warning("Live frames failed", event_id=event_id, room_id=room_id, error=str(live_error))
    pump.shutdown.set()
    progress_task.cancel()
    cancelled = await _await_task_exit(progress_task) or cancelled
    if cancelled is None and not delivery_task.done():
        delivery_queue.put_nowait(None)
        try:
            await asyncio.wait({delivery_task}, timeout=_STREAM_DELIVERY_DRAIN_TIMEOUT_SECONDS)
        except asyncio.CancelledError as error:
            cancelled = error
        if cancelled is None and not delivery_task.done():
            logger.warning(
                "Progress edits did not drain before terminal delivery; cancelling them",
                event_id=event_id,
                room_id=room_id,
            )
    delivery_task.cancel()
    cancelled = await _await_task_exit(delivery_task) or cancelled
    errors = (_task_error(progress_task), _task_error(delivery_task))
    if cancelled is not None:
        _log_progress_task_errors(errors, event_id=event_id, room_id=room_id)
        raise cancelled
    return errors


def _log_progress_task_errors(
    errors: tuple[Exception | None, Exception | None],
    *,
    event_id: str | None,
    room_id: str,
    reported: Exception | None = None,
) -> None:
    """Log each progress task's error except the one the caller ``reported`` itself."""
    for error in errors:
        if error is not None and error is not reported:
            logger.warning(
                "Progress edits stopped before terminal delivery",
                event_id=event_id,
                room_id=room_id,
                error=str(error),
            )


async def _cancel_stream_task(task: asyncio.Task[None]) -> None:
    """Cancel one stream supervision task and let it unwind, discarding its outcome.

    A process shutdown that cancelled the supervising task hands the task to the successor runtime too, which the turn
    driver it runs reads.
    """
    if task.done():
        with suppress(asyncio.CancelledError, Exception):
            await task
        return
    request_task_cancel(task, process_shutdown=current_task_is_process_shutdown())
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(task, timeout=0.5)


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
        await _cancel_stream_task(stream_task)
        raise asyncio.CancelledError

    task_error = task.exception()
    if task_error is not None:
        await _cancel_stream_task(stream_task)
        if not isinstance(task_error, Exception):
            raise task_error
        if task is delivery_task:
            _raise_nonterminal_delivery_error(task_error)
        raise task_error

    monitored_tasks.discard(task)
    return True


def _start_live_frames(
    client: nio.AsyncClient,
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> asyncio.Task[None] | None:
    """Start the reply's live frames, unless the operator switched them off."""
    if not streaming.config.defaults.streaming.live_frames:
        return None
    return asyncio.create_task(_run_live_frames(client, streaming, delivery_queue))


async def _run_live_frames(
    client: nio.AsyncClient,
    streaming: StreamingResponse,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Publish the reply's live frames and schedule its durable checkpoints while they run."""
    if not await homeserver_relays_live_frames(runtime_matrix_homeserver(streaming.runtime_paths)):
        return
    streaming.live = LivePublisher(client=client, room_id=streaming.room_id)
    last_checkpoint_request = 0.0

    def schedule_checkpoint() -> None:
        # Frames make the durable reply a checkpoint every update interval, even while nothing else asks for an edit,
        # as during a long tool call.
        nonlocal last_checkpoint_request
        now = time.time()
        if (
            streaming.live_active
            and now - max(streaming.last_update, last_checkpoint_request) >= streaming.update_interval
            and (
                streaming.has_uncommitted_presentation()
                or streaming._warmup_state.active_warmups
                or streaming._warmup_state.needs_warmup_clear_edit
            )
        ):
            last_checkpoint_request = now
            _queue_delivery_request(delivery_queue)

    def catch_up_after_frames_stopped() -> None:
        # Frames stopped, so the durable reply catches up at once and resumes the ordinary cadence.
        _queue_delivery_request(delivery_queue, force_refresh=True)

    await streaming.live.run(
        current=streaming.live_state,
        on_tick=schedule_checkpoint,
        on_stopped=catch_up_after_frames_stopped,
    )


async def _show_placeholder_when_ready(
    streaming: StreamingResponse,
    placeholder_ready: Callable[[], Awaitable[bool]],
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
) -> None:
    """Show the progress placeholder once a reply that started hidden may become visible."""
    if await placeholder_ready() and streaming.event_id is None and not streaming.accumulated_text.strip():
        _queue_delivery_request(delivery_queue, force_refresh=True, allow_empty_progress=True)


async def _consume_stream_with_progress_supervision(
    response_stream: AsyncIterator[StreamInputChunk],
    streaming: StreamingResponse,
    progress_task: asyncio.Task[None] | None,
    delivery_task: asyncio.Task[None] | None,
    delivery_queue: asyncio.Queue[_DeliveryRequest | None],
    placeholder_ready: Callable[[], Awaitable[bool]] | None,
) -> None:
    """Abort chunk consumption as soon as the worker-progress drain fails."""
    stream_task = asyncio.create_task(_consume_streaming_chunks(response_stream, streaming, delivery_queue))
    placeholder_task = (
        None
        if placeholder_ready is None
        else asyncio.create_task(_show_placeholder_when_ready(streaming, placeholder_ready, delivery_queue))
    )
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
        await _cancel_stream_task(stream_task)
        if placeholder_task is not None:
            # Stop before delivery shuts down so a late approval never queues a placeholder.
            await _cancel_stream_task(placeholder_task)


async def send_streaming_response(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    target: MessageTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    response_stream: AsyncIterator[StreamInputChunk],
    *,
    initial_send: TerminalSend,
    terminal_send: TerminalSend,
    terminal_edit: TerminalEdit,
    progress_write_ahead: ProgressWriteAhead,
    progress_delivered: ProgressDelivered,
    streaming_cls: type[StreamingResponse] = StreamingResponse,
    existing_event_id: str | None = None,
    adopt_existing_placeholder: bool = False,
    show_tool_calls: bool = True,
    extra_content: Mapping[str, Any] | None = None,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    visible_event_id_callback: Callable[[str], None] | None = None,
    visible_progress_callback: Callable[[str, Sequence[ToolTraceEntry]], None] | None = None,
    latest_thread_event_id: str | None = None,
    final_text_transform: FinalTextTransform | None = None,
    transport_is_current: Callable[[], Awaitable[bool]] | None = None,
    interactive_creator_agent: str | None = None,
    interactive_source_event_id: str | None = None,
    allow_new_terminal_message: Callable[[], bool] | None = None,
    resumed: UnfinishedStreamedReply | None = None,
    placeholder_ready: Callable[[], Awaitable[bool]] | None = None,
) -> StreamTransportOutcome:
    """Stream chunks to a Matrix room and return the canonical transport outcome.

    ``resumed`` is what a stopped attempt at ``existing_event_id`` showed; the
    stream continues below it instead of replacing it. ``placeholder_ready``
    resolves ``True`` once a reply that started without a placeholder may show one.
    """
    sc = config.defaults.streaming
    streaming = streaming_cls(
        final_text_transform=final_text_transform,
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
        visible_event_id_callback=visible_event_id_callback,
        visible_progress_callback=visible_progress_callback,
        terminal_edit=terminal_edit,
        terminal_send=terminal_send,
        transport_is_current=transport_is_current,
        interactive_creator_agent=interactive_creator_agent,
        interactive_source_event_id=interactive_source_event_id,
        allow_new_terminal_message=allow_new_terminal_message,
        resumed_text=resumed.resumed_text if resumed is not None else "",
        resumed_tool_trace=resumed.tool_trace if resumed is not None else (),
        initial_send=initial_send,
        progress_write_ahead=progress_write_ahead,
        progress_delivered=progress_delivered,
    )

    # Ensure the first chunk triggers an initial send immediately
    streaming.last_update = float("-inf")

    if existing_event_id:
        streaming.event_id = existing_event_id
        if visible_event_id_callback is not None:
            visible_event_id_callback(existing_event_id)
        streaming.placeholder_progress_sent = adopt_existing_placeholder
        # An adopted placeholder already shows this stream, so frames carry a model that thinks before it answers.
        streaming._shows_stream = adopt_existing_placeholder

    worker_progress_queue: asyncio.Queue[WorkerProgressEvent] = asyncio.Queue()
    delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
    task_errors: tuple[Exception | None, Exception | None] = (None, None)
    with worker_progress_pump_scope(asyncio.get_running_loop(), worker_progress_queue) as pump:
        delivery_task = asyncio.create_task(_drive_stream_delivery(client, streaming, delivery_queue))
        progress_task = asyncio.create_task(
            _drain_worker_progress_events(streaming, worker_progress_queue, pump, delivery_queue),
        )
        live_task = _start_live_frames(client, streaming, delivery_queue)
        try:
            try:
                await _consume_stream_with_progress_supervision(
                    response_stream,
                    streaming,
                    progress_task,
                    delivery_task,
                    delivery_queue,
                    placeholder_ready,
                )
            finally:
                # However consumption ended, both tasks stop here, once, before the stream finalizes or raises.
                task_errors = await _stop_progress_tasks(
                    delivery_queue,
                    delivery_task,
                    progress_task,
                    pump,
                    live_task=live_task,
                    event_id=streaming.event_id,
                    room_id=target.room_id,
                )
            progress_error, delivery_error = task_errors
            if progress_error is not None:
                _raise_progress_delivery_error(progress_error)
            if delivery_error is not None:
                _raise_nonterminal_delivery_error(delivery_error)
        except asyncio.CancelledError as exc:
            _log_progress_task_errors(task_errors, event_id=streaming.event_id, room_id=target.room_id)
            log_cancelled_response(
                logger,
                exc=exc,
                message_id=streaming.event_id,
                restart_message="Streaming response interrupted by sync restart",
                user_stop_message="Streaming response cancelled by user",
                interrupted_message="Streaming response interrupted — traceback for diagnosis",
            )
            # The stream writes no ending of its own: the reply's span exit notes the cancellation below what the
            # reply's records say it shows.
            raise
        except StreamingLifecycleSuspensionError as exc:
            _log_progress_task_errors(task_errors, event_id=streaming.event_id, room_id=target.room_id)
            if (delivery_error := task_errors[1]) is not None:
                raise PausePresentationError(delivery_error) from delivery_error
            if streaming.has_uncommitted_presentation():
                try:
                    await streaming._send_or_edit_message(
                        client,
                        force_nonterminal_delivery=True,
                    )
                    streaming.require_committed_presentation()
                except Exception as flush_error:
                    streaming.restore_last_delivered_state()
                    suspension_error = RuntimeError(f"Stream suspension presentation delivery failed: {flush_error}")
                    raise PausePresentationError(suspension_error) from flush_error
            exc.capture_presentation(streaming.committed_presentation())
            raise
        except Exception as exc:
            error = exc.error if isinstance(exc, _NonTerminalDeliveryError) else exc
            logger.exception("Streaming response failed", error=str(error))
            _log_progress_task_errors(task_errors, event_id=streaming.event_id, room_id=target.room_id, reported=error)
            # The stream writes no ending of its own: the reply's span exit notes the failure.
            raise StreamingDeliveryError(error) from error
        else:
            transport_outcome = await streaming.finalize(client)
        finally:
            if tool_trace_collector is not None:
                tool_trace_collector[:] = streaming.tool_trace

    return transport_outcome


@asynccontextmanager
async def stream_progress_edits(
    client: nio.AsyncClient,
    target: MessageTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    event_id: str,
    show_tool_calls: bool,
    progress_write_ahead: ProgressWriteAhead,
    progress_delivered: ProgressDelivered,
    extra_content: dict[str, Any] | None = None,
    visible_progress_callback: Callable[[str, Sequence[ToolTraceEntry]], None] | None = None,
    transport_is_current: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[ProgressPublisher]:
    """Stream progress into one existing reply whose terminal update belongs to the caller.

    Each publication is the caller's complete presentation. Appended text is
    throttled like ordinary streamed deltas, a visible tool-trace change is a
    tool boundary delivered like a streamed tool event, and any other change
    counts as one in-place mutation. The first publication is delivered at
    once, so the reply leaves whatever state it showed before. Worker warm-up
    progress is shown as in ordinary streaming. Progress is transport only: a
    rejected edit stops later progress without failing the caller, whose
    terminal delivery still owns the reply. Leaving the scope waits until no
    progress edit is in flight, even across cancellation, so the caller's
    terminal update can never race one.
    """
    stream_config = config.defaults.streaming
    streaming = StreamingResponse(
        target=target,
        config=config,
        runtime_paths=runtime_paths,
        event_id=event_id,
        last_update=float("-inf"),
        update_interval=stream_config.update_interval,
        min_update_interval=stream_config.min_update_interval,
        interval_ramp_seconds=stream_config.interval_ramp_seconds,
        max_idle=stream_config.max_idle,
        show_tool_calls=show_tool_calls,
        extra_content=extra_content,
        visible_progress_callback=visible_progress_callback,
        transport_is_current=transport_is_current,
        progress_write_ahead=progress_write_ahead,
        progress_delivered=progress_delivered,
    )
    published = False

    def apply_presentation(text: str) -> None:
        if text.startswith(streaming.accumulated_text):
            streaming._append_incremental_text(text[len(streaming.accumulated_text) :])
            return
        streaming.accumulated_text = text
        streaming._mark_nonadditive_text_mutation()

    worker_progress_queue: asyncio.Queue[WorkerProgressEvent] = asyncio.Queue()
    delivery_queue: asyncio.Queue[_DeliveryRequest | None] = asyncio.Queue()
    with worker_progress_pump_scope(asyncio.get_running_loop(), worker_progress_queue) as pump:
        delivery_task = asyncio.create_task(_drive_stream_delivery(client, streaming, delivery_queue))
        progress_task = asyncio.create_task(
            _drain_worker_progress_events(streaming, worker_progress_queue, pump, delivery_queue),
        )
        live_task = _start_live_frames(client, streaming, delivery_queue)

        async def publish(chunk: StructuredStreamChunk) -> None:
            nonlocal published
            if delivery_task.done():
                return
            if chunk.tool_progress is not None:
                # Only live frames show it; no write carries it.
                streaming.tool_progress = dict(chunk.tool_progress)
            trace_changed = False
            if chunk.tool_trace is not None and not _tool_traces_match(streaming.tool_trace, chunk.tool_trace):
                # Callers keep mutating their trace entries; hold the published state.
                streaming.tool_trace = deepcopy(chunk.tool_trace)
                trace_changed = True
            if not published:
                published = True
                streaming.accumulated_text = chunk.content
                _queue_delivery_request(delivery_queue, force_refresh=True, allow_empty_progress=True)
            elif chunk.content != streaming.accumulated_text or (trace_changed and streaming.show_tool_calls):
                await _apply_visible_text_chunk(
                    streaming,
                    delivery_queue,
                    chunk.content,
                    apply_chunk=apply_presentation,
                    boundary_refresh=trace_changed and streaming.show_tool_calls,
                )

        try:
            yield publish
        finally:
            task_errors = await _stop_progress_tasks(
                delivery_queue,
                delivery_task,
                progress_task,
                pump,
                live_task=live_task,
                event_id=event_id,
                room_id=target.room_id,
            )
            _log_progress_task_errors(task_errors, event_id=event_id, room_id=target.room_id)
