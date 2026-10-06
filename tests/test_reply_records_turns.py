"""Agent and team turns write their replies through durable records, at the ResponseRunner seam."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import DeliveryStage, DepartureSource, EventClass, EventKind, InboundEvent
from mindroom.hooks import FinalResponseDraft
from mindroom.matrix.client_delivery import MatrixDeliveryFailure, MatrixDeliveryFailureKind, send_message_outcome
from mindroom.reply_presentation import TEAM_PLACEHOLDER, decode_presentation, render_body
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest, ResponseRunner
from mindroom.response_sources import ResponseSources
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import TurnRecord
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.journal_membership_helpers import admit_room_membership
from tests.response_runner_helpers import _bot, _envelope, _noop_typing, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.delivery_gateway import ResponseIdentity

pytestmark = pytest.mark.asyncio


async def _streaming_bot(tmp_path: Path) -> AgentBot:
    bot = _bot(tmp_path)
    # Startup makes this bot instance the owner of its replies, so a Stop finds its spans live.
    await bot._reply_runtime.start()
    # Ingress admitted the request, so the journal holds it pending until a reply settles it.
    await bot.journal_principal().admit(
        InboundEvent(
            event_id="$event",
            room_id="!room:localhost",
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1,
            source={},
        ),
    )
    assert await bot._reply_runtime.store.is_pending("$event")
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


async def _pending_turn(bot: AgentBot) -> None:
    """Record the turn ingress persisted for ``$event``; it names no reply event until it finishes."""
    turn = bot._turn_store.attach_response_context(
        TurnRecord.create(["$event"], requester_id="@user:localhost"),
        history_scope=bot._turn_store.response_history_scope(ResponseAction(kind="individual")),
        conversation_target=_target(),
    )
    await bot._turn_store.record_pending_turn(turn)


async def _stop(bot: AgentBot, event_id: str, receipt_order: int) -> asyncio.Task[bool]:
    """Start a Stop on the reply showing ``event_id``, as a Stop reaction does: through the turn's durable Stop."""
    await _pending_turn(bot)
    stop = asyncio.create_task(
        bot._user_stop_reconciler.finalize(event_id, receipt_order, AsyncMock(), room_id=_target().room_id),
    )

    async def recorded() -> None:
        # The reply's record is the only observable the Stop commits before it waits for the lock.
        while (await _reply(bot)).stop_receipt_order != receipt_order:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await asyncio.wait_for(recorded(), timeout=5)
    return stop


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
        stop = await _stop(bot, reply.event_id, 7)
        await asyncio.wait_for(response, timeout=5)
        assert await asyncio.wait_for(stop, timeout=5)

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
    assert await bot._reply_runtime.store.is_pending("$event")

    # The journal retries the sources; the claim finds the reply its first attempt left.
    retry = _plain_request(_target())
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


async def test_team_answer_completes_its_reply(tmp_path: Path) -> None:
    """A team answer is one reply under the team placeholder, ended by its answer row."""
    bot = await _streaming_bot(tmp_path)
    bot.orchestrator = MagicMock(config=bot.config)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with patch_response_runner_module(
        team_response=AsyncMock(return_value="Team answer."),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        event_id = await runner.generate_team_response_helper(
            _plain_request(_target()),
            team_agents=[bot.matrix_id],
            team_mode="coordinate",
        )

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.event_id == event_id == "$sent1"
    assert decode_presentation(reply.presentation).placeholder == TEAM_PLACEHOLDER
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.COMPLETED]
    assert _sent_bodies(bot) == [TEAM_PLACEHOLDER, "Team answer."]


async def test_streamed_team_answer_completes_its_reply(tmp_path: Path) -> None:
    """A streamed team document replaces its body on each tick; the reply records the last one."""
    bot = await _streaming_bot(tmp_path)
    bot.orchestrator = MagicMock(config=bot.config)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        for chunk in ("Team", "Team answer."):
            yield chunk
            await asyncio.sleep(0.01)

    with patch_response_runner_module(
        team_response_stream=stream,
        should_use_streaming=AsyncMock(return_value=True),
        typing_indicator=_noop_typing,
    ):
        await runner.generate_team_response_helper(
            _plain_request(_target()),
            team_agents=[bot.matrix_id],
            team_mode="coordinate",
        )

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert render_body(decode_presentation(reply.presentation))[0] == _sent_bodies(bot)[-1]
    assert reply.possibly_shown_seq == reply.reply_sequence == reply.confirmed_seq
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.COMPLETED]


