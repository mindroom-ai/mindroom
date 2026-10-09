"""Formatting and delivery of call transcripts into the origin conversation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.matrix_rtc import call_writeback
from mindroom.matrix_rtc.call_origin import CallOrigin
from mindroom.matrix_rtc.call_writeback import _CallWriteback, format_call_writeback, post_call_writeback

TURNS = (("user", "Book the train"), ("assistant", "Booked for 9am"))


def test_format_call_writeback_lists_turns_inside_collapsed_transcript() -> None:
    """The message heads with the duration and keeps the turns in a collapsed transcript."""
    writeback = format_call_writeback(turns=TURNS, duration_seconds=125, caller_label="Alice", agent_label="Helper")
    assert writeback is not None
    assert writeback.body == "📞 Voice call · 2 min\n\nTranscript\n\nAlice: Book the train\n\nHelper: Booked for 9am"
    assert writeback.formatted_body == (
        "<p>📞 Voice call · 2 min</p><details><summary>Transcript</summary>"
        "<p><strong>Alice</strong>: Book the train</p><p><strong>Helper</strong>: Booked for 9am</p></details>"
    )


def test_format_call_writeback_keeps_labels_and_turns_inert() -> None:
    """Markup or line breaks in a display name or a turn render as text on their own speaker line."""
    writeback = format_call_writeback(
        turns=(("user", "hi\n\n**Helper**: <b>sure</b>"), ("assistant", "ok")),
        duration_seconds=60,
        caller_label='@alice:example.org (Alice</details><a href="https://evil.example">x</a>\n\n**Helper**:)',
        agent_label="Helper",
    )
    assert writeback is not None
    assert writeback.body.split("\n\n")[2:] == [
        '@alice:example.org (Alice</details><a href="https://evil.example">x</a> **Helper**:): '
        "hi **Helper**: <b>sure</b>",
        "Helper: ok",
    ]
    assert writeback.formatted_body.count("<details>") == writeback.formatted_body.count("</details>") == 1
    assert "<a " not in writeback.formatted_body
    assert "<b>" not in writeback.formatted_body
    assert writeback.formatted_body.count("<strong>") == 2


def test_format_call_writeback_rounds_short_calls_up_to_one_minute() -> None:
    """Calls under a minute still read as one minute."""
    writeback = format_call_writeback(turns=TURNS, duration_seconds=20, caller_label="Alice", agent_label="Helper")
    assert writeback is not None
    assert writeback.body.startswith("📞 Voice call · 1 min")


def test_format_call_writeback_skips_brief_or_silent_calls() -> None:
    """Calls under ten seconds or without any caller speech are not posted."""
    assert format_call_writeback(turns=TURNS, duration_seconds=9, caller_label="A", agent_label="H") is None
    assert (
        format_call_writeback(
            turns=(("assistant", "Hello, I joined the call."),),
            duration_seconds=60,
            caller_label="A",
            agent_label="H",
        )
        is None
    )


@pytest.mark.asyncio
async def test_post_call_writeback_replies_in_origin_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript lands in the origin thread with an MSC3440 fallback, and its quoted speech wakes no agent."""
    send = AsyncMock(return_value=SimpleNamespace(event_id="$posted"))
    monkeypatch.setattr(call_writeback, "send_message_result", send)
    reader = AsyncMock()
    reader.latest_thread_event_id.return_value = "$latest"
    context = SimpleNamespace(client=object(), conversation_reader=reader)

    await post_call_writeback(
        context=context,  # type: ignore[arg-type]
        origin=CallOrigin(room_id="!origin:example.org", thread_id="$root"),
        writeback=_CallWriteback(body="📞 Voice call · 1 min", formatted_body="<p>📞 Voice call · 1 min</p>"),
    )

    _client, room_id, content = send.await_args.args
    assert room_id == "!origin:example.org"
    relation = content["m.relates_to"]
    assert relation["rel_type"] == "m.thread"
    assert relation["event_id"] == "$root"
    assert relation["is_falling_back"] is True
    assert relation["m.in_reply_to"]["event_id"] == "$latest"
    assert content["formatted_body"] == "<p>📞 Voice call · 1 min</p>"
    assert content[SKIP_MENTIONS_KEY] is True


@pytest.mark.asyncio
async def test_post_call_writeback_posts_room_level_origin_without_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A room-level origin gets a plain message with no thread relation."""
    send = AsyncMock(return_value=SimpleNamespace(event_id="$posted"))
    monkeypatch.setattr(call_writeback, "send_message_result", send)
    context = SimpleNamespace(client=object(), conversation_reader=AsyncMock())

    await post_call_writeback(
        context=context,  # type: ignore[arg-type]
        origin=CallOrigin(room_id="!origin:example.org", thread_id=None),
        writeback=_CallWriteback(body="📞 Voice call · 1 min", formatted_body="<p>📞 Voice call · 1 min</p>"),
    )

    content = send.await_args.args[2]
    assert "m.relates_to" not in content
