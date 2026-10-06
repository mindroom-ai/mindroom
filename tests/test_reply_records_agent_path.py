"""An agent turn writes its reply through durable records, at the ResponseRunner seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import DeliveryStage
from mindroom.reply_presentation import decode_presentation, render_body
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRunner
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot

pytestmark = pytest.mark.asyncio


async def _streaming_bot(tmp_path: Path) -> AgentBot:
    bot = _bot(tmp_path)
    # Startup makes this bot instance the owner of its replies, so a Stop finds its spans live.
    await bot._reply_runtime.start()
    unique_room_send_responses(bot.client)
    streaming = bot.config.defaults.streaming
    # Send every progress edit, so the write-ahead path runs on each chunk.
    streaming.update_interval = 0.001
    streaming.min_update_interval = 0.001
    streaming.max_idle = 0.001
    return bot


async def _reply(bot: AgentBot) -> rl.Reply:
    reply = await bot._reply_runtime.store.replies.for_sources(("$event",))
    assert reply is not None
    return reply


async def _span_outcomes(bot: AgentBot, reply: rl.Reply) -> list[rl.SpanOutcome | None]:
    return [span.outcome for span in await bot._reply_runtime.store.replies.spans(reply.reply_id)]


def _sent_bodies(bot: AgentBot) -> list[str]:
    bodies = []
    for call in bot.client.room_send.await_args_list:
        content = call.kwargs["content"]
        bodies.append(str(content.get("m.new_content", content).get("body", "")))
    return bodies


async def test_streamed_answer_completes_its_reply(tmp_path: Path) -> None:
    """The placeholder, the progress edits, and the answer are one reply; its sources settle with the answer."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        for chunk in ("Hello", " there", ", friend."):
            yield chunk
            await asyncio.sleep(0.01)

    with patch_response_runner_module(
        stream_agent_response=stream,
        should_use_streaming=AsyncMock(return_value=True),
        typing_indicator=_noop_typing,
    ):
        event_id = await runner.generate_response(_plain_request(_target()))

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.event_id == event_id == "$sent1"
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.COMPLETED]
    assert render_body(decode_presentation(reply.presentation))[0] == "Hello there, friend."
    # Every write took the reply's sequence, and Matrix acknowledged the last one.
    assert reply.possibly_shown_seq == reply.reply_sequence == reply.confirmed_seq
    assert reply.reply_sequence >= 3
    principal = bot._reply_runtime.store
    initial = await principal.load_matrix_delivery(delivery_id="$event", stage=DeliveryStage.INITIAL)
    final = await principal.load_matrix_delivery(delivery_id="$event", stage=DeliveryStage.FINAL)
    assert initial is not None
    assert final is not None
    assert (initial.reply_id, initial.reply_sequence) == (reply.reply_id, 1)
    assert final.reply_id == reply.reply_id
    assert final.reply_sequence == reply.reply_sequence
    assert not await principal.is_pending("$event")
    assert _sent_bodies(bot)[-1] == "Hello there, friend."


async def test_blocking_answer_completes_its_reply(tmp_path: Path) -> None:
    """A non-streamed answer is the reply's terminal row."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with patch_response_runner_module(
        ai_response=AsyncMock(return_value="A complete answer."),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        await runner.generate_response(_plain_request(_target()))

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert render_body(decode_presentation(reply.presentation))[0] == "A complete answer."
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.COMPLETED]
    assert not await bot._reply_runtime.store.is_pending("$event")


async def test_stop_during_the_stream_cancels_the_reply_with_its_note(tmp_path: Path) -> None:
    """A Stop recorded on the reply cancels exactly its span, which writes the cancelled terminal row."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    streaming = asyncio.Event()

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "Partial"
        streaming.set()
        await asyncio.Event().wait()
        yield "never"

    with patch_response_runner_module(
        stream_agent_response=stream,
        should_use_streaming=AsyncMock(return_value=True),
        typing_indicator=_noop_typing,
    ):
        response = asyncio.create_task(runner.generate_response(_plain_request(_target())))
        await asyncio.wait_for(streaming.wait(), timeout=5)
        reply = await _reply(bot)
        assert reply.event_id is not None
        assert await bot._delivery_gateway.record_reply_stop(reply.event_id, 7, newer_edit=False)
        await asyncio.wait_for(response, timeout=5)

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.CANCELLED
    assert not reply.unapplied_stop
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.CANCELLED]
    assert _sent_bodies(bot)[-1] == "Partial\n\n**[Response cancelled by user]**"
    assert not await bot._reply_runtime.store.is_pending("$event")


