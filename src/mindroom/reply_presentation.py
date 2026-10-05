"""What one agent or team reply shows, and how each write of it renders.

A reply is one Matrix event that several execution spans write over time: the
turn that starts it, a restart's replay, an approval resume, an edit
regeneration. Its presentation is therefore a sequence of segments rather than
one string, so a later span can continue below an earlier one, a note can be
placed between them, and the tool markers of every segment can be numbered
across the whole reply.

Rendering is pure. It decides the body text, the visible tool trace, and the
``io.mindroom.stream_status`` value for one write; the delivery layer turns
that into Matrix content with the same formatting helpers every other message
uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

from mindroom.constants import (
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.streaming import (
    CANCELLED_RESPONSE_NOTE,
    INTERRUPTED_RESPONSE_NOTE,
    PROGRESS_PLACEHOLDER,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    TEAM_PROGRESS_PLACEHOLDER,
    clean_partial_reply_text,
    format_stream_error_note,
)
from mindroom.tool_system.events import (
    ToolTraceEntry,
    deserialize_tool_trace,
    remap_visible_tool_marker_indices,
    serialize_tool_trace,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PRESENTATION_VERSION = 1
AGENT_PLACEHOLDER = PROGRESS_PLACEHOLDER
TEAM_PLACEHOLDER = TEAM_PROGRESS_PLACEHOLDER
DELIVERY_FAILED_NOTE = "Response delivery failed. Please retry."
APPROVAL_START_FAILED_NOTE = "Tool approval could not be started. Please try again."


class NoteKind(StrEnum):
    """Why a note sits in a reply; its text is fixed by the kind or carried by the segment."""

    RESTART = "restart"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    DELIVERY_FAILED = "delivery_failed"
    APPROVAL_WAIT = "approval_wait"
    APPROVAL_FAILED = "approval_failed"


class WriteKind(StrEnum):
    """Which write of a reply is being rendered; it decides the wire status (DESIGN §5.3)."""

    # A placeholder sent before the model runs: ``pending``, as a plain message.
    PLACEHOLDER = "placeholder"
    # A stream's first send when no placeholder exists: ``pending``, as a notice.
    CREATE = "create"
    STREAM_TERMINAL_CREATE = "stream_terminal_create"
    HOOK_FAILURE_NOTICE = "hook_failure_notice"
    INTERACTIVE_ACKNOWLEDGEMENT = "interactive_acknowledgement"
    PROGRESS = "progress"
    PAUSE = "pause"
    TERMINAL = "terminal"
    BLOCKING_SEND = "blocking_send"


def format_error_note(error: object) -> str:
    """Return the note a stream ended by an exception shows."""
    return format_stream_error_note(str(error))


_FIXED_NOTE_TEXTS = {
    NoteKind.RESTART: RESTART_INTERRUPTED_RESPONSE_NOTE,
    NoteKind.CANCELLED: CANCELLED_RESPONSE_NOTE,
    NoteKind.INTERRUPTED: INTERRUPTED_RESPONSE_NOTE,
    NoteKind.DELIVERY_FAILED: DELIVERY_FAILED_NOTE,
    NoteKind.APPROVAL_FAILED: APPROVAL_START_FAILED_NOTE,
}


@dataclass(frozen=True, slots=True)
class Segment:
    """One contiguous part of a reply: an answer some span produced, or a note."""

    kind: Literal["answer", "note"]
    text: str
    span_id: str | None = None
    # Visible trace as collected; marker indices in ``text`` count from one within this segment.
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    # A team document's restorable state; ``text`` is its rendered body.
    team_state: Mapping[str, object] | None = None
    note: NoteKind | None = None

    def __post_init__(self) -> None:
        """Reject segments that mix answer and note fields."""
        if (self.kind == "note") != (self.note is not None):
            msg = "A note segment carries a note kind and an answer segment carries none"
            raise ValueError(msg)


def note_segment(kind: NoteKind, text: str | None = None) -> Segment:
    """Return one note segment, with the fixed text of its kind unless one is given."""
    resolved = text if text is not None else _FIXED_NOTE_TEXTS.get(kind)
    if resolved is None:
        msg = f"Note kind {kind.value!r} needs explicit text"
        raise ValueError(msg)
    return Segment(kind="note", text=resolved, note=kind)


@dataclass(frozen=True, slots=True)
class Presentation:
    """Everything a reply shows, as structured segments."""

    segments: tuple[Segment, ...] = ()
    trailing_note: Segment | None = None
    placeholder: str = AGENT_PLACEHOLDER
    show_tool_calls: bool = True
    extra_content: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep the trailing note a note."""
        if self.trailing_note is not None and self.trailing_note.kind != "note":
            msg = "A reply's trailing note must be a note segment"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RenderedReply:
    """The body, trace, and status one write of a reply carries."""

    body: str
    tool_trace: tuple[ToolTraceEntry, ...]
    stream_status: str | None
    # Sent as m.notice, which Matrix push rules suppress; only stream sends and edits are.
    in_progress: bool
    placeholder_only: bool


