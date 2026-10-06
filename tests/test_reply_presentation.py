"""Reply presentations render the same bodies and statuses main's streamer writes."""

from __future__ import annotations

import pytest

from mindroom.constants import (
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    NoteKind,
    Presentation,
    Segment,
    WriteKind,
    after_restart,
    continued_by,
    decode_presentation,
    encode_presentation,
    folded,
    format_error_note,
    note_segment,
    render,
    render_body,
    stream_status_for,
    with_answer,
    with_trailing_note,
)
from mindroom.streaming import (
    build_cancelled_response_update,
    build_restart_interrupted_body,
    format_stream_error_note,
    unfinished_streamed_reply,
)
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content


def _trace(name: str, *, call_id: str | None = None) -> ToolTraceEntry:
    return ToolTraceEntry(
        type="tool_call_completed",
        tool_name=name,
        args_preview="{}",
        result_preview="ok",
        tool_call_id=call_id,
        scope_key="scope" if call_id else None,
    )


def _answer(text: str, *traces: ToolTraceEntry, span_id: str = "span-1") -> Segment:
    return Segment(kind="answer", text=text, span_id=span_id, tool_trace=traces)


def test_codec_round_trips_every_field() -> None:
    """A stored presentation restores segments, notes, internal trace identity, and team state."""
    presentation = Presentation(
        segments=(
            _answer("first\n\n🔧 `search` [1]", _trace("search", call_id="call-1")),
            note_segment(NoteKind.RESTART),
            Segment(
                kind="answer",
                text="team body",
                span_id="span-2",
                team_state={"kind": "team_stream", "version": 2, "members": ["a"]},
            ),
        ),
        trailing_note=note_segment(NoteKind.ERROR, format_error_note("boom")),
        placeholder=TEAM_PLACEHOLDER,
        show_tool_calls=False,
        extra_content={"io.mindroom.ai_run": {"model": "m"}},
    )

    restored = decode_presentation(encode_presentation(presentation))

    assert restored == presentation
    assert restored.segments[0].tool_trace[0].tool_call_id == "call-1"
    assert restored.segments[0].tool_trace[0].scope_key == "scope"


def test_codec_rejects_unknown_versions_and_malformed_segments() -> None:
    """A presentation written by another version is never guessed at."""
    with pytest.raises(ValueError, match="version"):
        decode_presentation('{"version": 99}')
    with pytest.raises(TypeError, match="malformed"):
        decode_presentation(
            '{"version": 1, "segments": [{"kind": "answer"}], "placeholder": "x", "show_tool_calls": true}',
        )


@pytest.mark.parametrize(
    "visible",
    ["partial answer", "partial answer\n\n🔧 `search` [1]", ""],
)
def test_cancel_and_interruption_bodies_match_main(visible: str) -> None:
    """A trailing cancel or interruption note renders exactly main's terminal text."""
    for kind, cancel_source in ((NoteKind.CANCELLED, "user_stop"), (NoteKind.INTERRUPTED, "interrupted")):
        presentation = Presentation(segments=(_answer(visible),) if visible else ())
        body, _trace_entries = render_body(with_trailing_note(presentation, note_segment(kind)))
        expected, _status = build_cancelled_response_update(visible, cancel_source=cancel_source)
        assert body == expected


def test_error_note_matches_main() -> None:
    """An error note uses main's wording and truncation."""
    long_error = "x" * 400
    assert format_error_note(long_error) == format_stream_error_note(long_error)
    assert format_error_note("") == format_stream_error_note("")
    presentation = with_trailing_note(
        Presentation(segments=(_answer("text"),)),
        note_segment(NoteKind.ERROR, format_error_note("boom")),
    )
    assert render_body(presentation)[0] == f"text\n\n{format_stream_error_note('boom')}"


def test_restart_continuation_matches_main_and_numbers_tools_across_segments() -> None:
    """A replay continues below the shown work and the restart note, numbering its tools after the stopped ones."""
    shown = Presentation(segments=(_answer("before\n\n🔧 `search` [1]", _trace("search")),))
    resumed = after_restart(shown)
    resumed = with_answer(resumed, _answer("🔧 `fetch` [1]\n\nafter", _trace("fetch"), span_id="span-2"))

    body, trace = render_body(resumed)

    main_resumed = unfinished_streamed_reply(
        "before\n\n🔧 `search` [1]",
        {"io.mindroom.stream_status": "streaming", **(build_tool_trace_content([_trace("search")]) or {})},
    )
    assert main_resumed is not None
    assert body == main_resumed.resumed_text + "🔧 `fetch` [2]\n\nafter"
    assert [entry.tool_name for entry in trace] == ["search", "fetch"]


def test_restart_of_a_placeholder_replaces_it() -> None:
    """A reply that showed only its placeholder is replaced, not annotated."""
    assert after_restart(Presentation()) == Presentation()
    assert after_restart(Presentation(segments=(_answer(AGENT_PLACEHOLDER),))).segments == ()
    assert build_restart_interrupted_body(AGENT_PLACEHOLDER).startswith("**[")


