"""Formatting and delivery of call transcripts into the origin conversation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mindroom.matrix.message_builder import markdown_to_html
from mindroom.matrix_rtc import call_writeback
from mindroom.matrix_rtc.call_origin import CallOrigin
from mindroom.matrix_rtc.call_writeback import format_call_writeback, post_call_writeback

TURNS = (("user", "Book the train"), ("assistant", "Booked for 9am"))


def test_format_call_writeback_lists_turns_inside_collapsed_transcript() -> None:
    """The message heads with the duration and keeps the turns in a collapsed transcript."""
    body = format_call_writeback(turns=TURNS, duration_seconds=125, caller_label="Alice", agent_label="Helper")
    assert body is not None
    assert body.startswith("📞 Voice call · 2 min")
    assert "<details>" in body
    assert "<summary>Transcript</summary>" in body
    assert body.index("**Alice**: Book the train") < body.index("**Helper**: Booked for 9am")
    html = markdown_to_html(body)
    assert "<details>" in html
    assert "<summary>Transcript</summary>" in html


def test_format_call_writeback_rounds_short_calls_up_to_one_minute() -> None:
    """Calls under a minute still read as one minute."""
    body = format_call_writeback(turns=TURNS, duration_seconds=20, caller_label="Alice", agent_label="Helper")
    assert body is not None
    assert body.startswith("📞 Voice call · 1 min")


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
    """The transcript lands in the origin thread with an MSC3440 fallback to the latest event."""
    send = AsyncMock(return_value=SimpleNamespace(event_id="$posted"))
    monkeypatch.setattr(call_writeback, "send_message_result", send)
    reader = AsyncMock()
    reader.latest_thread_event_id.return_value = "$latest"
    context = SimpleNamespace(client=object(), conversation_reader=reader)

    posted = await post_call_writeback(
        context=context,  # type: ignore[arg-type]
        origin=CallOrigin(room_id="!origin:example.org", thread_id="$root"),
        body="📞 Voice call · 1 min",
    )

    assert posted is True
    _client, room_id, content = send.await_args.args
    assert room_id == "!origin:example.org"
    relation = content["m.relates_to"]
    assert relation["rel_type"] == "m.thread"
    assert relation["event_id"] == "$root"
    assert relation["is_falling_back"] is True
    assert relation["m.in_reply_to"]["event_id"] == "$latest"


@pytest.mark.asyncio
async def test_post_call_writeback_falls_back_to_thread_root_when_latest_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown latest event falls back to the thread root."""
    send = AsyncMock(return_value=SimpleNamespace(event_id="$posted"))
    monkeypatch.setattr(call_writeback, "send_message_result", send)
    reader = AsyncMock()
    reader.latest_thread_event_id.return_value = None
    context = SimpleNamespace(client=object(), conversation_reader=reader)

    await post_call_writeback(
        context=context,  # type: ignore[arg-type]
        origin=CallOrigin(room_id="!origin:example.org", thread_id="$root"),
        body="x",
    )

    relation = send.await_args.args[2]["m.relates_to"]
    assert relation["m.in_reply_to"]["event_id"] == "$root"


@pytest.mark.asyncio
async def test_post_call_writeback_posts_room_level_origin_without_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A room-level origin gets a plain message with no thread relation."""
    send = AsyncMock(return_value=SimpleNamespace(event_id="$posted"))
    monkeypatch.setattr(call_writeback, "send_message_result", send)
    context = SimpleNamespace(client=object(), conversation_reader=AsyncMock())

    await post_call_writeback(
        context=context,  # type: ignore[arg-type]
        origin=CallOrigin(room_id="!origin:example.org", thread_id=None),
        body="📞 Voice call · 1 min",
    )

    content = send.await_args.args[2]
    assert "m.relates_to" not in content
    context.conversation_reader.latest_thread_event_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_call_writeback_reports_failed_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed send is reported to the caller."""
    monkeypatch.setattr(call_writeback, "send_message_result", AsyncMock(return_value=None))
    reader = AsyncMock()
    reader.latest_thread_event_id.return_value = "$latest"
    context = SimpleNamespace(client=object(), conversation_reader=reader)
    assert (
        await post_call_writeback(
            context=context,  # type: ignore[arg-type]
            origin=CallOrigin(room_id="!origin:example.org", thread_id="$root"),
            body="x",
        )
        is False
    )