def _segment_has_work(segment: Segment, placeholder: str) -> bool:
    if segment.kind != "answer":
        return False
    if segment.tool_trace:
        return True
    text = segment.text.strip()
    return bool(text) and text != placeholder and any(char.isalnum() for char in text)


def visible_work(presentation: Presentation) -> bool:
    """Return whether the reply shows anything a span produced: answer text or a tool call."""
    return any(_segment_has_work(segment, presentation.placeholder) for segment in presentation.segments)


def _combined(segments: Sequence[Segment], placeholder: str) -> tuple[str, tuple[ToolTraceEntry, ...]]:
    """Join segments into one body, numbering each segment's tool markers after the earlier ones."""
    parts: list[str] = []
    trace: list[ToolTraceEntry] = []
    for segment in segments:
        text = segment.text
        if segment.tool_trace:
            offset = len(trace)
            if offset:
                text = remap_visible_tool_marker_indices(
                    text,
                    {index: index + offset for index in range(1, len(segment.tool_trace) + 1)},
                )
            trace.extend(segment.tool_trace)
        stripped = text.rstrip()
        if segment.kind == "answer" and stripped == placeholder:
            continue
        if stripped:
            parts.append(stripped)
    return "\n\n".join(parts), tuple(trace)


def render_body(presentation: Presentation) -> tuple[str, tuple[ToolTraceEntry, ...]]:
    """Return the full body and visible trace the reply shows, or its placeholder when empty."""
    body, trace = _combined(presentation.segments, presentation.placeholder)
    note = presentation.trailing_note
    if note is not None and (note.note is not NoteKind.APPROVAL_WAIT or not body):
        # A paused reply keeps showing its answer; the wait text appears only
        # when there is nothing else to show, as on main.
        body = f"{body}\n\n{note.text}" if body else note.text
    return (body or presentation.placeholder), trace


def _terminal_status(state: str) -> str:
    if state == "completed":
        return STREAM_STATUS_COMPLETED
    if state == "cancelled":
        return STREAM_STATUS_CANCELLED
    if state == "failed":
        return STREAM_STATUS_ERROR
    msg = f"No terminal wire status for reply state {state!r}"
    raise ValueError(msg)


def stream_status_for(write: WriteKind, *, state: str, needs_human_decision: bool = False) -> str | None:
    """Return the wire status one write carries, following main's rules."""
    match write:
        case WriteKind.PLACEHOLDER | WriteKind.CREATE:
            return STREAM_STATUS_PENDING
        case WriteKind.PROGRESS:
            return STREAM_STATUS_STREAMING
        case WriteKind.PAUSE:
            return STREAM_STATUS_APPROVAL_PENDING if needs_human_decision else STREAM_STATUS_PENDING
        case WriteKind.STREAM_TERMINAL_CREATE | WriteKind.TERMINAL:
            return _terminal_status(state)
        case WriteKind.HOOK_FAILURE_NOTICE:
            return STREAM_STATUS_ERROR
        case WriteKind.INTERACTIVE_ACKNOWLEDGEMENT | WriteKind.BLOCKING_SEND:
            return None


