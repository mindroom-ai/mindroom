"""Approval pauses, resumes, and their failures write the reply through durable records."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from agno.models.response import ToolExecution

from mindroom import reply_lifecycle as rl
from mindroom.approval_manager import initialize_approval_store
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.matrix.client_delivery import MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, shutdown_approval_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _noop_typing, _plain_request, _target
from tests.test_reply_records_turns import (
    _FlakyHomeserver,
    _pending_turn,
    _regeneration,
    _reply,
    _sent_bodies,
    _stop,
    _streaming_bot,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import MatrixDelivery

pytestmark = pytest.mark.asyncio


def _paused(run_id: str = "run-1", text: str = "Reading document") -> PausedAttempt:
    return PausedAttempt(
        session_id=_target().session_id,
        run_id=run_id,
        tools=(
            ToolExecution(
                tool_call_id=f"call-{run_id}",
                tool_name="read_document",
                requires_confirmation=True,
                approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
            ),
        ),
        response_text=text,
        toolkit_owners={("general", "read_document"): "test_toolkit"},
    )


@asynccontextmanager
async def _approval_bot(tmp_path: Path, *, requires_human: bool) -> AsyncIterator[AgentBot]:
    bot = await _streaming_bot(tmp_path)
    bot.config.agents["general"].memory_backend = "none"
    # Tool markers are not what these tests are about.
    bot.config.defaults.show_tool_calls = False

    async def prepare_event(_room: str, _thread: str | None, content: dict) -> dict:
        return content

    async def send_card(_delivery: MatrixDelivery) -> str:
        return "$approval-card"

    initialize_approval_store(
        bot.runtime_paths,
        prepare_event=prepare_event,
        send_delivery=send_card,
        resolve_delivery=AsyncMock(return_value=None),
        cards=bot._journal_store.principal("router@shared"),
        transport_sender=lambda: "@router:localhost",
        sending_device=lambda: "DEVICE",
    )
    try:
        with patch(
            "mindroom.approval_response.evaluate_tool_approval",
            AsyncMock(return_value=(requires_human, 60.0)),
        ):
            yield bot
    finally:
        await shutdown_approval_runtime()


async def _respond(bot: AgentBot, *, resume: AsyncMock | None = None) -> str | None:
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch_response_runner_module(
            ai_response=AsyncMock(side_effect=ResponsePausedForApproval(_paused())),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
        patch.object(type(runner), "_continue_entity_call", resume or AsyncMock()),
    ):
        return await runner.generate_response(_plain_request(_target()))


@contextmanager
def _journal_wakes(bot: AgentBot) -> Iterator[list[tuple[str, ...]]]:
    """Collect the sources the reply records hand back to the journal, which these tests do not run."""
    wakes: list[tuple[str, ...]] = []
    with patch.object(
        bot._journal_dispatcher,
        "retry_turn_sources",
        side_effect=lambda _room_id, sources: wakes.append(sources),
    ):
        yield wakes


async def _run_approval_wakes(bot: AgentBot, wakes: list[tuple[str, ...]]) -> None:
    """Do what the journal's worker does with a woken approval source: hand it to its continuation."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    while wakes:
        assert await runner.handoff_approval_source(wakes.pop(0)[0]) is False
        await runner.wait_for_source_owned_inbox_responses()


async def _superseded_cleanup(bot: AgentBot) -> None:
    """Wait for the old approval's cleanup, which runs outside the conversation."""

    async def finished() -> None:
        while await bot.journal_principal().approval_continuation_for_source("$event") is not None:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await asyncio.wait_for(finished(), timeout=5)


async def _span_kinds(bot: AgentBot) -> list[tuple[rl.SpanKind, rl.SpanOutcome | None]]:
    reply = await _reply(bot)
    return [(span.kind, span.outcome) for span in await bot._reply_runtime.store.replies.spans(reply.reply_id)]