def _regeneration(*, answer_event_id: str, edit_event_id: str = "$edit") -> ResponseRequest:
    """Return the request an edit of ``$event`` drives, regenerating the answer its turn record names."""
    target = _target()
    return replace(
        _plain_request(target),
        response_envelope=_envelope(target, source_event_id=edit_event_id),
        sources=ResponseSources(
            pending_event_ids=(edit_event_id,),
            logical_source_event_ids=("$event",),
            edit_receipt_order=1,
        ),
        prepared_edit_record=TurnRecord.create(["$event"], response_event_id=answer_event_id, completed=True),
        existing_event_id=answer_event_id,
    )


async def _answer(bot: AgentBot, request: ResponseRequest, answer: AsyncMock) -> str | None:
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with patch_response_runner_module(
        ai_response=answer,
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        return await runner.generate_response(request)


async def test_regeneration_replaces_the_answer_of_the_same_reply(tmp_path: Path) -> None:
    """An edit regenerates the reply in place: one reply, a regeneration span, and the new answer as its body."""
    bot = await _streaming_bot(tmp_path)
    await _answer(bot, _plain_request(_target()), AsyncMock(return_value="First answer."))

    assert await _answer(bot, _regeneration(answer_event_id="$sent1"), AsyncMock(return_value="Second answer.")) == (
        "$sent1"
    )

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert render_body(decode_presentation(reply.presentation))[0] == "Second answer."
    spans = await bot._reply_runtime.store.replies.spans(reply.reply_id)
    assert [(span.kind, span.outcome) for span in spans] == [
        (rl.SpanKind.TURN, rl.SpanOutcome.COMPLETED),
        (rl.SpanKind.REGENERATION, rl.SpanOutcome.COMPLETED),
    ]
    assert spans[-1].delivery_id == "$edit"
    # The regeneration edits the answer itself; no placeholder is sent over it.
    assert _sent_bodies(bot) == ["Thinking...", "First answer.", "Second answer."]


async def test_regeneration_failing_before_its_first_write_restores_the_old_answer(tmp_path: Path) -> None:
    """A regeneration that never wrote leaves the old answer and its records as they were."""
    bot = await _streaming_bot(tmp_path)
    await _answer(bot, _plain_request(_target()), AsyncMock(return_value="First answer."))
    before = await _reply(bot)

    with pytest.raises(RuntimeError, match="model down"):
        await _answer(bot, _regeneration(answer_event_id="$sent1"), AsyncMock(side_effect=RuntimeError("model down")))

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.presentation == before.presentation
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.COMPLETED, rl.SpanOutcome.RESTORED]
    assert _sent_bodies(bot) == ["Thinking...", "First answer."]


async def test_regenerating_an_answer_older_than_the_records_adopts_it(tmp_path: Path) -> None:
    """An answer written before durable records becomes a reply when an edit first regenerates it."""
    bot = await _streaming_bot(tmp_path)

    assert await _answer(bot, _regeneration(answer_event_id="$older"), AsyncMock(return_value="New answer.")) == (
        "$older"
    )

    reply = await bot._reply_runtime.store.replies.for_event("$older")
    assert reply is not None
    assert reply.state is rl.ReplyState.COMPLETED
    spans = await bot._reply_runtime.store.replies.spans(reply.reply_id)
    assert [(span.kind, span.outcome) for span in spans] == [(rl.SpanKind.REGENERATION, rl.SpanOutcome.COMPLETED)]
    edit = bot.client.room_send.await_args_list[-1].kwargs["content"]
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$older"}
    assert edit["m.new_content"]["body"] == "New answer."


async def _acknowledge_selection(bot: AgentBot) -> tuple[str | None, str | None]:
    return await bot._visible_responses.deliver_selection_acknowledgement(
        TurnRecord.create(["$event"], requester_id="@user:localhost"),
        target=_target(),
        requester_id="@user:localhost",
        response_text="You selected: 1 Yes\n\nProcessing your response...",
        delivery_turn_id="$event",
    )


