"""A retried edit regeneration runs again only while its reply's records say the edit is unanswered."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_reply_records_turns import (
    _answer,
    _regeneration,
    _reply,
    _sent_bodies,
    _span_outcomes,
    _streaming_bot,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_runner import ResponseRequest

pytestmark = pytest.mark.asyncio


def _retry() -> ResponseRequest:
    """Return the edit regenerator's retry of the regeneration ``$edit`` drives."""
    return replace(_regeneration(answer_event_id="$sent1"), sync_restart_retry_source_event_id="$event")


async def _answered(tmp_path: Path) -> AgentBot:
    """Answer ``$event``, then admit its edit ``$edit``, pending until a regeneration answers it."""
    bot = await _streaming_bot(tmp_path)
    await _answer(bot, _plain_request(_target()), AsyncMock(return_value="First answer."))
    await bot.journal_principal().admit(
        InboundEvent(
            event_id="$edit",
            room_id=_target().room_id,
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=2,
            source={},
        ),
    )
    return bot


async def test_a_retry_of_an_edit_its_reply_answered_runs_nothing(tmp_path: Path) -> None:
    """The regeneration already finished, so its retry claims nothing and the answer stands."""
    bot = await _answered(tmp_path)
    await _answer(bot, _regeneration(answer_event_id="$sent1"), AsyncMock(return_value="Edited answer."))
    answered = await _reply(bot)
    sends = len(_sent_bodies(bot))

    answer = AsyncMock(return_value="Answered twice.")
    assert await _answer(bot, _retry(), answer) is None

    answer.assert_not_awaited()
    assert len(_sent_bodies(bot)) == sends
    assert await _reply(bot) == answered
    assert await _span_outcomes(bot, answered) == [rl.SpanOutcome.COMPLETED, rl.SpanOutcome.COMPLETED]


async def test_a_retry_of_an_edit_a_restart_interrupted_runs_it_again(tmp_path: Path) -> None:
    """A regeneration the restart cut short is re-run as the same regeneration, and answers."""
    old = await _answered(tmp_path)
    runner = unwrap_extracted_collaborator(old._response_runner)
    answering = asyncio.Event()

    async def interrupted(*_args: object, **_kwargs: object) -> str:
        answering.set()
        await asyncio.Event().wait()
        return "Never answered."

    with patch_response_runner_module(
        ai_response=AsyncMock(side_effect=interrupted),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        cut_short = asyncio.create_task(runner.generate_response(_regeneration(answer_event_id="$sent1")))
        await asyncio.wait_for(answering.wait(), timeout=5)

    restarted = _bot(tmp_path)
    unique_room_send_responses(restarted.client)
    await restarted._reply_runtime.start()
    try:
        assert await _answer(restarted, _retry(), AsyncMock(return_value="Edited answer.")) == "$sent1"
        reply = await _reply(restarted)
        assert reply.state is rl.ReplyState.COMPLETED
        assert await _span_outcomes(restarted, reply) == [
            rl.SpanOutcome.COMPLETED,
            rl.SpanOutcome.LOST,
            rl.SpanOutcome.COMPLETED,
        ]
        assert _sent_bodies(restarted)[-1] == "Edited answer."
    finally:
        cut_short.cancel()
        with suppress(asyncio.CancelledError):
            await cut_short