async def test_an_approved_resume_completes_the_reply_it_paused(tmp_path: Path) -> None:
    """The pause and the resume are spans of one reply; the resume's answer ends it."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        await _respond(bot, resume=AsyncMock(return_value=CompletedApprovalRun("Approved answer.", {})))

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.COMPLETED
        assert reply.approval_id is None
        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, rl.SpanOutcome.COMPLETED),
        ]
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert not await bot._reply_runtime.store.is_pending("$event")
        assert _sent_bodies(bot) == ["Thinking...", "Reading document", "Approved answer."]


async def test_a_pause_waiting_for_a_human_leaves_the_reply_paused(tmp_path: Path) -> None:
    """A pause row shows the waiting answer; the reply waits for its approval."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)

        reply = await _reply(bot)
        continuation = await bot.journal_principal().approval_continuation_for_source("$event")
        assert continuation is not None
        assert continuation.state == "waiting"
        assert reply.state is rl.ReplyState.PAUSED
        assert reply.approval_id == continuation.approval_id
        assert reply.confirmed_seq == reply.reply_sequence
        assert await _span_kinds(bot) == [(rl.SpanKind.TURN, rl.SpanOutcome.PAUSED)]
        assert _sent_bodies(bot) == ["Thinking...", "Reading document"]


async def test_stop_on_a_paused_reply_cancels_it_through_its_approval(tmp_path: Path) -> None:
    """A Stop fences the approval with the turn's durable Stop; the settlement shows the note and ends the reply."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        with _journal_wakes(bot) as wakes:
            stop = await _stop(bot, "$sent1", 5)
            assert await asyncio.wait_for(stop, timeout=5)
        # The Stop fenced the approval and woke its source; its settlement expires the cards.
        assert wakes == [("$event",)]
        await _run_approval_wakes(bot, wakes)

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.CANCELLED
        assert not reply.unapplied_stop
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert _sent_bodies(bot)[-1] == "**[Response cancelled by user]**"


async def _reply_in(bot: AgentBot, state: rl.ReplyState) -> rl.Reply:
    """Wait until the reply for ``$event`` exists and is in ``state``."""

    async def reached() -> rl.Reply:
        while (reply := await bot._reply_runtime.store.replies.for_sources(("$event",))) is None or (  # noqa: ASYNC110
            reply.state is not state
        ):
            await asyncio.sleep(0.01)
        return reply

    return await asyncio.wait_for(reached(), timeout=5)


async def test_stop_on_a_reply_waiting_in_place_cancels_its_wait_through_its_approval(tmp_path: Path) -> None:
    """A Stop during a response-local approval wait cancels the waiting span; the approval's settlement ends the reply."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)

        async def wait_in_place(*_args: object, **_kwargs: object) -> str:
            # An agent CLI call inside the run asks for approval and waits for it here.
            context = get_tool_runtime_context()
            assert context is not None
            assert context.cli_approval_handler is not None
            cli_call = {"kind": "agent_cli", "call_id": "inner", "parent_bash_call_id": "bash"}
            await context.cli_approval_handler(replace(_paused(), cli_call=cli_call))
            return "Never answered."

        published = asyncio.Event()
        publish_generation = runner._approval_responses.publish_generation

        async def publish_then_wait(*args: object, **kwargs: object) -> object:
            result = await publish_generation(*args, **kwargs)
            published.set()
            return result

        with (
            patch_response_runner_module(
                ai_response=AsyncMock(side_effect=wait_in_place),
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing,
            ),
            patch.object(runner._approval_responses, "publish_generation", new=publish_then_wait),
        ):
            response = asyncio.create_task(runner.generate_response(_plain_request(_target())))
            # The cards are out and the run waits for the decision.
            await asyncio.wait_for(published.wait(), timeout=5)
            waiting = await _reply_in(bot, rl.ReplyState.PAUSED)
            # The wait keeps its span, and with it the Stop button.
            assert waiting.current_span_id is not None
            with _journal_wakes(bot) as wakes:
                stop = await _stop(bot, "$sent1", 5)
                assert await asyncio.wait_for(stop, timeout=5)
                await asyncio.wait_for(response, timeout=5)
        # The Stop fenced the approval and woke its source, but the cancelled
        # wait already ran its settlement, which expired the cards.
        assert wakes == [("$event",)]
        assert await runner.handoff_approval_source("$event") is None

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.CANCELLED
        assert not reply.unapplied_stop
        assert reply.approval_id is None
        assert await _span_kinds(bot) == [(rl.SpanKind.TURN, rl.SpanOutcome.CANCELLED)]
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert not await bot._reply_runtime.store.is_pending("$event")
        assert _sent_bodies(bot)[-1] == "Reading document\n\n**[Response cancelled by user]**"