async def test_selection_answer_adopts_the_span_its_acknowledgement_created(tmp_path: Path) -> None:
    """The acknowledgement creates the reply; the answer runs in that same span and edits the acknowledgement."""
    bot = await _streaming_bot(tmp_path)
    ack_event_id, span_id = await _acknowledge_selection(bot)
    assert ack_event_id == "$sent1"
    assert span_id is not None
    acknowledged = await _reply(bot)
    assert acknowledged.placeholder_only
    assert acknowledged.current_span_id is None

    request = replace(
        _plain_request(_target()),
        existing_event_id=ack_event_id,
        existing_event_is_placeholder=True,
        interactive_span_id=span_id,
    )
    assert await _answer(bot, request, AsyncMock(return_value="Selected answer.")) == ack_event_id

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    spans = await bot._reply_runtime.store.replies.spans(reply.reply_id)
    assert [(span.span_id, span.outcome) for span in spans] == [(span_id, rl.SpanOutcome.COMPLETED)]
    assert _sent_bodies(bot) == ["You selected: 1 Yes\n\nProcessing your response...", "Selected answer."]


async def test_a_retried_acknowledgement_finds_the_reply_its_first_attempt_created(tmp_path: Path) -> None:
    """Sending the acknowledgement again resolves the first attempt's row; no second reply or message appears."""
    bot = await _streaming_bot(tmp_path)
    first = await _acknowledge_selection(bot)

    assert await _acknowledge_selection(bot) == first
    assert len(_sent_bodies(bot)) == 1


async def test_a_dispatch_failure_before_the_answer_ends_the_selection_reply(tmp_path: Path) -> None:
    """A failure before any span ran the selection shows the error on its acknowledgement and settles it."""
    bot = await _streaming_bot(tmp_path)
    ack_event_id, _span_id = await _acknowledge_selection(bot)
    assert ack_event_id is not None

    assert await bot._delivery_gateway.fail_reply_dispatch(ack_event_id, "[general] ⚠️ Error: lookup failed")

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.FAILED
    assert reply.owed_write is None
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.FAILED]
    assert _sent_bodies(bot)[-1] == "[general] ⚠️ Error: lookup failed"


async def test_stop_before_the_span_starts_its_task_cancels_it_before_the_model_runs(tmp_path: Path) -> None:
    """A Stop that reaches a span still preparing cancels its task the moment it registers."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    preparing = asyncio.Event()
    release = asyncio.Event()
    prepare = ResponseRunner.prepare_response_runtime

    async def slow_prepare(self: ResponseRunner, *args: object, **kwargs: object) -> object:
        preparing.set()
        await release.wait()
        return await prepare(self, *args, **kwargs)

    model = AsyncMock(return_value="An answer.")
    with (
        patch.object(ResponseRunner, "prepare_response_runtime", new=slow_prepare),
        patch_response_runner_module(
            ai_response=model,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
    ):
        response = asyncio.create_task(runner.generate_response(_plain_request(_target())))
        await asyncio.wait_for(preparing.wait(), timeout=5)
        stop = await _stop(bot, "$sent1", 7)
        release.set()
        await asyncio.wait_for(response, timeout=5)
        assert await asyncio.wait_for(stop, timeout=5)

    model.assert_not_awaited()
    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.CANCELLED
    assert not reply.unapplied_stop
    assert _sent_bodies(bot)[-1] == "**[Response cancelled by user]**"
    assert not await bot._reply_runtime.store.is_pending("$event")


async def test_a_retry_whose_source_ended_settles_the_reply_its_earlier_attempt_left(tmp_path: Path) -> None:
    """The first gate's rejection of a terminal source ends the released reply through its records."""
    bot = await _streaming_bot(tmp_path)
    with pytest.raises(RuntimeError, match="model down"):
        await _answer(bot, _plain_request(_target()), AsyncMock(side_effect=RuntimeError("model down")))
    assert (await _reply(bot)).state is rl.ReplyState.ACTIVE

    retry = replace(
        _plain_request(_target()),
        existing_event_id="$sent1",
        existing_event_is_placeholder=True,
        prepare_source_turn=AsyncMock(return_value=True),
    )
    assert await _answer(bot, retry, AsyncMock(return_value="Never.")) is None

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.GONE
    assert reply.redaction_pending == ()
    bot.client.room_redact.assert_awaited_once()
    assert "$sent1" in (*bot.client.room_redact.await_args.args, *bot.client.room_redact.await_args.kwargs.values())
    # Main's interrupted note is not written over the placeholder.
    assert _sent_bodies(bot) == ["Thinking..."]