def render(
    presentation: Presentation,
    write: WriteKind,
    *,
    state: str,
    frozen_display: Presentation | None = None,
    needs_human_decision: bool = False,
) -> RenderedReply:
    """Render one write of a reply; a frozen display, when present, is what the reply shows."""
    shown = presentation
    if frozen_display is not None:
        shown = replace(frozen_display, trailing_note=presentation.trailing_note)
    body, trace = render_body(shown)
    status = stream_status_for(write, state=state, needs_human_decision=needs_human_decision)
    return RenderedReply(
        body=body,
        tool_trace=trace if shown.show_tool_calls else (),
        stream_status=status,
        # Only the stream's own sends and edits are notices; a placeholder or a
        # pause is a plain message, so push rules still apply to it.
        in_progress=write in {WriteKind.CREATE, WriteKind.PROGRESS},
        placeholder_only=body == shown.placeholder,
    )


def with_trailing_note(presentation: Presentation, note: Segment | None) -> Presentation:
    """Return the presentation with its trailing note replaced."""
    return replace(presentation, trailing_note=note)


def folded(presentation: Presentation) -> Presentation:
    """Return the presentation as one answer segment that later spans continue below.

    A frozen display and the notes inside it become plain history: a later
    span appends after them and never rewrites them.
    """
    body, trace = _combined(presentation.segments, presentation.placeholder)
    note = presentation.trailing_note
    if note is not None and note.note is not NoteKind.APPROVAL_WAIT:
        body = f"{body}\n\n{note.text}" if body else note.text
    if not body and not trace:
        return replace(presentation, segments=(), trailing_note=None)
    return replace(presentation, segments=(Segment(kind="answer", text=body, tool_trace=trace),), trailing_note=None)


def shown_work(possibly_shown: Presentation) -> Segment | None:
    """Return what a stopped reply showed of its work, without its notes, as main reads it back.

    Trailing cancel, interruption, restart, and error notes are dropped, so a
    reply interrupted twice before its continuation showed anything carries
    one restart note, not two. ``None`` means only a placeholder or notes.
    """
    body, trace = _combined(folded(possibly_shown).segments, possibly_shown.placeholder)
    text = clean_partial_reply_text(body)
    if not text and not trace:
        return None
    return Segment(kind="answer", text=text, tool_trace=trace)


def after_restart(possibly_shown: Presentation) -> Presentation:
    """Return what a replay continues below: the shown work and the restart note, or nothing.

    A reply that showed only its placeholder is replaced rather than
    annotated, as main's continuation does.
    """
    work = shown_work(possibly_shown)
    if work is None:
        return replace(possibly_shown, segments=(), trailing_note=None)
    return replace(possibly_shown, segments=(work, note_segment(NoteKind.RESTART)), trailing_note=None)


def with_answer(presentation: Presentation, answer: Segment) -> Presentation:
    """Return the presentation with this span's answer segment set, appended after older ones."""
    if answer.kind != "answer":
        msg = "Only an answer segment can be a span's answer"
        raise ValueError(msg)
    segments = list(presentation.segments)
    if segments and segments[-1].kind == "answer" and segments[-1].span_id == answer.span_id:
        segments[-1] = answer
    else:
        segments.append(answer)
    return replace(presentation, segments=tuple(segments))


def current_answer(presentation: Presentation, span_id: str | None) -> Segment | None:
    """Return the answer segment a span is still writing, if the reply ends with one."""
    if presentation.segments and presentation.segments[-1].kind == "answer":
        last = presentation.segments[-1]
        if span_id is None or last.span_id == span_id:
            return last
    return None