async def test_stop_during_an_approval_resume_cancels_it_through_its_approval(tmp_path: Path) -> None:
    """A Stop on a resuming reply cancels the resume span; the approval's settlement shows the note."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        resuming = asyncio.Event()

        async def resume_until_cancelled(*_args: object, **_kwargs: object) -> CompletedApprovalRun:
            resuming.set()
            await asyncio.Event().wait()
            return CompletedApprovalRun("Never answered.", {})

        response = asyncio.create_task(_respond(bot, resume=AsyncMock(side_effect=resume_until_cancelled)))
        await asyncio.wait_for(resuming.wait(), timeout=5)
        with _journal_wakes(bot) as wakes:
            stop = await _stop(bot, "$sent1", 5)
            assert await asyncio.wait_for(stop, timeout=5)
            await asyncio.wait_for(response, timeout=5)
        # The cancelled resume already ran the settlement its wake asked for.
        assert wakes == [("$event",)]
        runner = unwrap_extracted_collaborator(bot._response_runner)
        assert await runner.handoff_approval_source("$event") is None

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.CANCELLED
        assert not reply.unapplied_stop
        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, rl.SpanOutcome.CANCELLED),
        ]
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert not await bot._reply_runtime.store.is_pending("$event")
        assert _sent_bodies(bot)[-1] == "**[Response cancelled by user]**"


async def test_a_failed_resume_shows_its_failure_and_ends_the_reply(tmp_path: Path) -> None:
    """The resume span ends first; the continuation's settlement writes the note and fails the reply."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        await _respond(bot, resume=AsyncMock(side_effect=RuntimeError("tool exploded")))

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.FAILED
        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, rl.SpanOutcome.FAILED),
        ]
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert "tool exploded" in _sent_bodies(bot)[-1]


async def test_a_chained_pause_pauses_the_resume_span_and_the_next_resume_answers(tmp_path: Path) -> None:
    """Each generation is one resume span; the chained pause is written with the continuation's advance."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        resume = AsyncMock(
            side_effect=[
                _paused(run_id="run-2", text="Reading the second document"),
                CompletedApprovalRun("Both read.", {}),
            ],
        )
        await _respond(bot, resume=resume)

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.COMPLETED
        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, rl.SpanOutcome.COMPLETED),
        ]
        assert _sent_bodies(bot) == ["Thinking...", "Reading document", "Reading the second document", "Both read."]


async def test_an_edit_supersedes_the_approval_of_the_reply_it_regenerates(tmp_path: Path) -> None:
    """Decision 1: the old approval settles without a note while the regeneration still runs."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        old = await bot.journal_principal().approval_continuation_for_source("$event")
        assert old is not None

        runner = unwrap_extracted_collaborator(bot._response_runner)
        answering, release = asyncio.Event(), asyncio.Event()

        async def slow_answer(*_args: object, **_kwargs: object) -> str:
            answering.set()
            await release.wait()
            return "A fresh answer."

        with patch_response_runner_module(
            ai_response=AsyncMock(side_effect=slow_answer),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            response = asyncio.create_task(runner.generate_response(_regeneration(answer_event_id="$sent1")))
            await asyncio.wait_for(answering.wait(), timeout=5)
            sends = len(_sent_bodies(bot))

            async def settled() -> None:
                while await bot.journal_principal().approval_continuation_for_source("$event") is not None:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)

            # The regeneration holds the conversation; the old approval's cleanup does not wait for it.
            await asyncio.wait_for(settled(), timeout=5)
            assert not await bot._reply_runtime.store.is_pending("$event")
            assert len(_sent_bodies(bot)) == sends
            release.set()
            assert await asyncio.wait_for(response, timeout=5) == "$sent1"

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.COMPLETED
        assert _sent_bodies(bot)[-1] == "A fresh answer."


async def test_a_regeneration_of_a_paused_reply_that_fails_ends_it_interrupted(tmp_path: Path) -> None:
    """Rolling back a superseded pause never restores consent: the reply fails with the interruption note."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        runner = unwrap_extracted_collaborator(bot._response_runner)
        with (
            patch_response_runner_module(
                ai_response=AsyncMock(side_effect=RuntimeError("model down")),
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing,
            ),
            pytest.raises(RuntimeError, match="model down"),
        ):
            await runner.generate_response(_regeneration(answer_event_id="$sent1"))

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.FAILED
        assert reply.approval_id is None
        assert reply.owed_write is None
        assert _sent_bodies(bot)[-1] == "Reading document\n\n**[Response interrupted]**"
        # The superseded approval's cleanup finished it.
        await _superseded_cleanup(bot)


async def test_a_regeneration_of_a_paused_reply_may_pause_again(tmp_path: Path) -> None:
    """The edit's new answer asks again; the old approval is superseded and one continuation holds the reply."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        # Ingress admitted the edit; the new continuation takes it as its source.
        await bot.journal_principal().admit(
            InboundEvent(
                event_id="$edit",
                room_id="!room:localhost",
                thread_id=None,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender="@user:localhost",
                origin_server_ts=2,
                source={},
            ),
        )
        runner = unwrap_extracted_collaborator(bot._response_runner)
        with patch_response_runner_module(
            ai_response=AsyncMock(side_effect=ResponsePausedForApproval(_paused(run_id="run-edit", text="Rereading"))),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            await runner.generate_response(_regeneration(answer_event_id="$sent1"))

        reply = await _reply(bot)
        newer = await bot.journal_principal().approval_continuation_for_source("$edit")
        assert newer is not None
        assert reply.state is rl.ReplyState.PAUSED
        assert reply.approval_id == newer.approval_id
        # The old approval was superseded, and its cleanup finished it.
        await _superseded_cleanup(bot)
        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.REGENERATION, rl.SpanOutcome.PAUSED),
        ]
        assert _sent_bodies(bot)[-1] == "Rereading"