async def test_a_final_transform_is_what_the_reply_shows_from_then_on(tmp_path: Path) -> None:
    """The transformed whole reply is its frozen display; the span's own answer stays canonical."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    hooks = unwrap_extracted_collaborator(bot._delivery_gateway).deps.response_hooks

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "Hello"

    async def shout(*, identity: ResponseIdentity, response_text: str) -> FinalResponseDraft:
        return FinalResponseDraft(
            response_text=response_text.upper() + "!",
            response_kind=identity.response_kind,
            envelope=identity.response_envelope,
        )

    with (
        patch.object(hooks, "_apply_final_response_transform", new=shout),
        patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ),
    ):
        await runner.generate_response(_plain_request(_target()))

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert render_body(decode_presentation(reply.presentation))[0] == "Hello"
    assert reply.frozen_display is not None
    assert render_body(decode_presentation(reply.frozen_display))[0] == "HELLO!"
    assert reply.possibly_shown == reply.frozen_display
    assert _sent_bodies(bot)[-1] == "HELLO!"


class _FlakyHomeserver:
    """Fails the first sends, then lets the real transport through."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.real = send_message_outcome
        self.refused = asyncio.Event()

    async def send(self, *args: object, **kwargs: object) -> object:
        if self.failures > 0:
            self.failures -= 1
            self.refused.set()
            return MatrixDeliveryFailure(MatrixDeliveryFailureKind.SEND_EXCEPTION, "homeserver hiccup")
        return await self.real(*args, **kwargs)


async def test_an_answer_is_recorded_before_an_earlier_row_that_cannot_be_sent_yet(tmp_path: Path) -> None:
    """A placeholder Matrix refused twice does not cost the answer: both rows are owed and sent in order."""
    bot = await _streaming_bot(tmp_path)
    flaky = _FlakyHomeserver(failures=2)
    with patch("mindroom.delivery_gateway.send_message_outcome", new=flaky.send):
        assert await _answer(bot, _plain_request(_target()), AsyncMock(return_value="A complete answer.")) is None

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.event_id is None
    assert bot.client.room_send.await_count == 0
    # The answer handed its sources over with its row; recovery delivers both rows in order.
    assert not await bot._reply_runtime.store.is_pending("$event")
    assert (await bot._delivery_gateway.recover_deliveries()).complete

    reply = await _reply(bot)
    assert reply.event_id == "$sent1"
    assert reply.confirmed_seq == reply.reply_sequence
    assert _sent_bodies(bot) == ["Thinking...", "A complete answer."]


async def test_a_retried_terminal_edit_resolves_to_the_row_it_recorded(tmp_path: Path) -> None:
    """A final edit Matrix refused once is retried as the same row, and the answer completes."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "Hello there."

    flaky = _FlakyHomeserver(failures=0)
    real_send = flaky.real

    async def fail_terminal_once(*args: object, **kwargs: object) -> object:
        content = args[2] if len(args) > 2 else kwargs.get("content")
        if (
            isinstance(content, dict)
            and content.get("io.mindroom.stream_status") == "completed"
            and flaky.failures == 0
        ):
            flaky.failures = -1
            return MatrixDeliveryFailure(MatrixDeliveryFailureKind.SEND_EXCEPTION, "homeserver hiccup")
        return await real_send(*args, **kwargs)

    with (
        patch("mindroom.delivery_gateway.send_message_outcome", new=fail_terminal_once),
        patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ),
    ):
        await runner.generate_response(_plain_request(_target()))

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.COMPLETED]
    assert (await bot._delivery_gateway.recover_deliveries()).complete
    reply = await _reply(bot)
    assert reply.confirmed_seq == reply.reply_sequence
    assert _sent_bodies(bot)[-1] == "Hello there."


async def _blocked_stream(
    bot: AgentBot,
    first_chunk: str | None = "Partial",
) -> tuple[asyncio.Task[str | None], asyncio.Event]:
    """Start a streamed answer that shows ``first_chunk``, if any, and then waits until cancelled."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    streaming = asyncio.Event()

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        if first_chunk is not None:
            yield first_chunk
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
    return response, streaming