def continued_by(presentation: Presentation, span_id: str) -> Presentation:
    """Hand the reply's last answer segment to a span that continues it, as an approval resume does."""
    last = current_answer(presentation, None)
    if last is None:
        return replace(presentation, trailing_note=None)
    return replace(
        presentation,
        segments=(*presentation.segments[:-1], replace(last, span_id=span_id)),
        trailing_note=None,
    )


# ---------------------------------------------------------------------------
# Codec


def _encode_segment(segment: Segment) -> dict[str, object]:
    encoded: dict[str, object] = {"kind": segment.kind, "text": segment.text}
    if segment.span_id is not None:
        encoded["span_id"] = segment.span_id
    if segment.tool_trace:
        encoded["tool_trace"] = list(serialize_tool_trace(segment.tool_trace, include_internal=True))
    if segment.team_state is not None:
        encoded["team_state"] = dict(segment.team_state)
    if segment.note is not None:
        encoded["note"] = segment.note.value
    return encoded


def _decode_segment(raw: object) -> Segment:
    if not isinstance(raw, dict):
        msg = "Stored reply segment is not an object"
        raise TypeError(msg)
    item = cast("dict[str, object]", raw)
    kind = item.get("kind")
    text = item.get("text")
    span_id = item.get("span_id")
    trace = item.get("tool_trace", [])
    team_state = item.get("team_state")
    note = item.get("note")
    if (
        kind not in {"answer", "note"}
        or not isinstance(text, str)
        or (span_id is not None and not isinstance(span_id, str))
        or not isinstance(trace, list)
        or (team_state is not None and not isinstance(team_state, dict))
        or (note is not None and not isinstance(note, str))
    ):
        msg = "Stored reply segment is malformed"
        raise TypeError(msg)
    return Segment(
        kind=cast("Literal['answer', 'note']", kind),
        text=text,
        span_id=span_id,
        tool_trace=tuple(deserialize_tool_trace(cast("list[Mapping[str, object]]", trace))),
        team_state=cast("dict[str, object] | None", team_state),
        note=NoteKind(note) if note is not None else None,
    )


def encode_presentation(presentation: Presentation) -> str:
    """Serialize one presentation for the reply store."""
    encoded: dict[str, object] = {
        "version": PRESENTATION_VERSION,
        "segments": [_encode_segment(segment) for segment in presentation.segments],
        "placeholder": presentation.placeholder,
        "show_tool_calls": presentation.show_tool_calls,
    }
    if presentation.trailing_note is not None:
        encoded["trailing_note"] = _encode_segment(presentation.trailing_note)
    if presentation.extra_content:
        encoded["extra_content"] = dict(presentation.extra_content)
    return json.dumps(encoded, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def decode_presentation(stored: str) -> Presentation:
    """Restore one stored presentation, rejecting unknown versions."""
    raw = json.loads(stored)
    if not isinstance(raw, dict):
        msg = "Stored reply presentation is not an object"
        raise TypeError(msg)
    data = cast("dict[str, object]", raw)
    if data.get("version") != PRESENTATION_VERSION:
        msg = f"Unsupported reply presentation version {data.get('version')!r}"
        raise ValueError(msg)
    segments = data.get("segments")
    placeholder = data.get("placeholder")
    show_tool_calls = data.get("show_tool_calls")
    extra_content = data.get("extra_content", {})
    trailing = data.get("trailing_note")
    if (
        not isinstance(segments, list)
        or not isinstance(placeholder, str)
        or not isinstance(show_tool_calls, bool)
        or not isinstance(extra_content, dict)
    ):
        msg = "Stored reply presentation is malformed"
        raise TypeError(msg)
    return Presentation(
        segments=tuple(_decode_segment(segment) for segment in segments),
        trailing_note=_decode_segment(trailing) if trailing is not None else None,
        placeholder=placeholder,
        show_tool_calls=show_tool_calls,
        extra_content=cast("dict[str, object]", extra_content),
    )