async def test_a_failure_note_that_could_not_be_sent_is_resent_not_written_again(tmp_path: Path) -> None:
    """Settlement retries resend the one note row it recorded, then finish the reply."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        runner = unwrap_extracted_collaborator(bot._response_runner)
        continuation = await bot.journal_principal().approval_continuation_for_source("$event")
        assert continuation is not None
        failing = await runner._approval_responses.request_failure(continuation, "Card publication failed")
        assert failing is not None
        before = (await _reply(bot)).reply_sequence

        flaky = _FlakyHomeserver(failures=2)
        with patch("mindroom.delivery_gateway.send_message_outcome", new=flaky.send):
            assert not await runner._approval_responses.settle_failure(failing, "Card publication failed")
            assert not await runner._approval_responses.settle_failure(failing, "Card publication failed")
            assert await runner._approval_responses.settle_failure(failing, "Card publication failed")

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.FAILED
        assert reply.reply_sequence == before + 1
        assert reply.confirmed_seq == reply.reply_sequence
        assert _sent_bodies(bot)[-1] == "Card publication failed"


async def test_a_stop_recorded_before_the_failure_note_decides_it(tmp_path: Path) -> None:
    """A Stop that lands while a failed approval settles makes its note the cancellation, as its state is."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        runner = unwrap_extracted_collaborator(bot._response_runner)
        gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
        continuation = await bot.journal_principal().approval_continuation_for_source("$event")
        assert continuation is not None
        failing = await runner._approval_responses.request_failure(continuation, "Card publication failed")
        assert failing is not None
        # The Stop commits with the turn record, before this settlement writes its note.
        await _pending_turn(bot)
        stop = await gateway.reply_stop("$sent1", 5, room_id=_target().room_id, may_wait=False)
        assert await bot._turn_store.record_user_stopped_response("$sent1", 5, turn_id=stop.turn_id, also=stop)

        assert await runner._approval_responses.settle_failure(failing, "Card publication failed")

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.CANCELLED
        assert _sent_bodies(bot)[-1] == "**[Response cancelled by user]**"


async def test_a_pause_whose_send_failed_once_still_waits_for_its_approval(tmp_path: Path) -> None:
    """The pause row commits with the continuation, so a refused send leaves it owed, not the approval failed."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        real_send = _FlakyHomeserver(failures=0).real
        failed: list[str] = []

        async def refuse_the_pause_once(*args: object, **kwargs: object) -> object:
            content = args[2] if len(args) > 2 else kwargs.get("content")
            new_content = content.get("m.new_content", {}) if isinstance(content, dict) else {}
            if not failed and new_content.get("body") == "Reading document":
                failed.append("pause")
                return MatrixDeliveryFailure(MatrixDeliveryFailureKind.SEND_EXCEPTION, "homeserver hiccup")
            return await real_send(*args, **kwargs)

        with patch("mindroom.delivery_gateway.send_message_outcome", new=refuse_the_pause_once):
            await _respond(bot)

        assert failed == ["pause"]
        continuation = await bot.journal_principal().approval_continuation_for_source("$event")
        assert continuation is not None
        assert continuation.state == "waiting"
        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.PAUSED
        assert reply.confirmed_seq != reply.reply_sequence
        assert (await bot._delivery_gateway.recover_deliveries()).complete
        reply = await _reply(bot)
        assert reply.confirmed_seq == reply.reply_sequence
        assert _sent_bodies(bot)[-1] == "Reading document"