async def test_a_stop_on_a_running_reply_does_not_wait_for_its_conversation(tmp_path: Path) -> None:
    """The reply's records take the Stop at once; the turn names the reply's event and its Stop, already settled."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    response, _streaming = await _blocked_stream(bot)
    reply = await _reply(bot)
    assert reply.event_id is not None
    await _pending_turn(bot)

    with patch.object(type(runner), "finalize_user_stop", new=AsyncMock()) as polling:
        assert await bot._user_stop_reconciler.finalize(reply.event_id, 7, AsyncMock(), room_id=_target().room_id)
    polling.assert_not_awaited()
    stopped = bot._turn_store.get_turn_record("$event")
    assert stopped is not None
    assert stopped.response_event_id == reply.event_id
    assert stopped.user_stop_settled_receipt_order == 7

    await asyncio.wait_for(response, timeout=5)
    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.CANCELLED
    assert _sent_bodies(bot)[-1] == "Partial\n\n**[Response cancelled by user]**"


def _stop_reaction(reacts_to: str) -> MagicMock:
    return MagicMock(key="🛑", reacts_to=reacts_to, sender="@user:localhost", event_id="$stop")


async def test_a_stop_reaction_reaches_a_running_reply_only_in_its_room(tmp_path: Path) -> None:
    """A running reply's records accept its Stop reaction; the same event named from another room is left alone."""
    bot = await _streaming_bot(tmp_path)
    response, _streaming = await _blocked_stream(bot)
    reply = await _reply(bot)
    assert reply.event_id is not None
    await _pending_turn(bot)
    dispatcher = bot._reaction_dispatcher
    with (
        patch.object(bot._journal_dispatcher, "claim_semantic_consumer", new=AsyncMock()),
        patch.object(bot._journal_dispatcher, "receipt_order", new=AsyncMock(return_value=7)),
    ):
        elsewhere = MagicMock(room_id="!elsewhere:localhost")
        assert not await dispatcher._maybe_handle_stop_reaction(elsewhere, _stop_reaction(reply.event_id), None)
        here = MagicMock(room_id=_target().room_id)
        assert await dispatcher._maybe_handle_stop_reaction(here, _stop_reaction(reply.event_id), None)

    await asyncio.wait_for(response, timeout=5)
    assert (await _reply(bot)).state is rl.ReplyState.CANCELLED


async def test_a_stop_before_the_create_is_acknowledged_applies_when_it_is(tmp_path: Path) -> None:
    """A Stop on the event a reply's create is still sending waits for that create, then cancels the reply."""
    bot = await _streaming_bot(tmp_path)
    # The homeserver created the placeholder, but its answers keep failing.
    flaky = _FlakyHomeserver(failures=1_000)
    with patch("mindroom.delivery_gateway.send_message_outcome", new=flaky.send):
        # The answer shows nothing yet: its first text would be a create this
        # homeserver refuses, which fails the stream before the Stop arrives.
        response, _streaming = await _blocked_stream(bot, first_chunk=None)
        await asyncio.wait_for(flaky.refused.wait(), timeout=5)
        reply = await _reply(bot)
        assert reply.event_id is None
        await _pending_turn(bot)

        # The user reacted to the event the homeserver did create.
        assert await bot._user_stop_reconciler.accepts_reply_stop("$sent1", _target().room_id)
        assert await bot._user_stop_reconciler.finalize("$sent1", 7, AsyncMock(), room_id=_target().room_id)
        assert (await _reply(bot)).stop_receipt_order is None
        pending = bot._turn_store.get_turn_record("$event")
        assert pending is not None
        assert not pending.completed

    assert (await bot._delivery_gateway.recover_deliveries()).complete
    await asyncio.wait_for(response, timeout=5)
    reply = await _reply(bot)
    assert reply.event_id == "$sent1"
    assert reply.state is rl.ReplyState.CANCELLED
    assert not reply.unapplied_stop
    stopped = bot._turn_store.get_turn_record("$event")
    assert stopped is not None
    assert stopped.user_stop_receipt_order == 7
    assert stopped.response_event_id == "$sent1"