def test_approval_wait_note_shows_only_without_an_answer() -> None:
    """A paused reply keeps its answer visible; the wait text replaces only the placeholder."""
    wait = note_segment(NoteKind.APPROVAL_WAIT, "Waiting for approval: `shell`")
    assert render_body(with_trailing_note(Presentation(), wait))[0] == "Waiting for approval: `shell`"
    assert render_body(with_trailing_note(Presentation(segments=(_answer("partial"),)), wait))[0] == "partial"


def test_continued_by_hands_the_paused_answer_to_the_resume() -> None:
    """An approval resume continues the paused answer segment and drops the wait note."""
    paused = with_trailing_note(
        Presentation(segments=(_answer("partial", _trace("shell")),)),
        note_segment(NoteKind.APPROVAL_WAIT, "Waiting"),
    )
    resumed = continued_by(paused, "span-2")
    assert resumed.trailing_note is None
    assert resumed.segments[-1].span_id == "span-2"
    assert resumed.segments[-1].text == "partial"


def test_folded_turns_a_frozen_display_into_history() -> None:
    """Notes inside a frozen display stay, and later spans append below them."""
    frozen = with_trailing_note(Presentation(segments=(_answer("answer"),)), note_segment(NoteKind.CANCELLED))
    history = folded(frozen)
    assert history.trailing_note is None
    assert render_body(history)[0] == build_cancelled_response_update("answer", cancel_source="user_stop")[0]


@pytest.mark.parametrize(
    ("write", "state", "decision", "expected"),
    [
        (WriteKind.PLACEHOLDER, "active", False, STREAM_STATUS_PENDING),
        (WriteKind.CREATE, "active", False, STREAM_STATUS_PENDING),
        (WriteKind.PROGRESS, "active", False, STREAM_STATUS_STREAMING),
        (WriteKind.PAUSE, "paused", True, STREAM_STATUS_APPROVAL_PENDING),
        (WriteKind.PAUSE, "paused", False, STREAM_STATUS_PENDING),
        (WriteKind.TERMINAL, "completed", False, STREAM_STATUS_COMPLETED),
        (WriteKind.TERMINAL, "cancelled", False, STREAM_STATUS_CANCELLED),
        (WriteKind.TERMINAL, "failed", False, STREAM_STATUS_ERROR),
        (WriteKind.STREAM_TERMINAL_CREATE, "completed", False, STREAM_STATUS_COMPLETED),
        (WriteKind.STREAM_TERMINAL_CREATE, "cancelled", False, STREAM_STATUS_CANCELLED),
        (WriteKind.HOOK_FAILURE_NOTICE, "failed", False, STREAM_STATUS_ERROR),
        (WriteKind.INTERACTIVE_ACKNOWLEDGEMENT, "active", False, None),
        (WriteKind.BLOCKING_SEND, "completed", False, None),
    ],
)
def test_wire_status_per_write(write: WriteKind, state: str, decision: bool, expected: str | None) -> None:
    """Every write kind carries main's wire status (DESIGN.md §5.3)."""
    assert stream_status_for(write, state=state, needs_human_decision=decision) == expected


def test_terminal_write_of_a_non_terminal_state_is_refused() -> None:
    """A terminal write needs a terminal reply state."""
    with pytest.raises(ValueError, match="terminal"):
        stream_status_for(WriteKind.TERMINAL, state="active")


def test_render_uses_a_frozen_display_and_hides_trace_when_tool_calls_are_hidden() -> None:
    """The frozen post-hook display wins over the canonical answer, and hidden traces stay off the wire."""
    presentation = Presentation(segments=(_answer("raw", _trace("search")),), show_tool_calls=False)
    frozen = Presentation(segments=(_answer("transformed"),), show_tool_calls=False)
    rendered = render(presentation, WriteKind.TERMINAL, state="completed", frozen_display=frozen)
    assert rendered.body == "transformed"
    assert rendered.tool_trace == ()
    assert rendered.stream_status == STREAM_STATUS_COMPLETED
    assert not rendered.in_progress


def test_render_of_an_empty_reply_is_its_placeholder() -> None:
    """A reply with nothing to show renders its kind's placeholder and says so."""
    rendered = render(Presentation(placeholder=TEAM_PLACEHOLDER), WriteKind.PLACEHOLDER, state="active")
    assert rendered.body == TEAM_PLACEHOLDER
    assert rendered.placeholder_only


def test_restart_after_a_noted_interruption_carries_one_note() -> None:
    """Notes a stopped reply already showed are dropped before the restart note, as main reads them back."""
    interrupted = with_trailing_note(Presentation(segments=(_answer("partial"),)), note_segment(NoteKind.INTERRUPTED))
    once = after_restart(interrupted)
    assert render_body(once)[0] == "partial\n\n**[Response interrupted by service restart]**"
    twice = after_restart(once)
    assert render_body(twice)[0] == render_body(once)[0]


def test_only_stream_sends_and_edits_are_notices() -> None:
    """Placeholders and pauses stay plain messages so push rules apply to them."""
    assert not render(Presentation(), WriteKind.PLACEHOLDER, state="active").in_progress
    assert not render(Presentation(), WriteKind.PAUSE, state="paused").in_progress
    assert render(Presentation(), WriteKind.CREATE, state="active").in_progress
    assert render(Presentation(), WriteKind.PROGRESS, state="active").in_progress