async def test_setup_failure_after_the_placeholder_shows_the_dispatch_error(tmp_path: Path) -> None:
    """A preparation failure after the placeholder fails the reply and delivers its error through the reply."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch.object(ResponseRunner, "prepare_response_runtime", new=AsyncMock(side_effect=RuntimeError("down"))),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ),
        pytest.raises(PostLockRequestPreparationError),
    ):
        await runner.generate_response(_plain_request(_target()))

    reply = await _reply(bot)
    assert reply.event_id == "$sent1"
    assert reply.state is rl.ReplyState.FAILED
    assert reply.owed_write is None
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.FAILED]
    assert reply.confirmed_seq == reply.reply_sequence == 2
    assert not await bot._reply_runtime.store.is_pending("$event")


async def test_retry_after_an_error_before_delivery_continues_the_same_reply(tmp_path: Path) -> None:
    """A failure before anything streamed keeps the placeholder; the retry answers into it as a replay."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch_response_runner_module(
            ai_response=AsyncMock(side_effect=RuntimeError("model down")),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
        pytest.raises(RuntimeError, match="model down"),
    ):
        await runner.generate_response(_plain_request(_target()))

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.ACTIVE
    assert reply.event_id == "$sent1"
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.RELEASED]

    # The journal retries the sources; the dispatcher names the placeholder it recovered.
    retry = replace(
        _plain_request(_target()),
        existing_event_id="$sent1",
        existing_event_is_placeholder=True,
        existing_event_is_recovered=True,
    )
    with patch_response_runner_module(
        ai_response=AsyncMock(return_value="Recovered answer."),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        assert await runner.generate_response(retry) == "$sent1"

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    spans = await bot._reply_runtime.store.replies.spans(reply.reply_id)
    assert [(span.kind, span.outcome) for span in spans] == [
        (rl.SpanKind.TURN, rl.SpanOutcome.RELEASED),
        (rl.SpanKind.REPLAY, rl.SpanOutcome.COMPLETED),
    ]
    assert _sent_bodies(bot) == ["Thinking...", "Recovered answer."]
    assert not await bot._reply_runtime.store.is_pending("$event")


async def test_an_event_no_reply_owns_keeps_mains_path(tmp_path: Path) -> None:
    """A recovered response written before durable records is answered without claiming a reply."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = replace(
        _plain_request(_target()),
        existing_event_id="$older",
        existing_event_is_placeholder=True,
    )
    with patch_response_runner_module(
        ai_response=AsyncMock(return_value="An answer."),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        await runner.generate_response(request)

    assert await bot._reply_runtime.store.replies.for_sources(("$event",)) is None


async def test_a_claim_the_rules_refuse_settles_with_a_dispatch_error(tmp_path: Path) -> None:
    """Sources that reach a terminal reply again are a lookup bug: the dispatch fails rather than add a second reply."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with patch_response_runner_module(
        ai_response=AsyncMock(return_value="A complete answer."),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        await runner.generate_response(_plain_request(_target()))
        completed = await _reply(bot)
        with pytest.raises(PostLockRequestPreparationError) as raised:
            await runner.generate_response(_plain_request(_target()))

    assert not raised.value.reply_owned
    assert raised.value.placeholder_event_id is None
    assert await _reply(bot) == completed