async def test_a_stop_reaches_a_reply_whose_attempt_created_its_event(tmp_path: Path) -> None:
    """With no placeholder shown, the stream's first text creates the event inside the attempt; its Stop still cancels the span."""
    bot = await _streaming_bot(tmp_path)

    async def created() -> rl.Reply:
        while (reply := await _reply(bot)).event_id is None:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return reply

    # The placeholder's send fails, so the attempt starts without an event.
    flaky = _FlakyHomeserver(failures=1)
    with patch("mindroom.delivery_gateway.send_message_outcome", new=flaky.send):
        response, _streaming = await _blocked_stream(bot)
        reply = await asyncio.wait_for(created(), timeout=5)
        assert reply.event_id is not None
        stop = await _stop(bot, reply.event_id, 7)
        await asyncio.wait_for(response, timeout=5)
        assert await asyncio.wait_for(stop, timeout=5)

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.CANCELLED
    assert not reply.unapplied_stop
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.CANCELLED]
    assert _sent_bodies(bot)[-1] == "Partial\n\n**[Response cancelled by user]**"


async def test_a_stop_after_the_answer_was_written_changes_nothing(tmp_path: Path) -> None:
    """A Stop that reaches a reply after its terminal row is satisfied by it: the answer stands, with no note."""
    bot = await _streaming_bot(tmp_path)
    await _pending_turn(bot)
    await _answer(bot, _plain_request(_target()), AsyncMock(return_value="A complete answer."))
    answered = await _reply(bot)
    assert answered.event_id is not None
    sent = len(bot.client.room_send.await_args_list)

    assert await bot._user_stop_reconciler.finalize(answered.event_id, 7, AsyncMock(), room_id=_target().room_id)

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert reply.stop_receipt_order == 7
    assert not reply.unapplied_stop
    assert reply.presentation == answered.presentation
    assert reply.owed_write is None
    assert len(bot.client.room_send.await_args_list) == sent
    stopped = bot._turn_store.get_turn_record("$event")
    assert stopped is not None
    assert stopped.user_stop_settled_receipt_order == 7


async def test_a_stop_after_a_restart_cancels_the_reply_the_old_instance_left(tmp_path: Path) -> None:
    """With no span running it, a Stop cancels the reply at once: the note is owed and sent, the button redacted."""
    old = await _streaming_bot(tmp_path)

    async def shown_with_button() -> rl.Reply:
        while (reply := await _reply(old)).stop_button_event_id is None or "Partial" not in _sent_bodies(old):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return reply

    with patch("mindroom.response_attempt.is_user_online", new=AsyncMock(return_value=True)):
        response, _streaming = await _blocked_stream(old)
        left = await asyncio.wait_for(shown_with_button(), timeout=5)
    assert left.event_id is not None

    restarted = _bot(tmp_path)
    unique_room_send_responses(restarted.client)
    await restarted._reply_runtime.start()
    try:
        # Its source is still pending, so the reply waits for its replay; no span runs it.
        waiting = await _reply(restarted)
        assert waiting.state is rl.ReplyState.ACTIVE
        assert waiting.current_span_id is None
        stop = await _stop(restarted, left.event_id, 7)
        assert await asyncio.wait_for(stop, timeout=5)
        assert (await restarted._delivery_gateway.recover_deliveries()).complete

        reply = await _reply(restarted)
        assert reply.state is rl.ReplyState.CANCELLED
        assert not reply.unapplied_stop
        assert reply.owed_write is None
        assert reply.stop_button_event_id is None
        assert reply.redaction_pending == ()
        assert _sent_bodies(restarted)[-1] == "Partial\n\n**[Response cancelled by user]**"
        assert [call.args[1] for call in restarted.client.room_redact.await_args_list] == [left.stop_button_event_id]
        assert not await restarted._reply_runtime.store.is_pending("$event")
    finally:
        response.cancel()
        with suppress(asyncio.CancelledError):
            await response


async def test_leaving_the_room_mid_stream_ends_the_reply_without_writing_to_it(tmp_path: Path) -> None:
    """The departure ends the reply gone and releases its span; the cancelled stream writes nothing more."""
    bot = await _streaming_bot(tmp_path)
    response, _streaming = await _blocked_stream(bot)

    async def partial_shown() -> None:
        while "Partial" not in _sent_bodies(bot):  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await asyncio.wait_for(partial_shown(), timeout=5)
    reply = await _reply(bot)
    assert reply.event_id is not None
    sends = len(bot.client.room_send.await_args_list)

    await admit_room_membership(bot.journal_principal(), _target().room_id, "leave", source=DepartureSource.LOCAL)
    await bot._reply_runtime.departed(_target().room_id)
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(response, timeout=5)

    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.GONE
    assert await _span_outcomes(bot, reply) == [rl.SpanOutcome.RELEASED]
    assert len(bot.client.room_send.await_args_list) == sends
    assert bot._reply_runtime.spans.live_span_ids() == frozenset()


async def test_the_stop_button_is_the_replys_and_leaves_with_its_active_state(tmp_path: Path) -> None:
    """The button is sent once on the reply's event, recorded on the reply, and redacted when the reply ends."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch("mindroom.response_attempt.is_user_online", new=AsyncMock(return_value=True)),
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="An answer."),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
    ):
        await runner.generate_response(_plain_request(_target()))

    sends = bot.client.room_send.await_args_list
    buttons = [
        (f"$sent{index}", call.kwargs["content"])
        for index, call in enumerate(sends, start=1)
        if call.kwargs["message_type"] == "m.reaction"
    ]
    assert len(buttons) == 1
    button_id, button = buttons[0]
    assert button["m.relates_to"]["key"] == "🛑"
    reply = await _reply(bot)
    assert reply.state is rl.ReplyState.COMPLETED
    assert button["m.relates_to"]["event_id"] == reply.event_id
    assert reply.stop_button_event_id is None
    assert reply.redaction_pending == ()
    assert [call.args[1] for call in bot.client.room_redact.await_args_list] == [button_id]
    assert bot._response_runner.deps.stop_manager.tracked_messages == {}


async def test_history_counts_a_reply_in_progress_only_while_its_span_runs(tmp_path: Path) -> None:
    """A reply whose span runs here is in progress; once it ends, it is not."""
    bot = await _streaming_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    response, _streaming = await _blocked_stream(bot)
    reply = await _reply(bot)
    assert await runner._active_response_event_ids(_target().room_id) == {reply.event_id}
    assert await runner._active_response_event_ids("!elsewhere:localhost") == set()

    response.cancel()
    with suppress(asyncio.CancelledError):
        await response
    assert await runner._active_response_event_ids(_target().room_id) == set()


async def test_a_regeneration_that_waits_for_earlier_writes_keeps_its_edit(tmp_path: Path) -> None:
    """An edit whose reply still owes a write waits for it: its sources stay pending, owned by that row's wake."""
    bot = await _streaming_bot(tmp_path)
    real_send = _FlakyHomeserver(failures=0).real

    async def refuse_answers(*args: object, **kwargs: object) -> object:
        content = args[2] if len(args) > 2 else kwargs.get("content")
        if isinstance(content, dict) and content.get("io.mindroom.stream_status") == "completed":
            return MatrixDeliveryFailure(MatrixDeliveryFailureKind.SEND_EXCEPTION, "homeserver hiccup")
        return await real_send(*args, **kwargs)

    with patch("mindroom.delivery_gateway.send_message_outcome", new=refuse_answers):
        await _answer(bot, _plain_request(_target()), AsyncMock(return_value="The first answer."))
    reply = await _reply(bot)
    assert reply.event_id == "$sent1"
    assert await bot._reply_runtime.store.replies.has_unresolved_rows(reply.reply_id)

    request = replace(_regeneration(answer_event_id="$sent1"), source_handoff=asyncio.Event())
    model = AsyncMock(return_value="A fresh answer.")
    assert await _answer(bot, request, model) is None
    model.assert_not_awaited()
    # The edit regenerator reports the edit owned, so the journal keeps it for the retry.
    assert request.source_handoff is not None
    assert request.source_handoff.is_set()
    assert await _span_outcomes(bot, await _reply(bot)) == [rl.SpanOutcome.COMPLETED]
