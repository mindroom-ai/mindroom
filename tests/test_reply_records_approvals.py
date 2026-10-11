"""Approval pauses, resumes, and their failures write the reply through durable records."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from mindroom import reply_lifecycle as rl
from mindroom.approval_manager import initialize_approval_store
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.cancellation import request_task_cancel
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, InboundEvent
from mindroom.event_journal.replies import ReplyStore
from mindroom.matrix.client_delivery import MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.matrix.journal_ingress import _inbound_event, _projected_event
from mindroom.reply_presentation import (
    NoteKind,
    Segment,
    decode_presentation,
    encode_presentation,
    note_segment,
    render_body,
)
from mindroom.response_turn import CompletedApprovalRun, PausedAnswer, PausedAttempt, ResponsePausedForApproval
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, shutdown_approval_runtime
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.turn_store import TurnStore
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_reply_records_turns import (
    _admit_edit,
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
    from mindroom.message_target import MessageTarget

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


async def _respond(
    bot: AgentBot,
    *,
    resume: AsyncMock | None = None,
    target: MessageTarget | None = None,
) -> str | None:
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = target or _target()
    with (
        patch_response_runner_module(
            ai_response=AsyncMock(
                side_effect=ResponsePausedForApproval(replace(_paused(), session_id=target.session_id)),
            ),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
        patch.object(type(runner), "_continue_entity_call", resume or AsyncMock()),
    ):
        return await runner.generate_response(_plain_request(target))


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


async def test_a_finished_approval_records_its_turn_answered(tmp_path: Path) -> None:
    """Finishing the continuation settles the turn's sources through the reply path, which records the turn answered."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        await _pending_turn(bot)
        with patch.object(TurnStore, "terminal_turn_record", return_value=None):
            await _respond(bot, resume=AsyncMock(return_value=CompletedApprovalRun("Approved answer.", {})))

        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        record = bot._turn_store.get_turn_record("$event")
        assert record is not None
        assert record.completed
        assert bot._turn_store.is_handled("$event")


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


async def test_a_handoff_that_fails_after_its_pause_committed_writes_nothing_behind_the_reply(tmp_path: Path) -> None:
    """Once the pause committed, the reply is its approval's: no direct failure edit replaces what it shows."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)
        with (
            patch.object(
                runner._approval_responses,
                "publish_generation",
                AsyncMock(side_effect=RuntimeError("cards unavailable")),
            ),
            patch.object(type(runner), "_failed_approval_handoff", AsyncMock(return_value=None)),
        ):
            await _respond(bot)

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.PAUSED
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
        # The Stop keeps what the reply showed and adds its note, as on any reply.
        assert _sent_bodies(bot)[-1] == "Reading document\n\n**[Response cancelled by user]**"


async def test_a_pending_approval_keeps_its_conversation_busy_until_it_ends(tmp_path: Path) -> None:
    """Later messages of the conversation wait while the approval is pending, as behind a running reply."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        target = _target()
        runner = bot._response_runner
        assert runner.is_held_for_approval(target)
        assert target.resolved_thread_id in runner.active_thread_ids_for_room(target.room_id)
        idle = asyncio.create_task(runner.wait_for_thread_response_idle(target.room_id, target.resolved_thread_id))
        await asyncio.sleep(0)
        assert not idle.done()
        with _journal_wakes(bot) as wakes:
            assert await asyncio.wait_for(await _stop(bot, "$sent1", 5), timeout=5)
        await _run_approval_wakes(bot, wakes)
        # The approval's end lets the conversation answer what waited.
        assert not runner.is_held_for_approval(target)
        await asyncio.wait_for(idle, timeout=5)


async def _send_message(bot: AgentBot, event_id: str, body: str, *, thread_id: str | None = None) -> None:
    """Hand a message from the requester to the bot, as ingress does once the journal admitted it."""
    room = nio.MatrixRoom(_target().room_id, bot.matrix_id.full_id)
    room.add_member("@user:localhost", "User", None)
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    message = nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 3,
            "room_id": room.room_id,
            "type": "m.room.message",
        },
    )
    await bot.journal_principal().admit(
        InboundEvent(
            event_id=event_id,
            room_id=room.room_id,
            thread_id=thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=3,
            source={},
        ),
    )
    await bot._turn_controller.handle_text_event(room, message)
    await wait_for_background_tasks(timeout=5.0, owner=bot._turn_controller.deps.runtime)


def _reactions(bot: AgentBot) -> list[dict[str, str]]:
    return [
        call.kwargs["content"]["m.relates_to"]
        for call in bot.client.room_send.await_args_list
        if call.kwargs["message_type"] == "m.reaction"
    ]


async def _wait_until_sent(bot: AgentBot, body: str) -> None:
    async def sent() -> None:
        while body not in _sent_bodies(bot):  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await asyncio.wait_for(sent(), timeout=5)


async def test_a_message_sent_while_an_approval_waits_is_answered_once_after_it_ends(tmp_path: Path) -> None:
    """A follow-up to a conversation an approval holds gets ⏳, starts no turn of its own, and is answered after the decision.

    A turn started beside the paused one would not see the paused request answered and could ask for the same tool again.
    """
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        # The paused reply answers in room mode, so a later top-level message is part of its conversation.
        bot.config.agents["general"].thread_mode = "room"
        await _respond(bot)

        async def answer(*_args: object, **_kwargs: object) -> str:
            # A turn that runs while the approval waits cannot see the paused run, so it repeats the gated call.
            if await bot.journal_principal().approval_continuation_for_source("$event") is not None:
                raise ResponsePausedForApproval(_paused("run-2"))
            return "Follow-up answer."

        model = AsyncMock(side_effect=answer)
        with patch_response_runner_module(
            ai_response=model,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            await _send_message(bot, "$followup", "thanks")

            assert _reactions(bot) == [{"rel_type": "m.annotation", "event_id": "$followup", "key": "⏳"}]
            # The follow-up waits behind the approval: no second turn runs beside the paused one.
            runner = unwrap_extracted_collaborator(bot._response_runner)
            assert runner._lifecycle_coordinator._get_or_create_queued_signal(_target()).has_pending_human_messages()
            model.assert_not_awaited()
            assert runner.is_held_for_approval(_target())

            with _journal_wakes(bot) as wakes:
                assert await asyncio.wait_for(await _stop(bot, "$sent1", 5), timeout=5)
            await _run_approval_wakes(bot, wakes)
            # A follow-up turn that ran beside the paused reply would now show a second card for the same call.
            assert await bot._journal_store.principal("router@shared").pending_approval_room_ids() == ()
            await _wait_until_sent(bot, "Follow-up answer.")

        model.assert_awaited_once()
        assert await bot.journal_principal().approval_continuation_for_source("$followup") is None
        assert not await bot._reply_runtime.store.is_pending("$followup")


async def test_a_message_in_another_conversation_is_answered_while_an_approval_waits(tmp_path: Path) -> None:
    """A pending approval holds only its own conversation.

    A message that starts another conversation is answered at once, even while a follow-up waits in the held one.
    """
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        # In thread mode the paused request roots its own thread, and a later top-level message starts another one.
        paused = _target(thread_id="$event")
        await _respond(bot, target=paused)
        model = AsyncMock(return_value="Other answer.")
        with patch_response_runner_module(
            ai_response=model,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            await _send_message(bot, "$followup", "thanks", thread_id="$event")
            await _send_message(bot, "$other", "something else")
            await _wait_until_sent(bot, "Other answer.")

        # Only the other conversation's message ran; the follow-up still waits with ⏳.
        model.assert_awaited_once()
        assert _reactions(bot) == [{"rel_type": "m.annotation", "event_id": "$followup", "key": "⏳"}]
        assert not await bot._reply_runtime.store.is_pending("$other")
        # The approval still waits and still holds its own conversation.
        assert bot._response_runner.is_held_for_approval(paused)
        assert await bot.journal_principal().approval_continuation_for_source("$event") is not None


async def test_a_hold_whose_approval_is_gone_does_not_keep_its_conversation_waiting(tmp_path: Path) -> None:
    """A waiting message rechecks the approvals that hold its conversation, so a hold whose end was lost goes."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        target = _target()
        runner = unwrap_extracted_collaborator(bot._response_runner)
        runner._lifecycle_coordinator.hold_for_approval("approval-gone", target)
        assert runner.is_held_for_approval(target)
        with patch("mindroom.response_runner._APPROVAL_HOLD_RECHECK_SECONDS", 0.01):
            await asyncio.wait_for(
                runner.wait_for_thread_response_idle(target.room_id, target.resolved_thread_id),
                timeout=5,
            )
        assert not runner.is_held_for_approval(target)


async def test_a_restart_keeps_the_conversation_of_a_pending_approval_busy(tmp_path: Path) -> None:
    """The bot that takes over holds the conversation of every approval still pending, before it replays anything."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        restarted = _bot(tmp_path)
        assert not restarted._response_runner.is_held_for_approval(_target())
        await restarted._reply_runtime.start()
        assert restarted._response_runner.is_held_for_approval(_target())


async def test_deleting_the_message_of_a_paused_reply_cancels_its_approval_and_removes_the_reply(
    tmp_path: Path,
) -> None:
    """The deletion cancels the approval as a Stop would: its cards expire, the reply goes, and the conversation frees."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        paused = await _reply(bot)
        assert paused.event_id is not None
        room = nio.MatrixRoom(_target().room_id, bot.matrix_id.full_id)
        redaction = nio.Event.parse_event(
            {
                "event_id": "$redaction",
                "type": "m.room.redaction",
                "sender": "@user:localhost",
                "origin_server_ts": 2,
                "redacts": "$event",
                "content": {},
            },
        )
        assert isinstance(redaction, nio.RedactionEvent)
        await bot.journal_principal().admit(
            _inbound_event(room.room_id, redaction, EventKind.REDACTION, EventClass.ACTIONABLE),
            _projected_event(room.room_id, redaction, EventKind.REDACTION, self_sender=room.own_user_id),
        )
        with _journal_wakes(bot) as wakes:
            await bot._on_redaction(room, redaction)
        assert wakes == [("$event",)]
        await _run_approval_wakes(bot, wakes)

        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert await bot._journal_store.principal("router@shared").pending_approval_room_ids() == ()
        assert not bot._response_runner.is_held_for_approval(_target())

        async def redacted() -> rl.Reply:
            while (reply := await _reply(bot)).redaction_pending:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            return reply

        gone = await asyncio.wait_for(redacted(), timeout=5)
        assert gone.state is rl.ReplyState.GONE
        assert paused.event_id in [call.args[1] for call in bot.client.room_redact.await_args_list]
        # No note is written over a reply that goes.
        assert "Response cancelled by user" not in _sent_bodies(bot)[-1]


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
        assert await bot._journal_store.principal("router@shared").pending_approval_room_ids() == ()

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.CANCELLED
        assert not reply.unapplied_stop
        assert reply.approval_id is None
        assert await _span_kinds(bot) == [(rl.SpanKind.TURN, rl.SpanOutcome.CANCELLED)]
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert not await bot._reply_runtime.store.is_pending("$event")
        assert _sent_bodies(bot)[-1] == "Reading document\n\n**[Response cancelled by user]**"


@pytest.mark.parametrize("answers", [True, False])
async def test_a_turn_approved_in_place_settles_through_its_approval(tmp_path: Path, answers: bool) -> None:
    """A run whose CLI call was approved while it waited ends its reply once, with the answer or the error note."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)

        async def approved_in_place(*_args: object, **_kwargs: object) -> str:
            context = get_tool_runtime_context()
            assert context is not None
            assert context.cli_approval_handler is not None
            cli_call = {"kind": "agent_cli", "call_id": "inner", "parent_bash_call_id": "bash"}
            paused = _paused()
            requirement = RunRequirement(tool_execution=paused.tools[0])
            await context.cli_approval_handler(replace(paused, cli_call=cli_call, requirements=(requirement,)))
            if not answers:
                msg = "model down"
                raise RuntimeError(msg)
            return "Done reading."

        with patch_response_runner_module(
            ai_response=AsyncMock(side_effect=approved_in_place),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            if answers:
                await runner.generate_response(_plain_request(_target()))
            else:
                with pytest.raises(RuntimeError, match="model down"):
                    await runner.generate_response(_plain_request(_target()))
        if await bot.journal_principal().approval_continuation_for_source("$event") is not None:
            # A failure settles from the source worker the fence woke.
            await _run_approval_wakes(bot, [("$event",)])

        reply = await _reply(bot)
        assert reply.state is (rl.ReplyState.COMPLETED if answers else rl.ReplyState.FAILED)
        assert reply.approval_id is None
        assert await bot.journal_principal().approval_continuation_for_source("$event") is None
        assert not await bot._reply_runtime.store.is_pending("$event")
        if answers:
            assert _sent_bodies(bot)[-1] == "Done reading."
        else:
            assert "model down" in _sent_bodies(bot)[-1]


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
        # The Stop keeps what the reply showed and adds its note, as on any reply.
        assert _sent_bodies(bot)[-1] == "Reading document\n\n**[Response cancelled by user]**"


async def test_a_shutdown_during_an_approval_resume_leaves_it_for_replay(tmp_path: Path) -> None:
    """A process stop leaves the resume span as a crash would; recovery hands it back to replay, which may claim it."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        resuming = asyncio.Event()

        async def resume_until_cancelled(*_args: object, **_kwargs: object) -> CompletedApprovalRun:
            resuming.set()
            await asyncio.Event().wait()
            return CompletedApprovalRun("Never answered.", {})

        response = asyncio.create_task(_respond(bot, resume=AsyncMock(side_effect=resume_until_cancelled)))
        await asyncio.wait_for(resuming.wait(), timeout=5)
        request_task_cancel(response, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(response, timeout=5)

        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, None),
        ]
        failing = await bot.journal_principal().approval_continuation_for_source("$event")
        assert failing is not None
        assert failing.state == "failing"
        runner = unwrap_extracted_collaborator(bot._response_runner)
        await runner._recover_failing_approval(failing, target=_target())

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.ACTIVE
        assert reply.current_span_id is None
        assert await _span_kinds(bot) == [
            (rl.SpanKind.TURN, rl.SpanOutcome.PAUSED),
            (rl.SpanKind.APPROVAL_RESUME, rl.SpanOutcome.RELEASED),
        ]
        assert await bot._reply_runtime.store.is_pending("$event")


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


async def test_a_resume_keeps_what_earlier_spans_showed_in_its_final_answer(tmp_path: Path) -> None:
    """A replay that paused continues below the stopped attempt it showed; its progress and answer keep that above it."""
    earlier = Segment(kind="answer", text="Earlier partial", span_id="earlier-span")
    resuming = False

    async def streaming(*_args: object, **_kwargs: object) -> bool:
        # The paused answer was blocking; the resume streams its progress.
        return resuming

    async def resume_with_progress(*_args: object, progress: object = None, **_kwargs: object) -> CompletedApprovalRun:
        assert progress is not None
        await progress(StructuredStreamChunk(content="Reading document, then more"))  # type: ignore[operator]
        return CompletedApprovalRun("Approved answer.", {})

    async with _approval_bot(tmp_path, requires_human=False) as bot:
        runtime_type = type(bot._reply_runtime)
        claim = runtime_type.claim_approval_resume
        runner = unwrap_extracted_collaborator(bot._response_runner)

        async def claim_below_an_earlier_attempt(runtime: object, *args: object, **kwargs: object) -> object:
            nonlocal resuming
            resuming = True

            def below_earlier(current: rl.Reply) -> rl.Transition:
                shown = decode_presentation(current.presentation)
                prefixed = encode_presentation(
                    replace(shown, segments=(earlier, note_segment(NoteKind.RESTART), *shown.segments)),
                )
                return rl.Transition(
                    outcome=rl.Outcome.APPLIED,
                    reply=replace(current, presentation=prefixed, possibly_shown=prefixed),
                )

            await bot._reply_runtime.store.replies.update((await _reply(bot)).reply_id, below_earlier)
            return await claim(runtime, *args, **kwargs)  # type: ignore[arg-type]

        recorded: list[str] = []
        write_ahead = ReplyStore.write_ahead

        async def record_write_ahead(store: ReplyStore, **kwargs: object) -> object:
            recorded.append(str(kwargs["shown"]))
            return await write_ahead(store, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(runtime_type, "claim_approval_resume", claim_below_an_earlier_attempt),
            patch_response_runner_module(
                ai_response=AsyncMock(side_effect=ResponsePausedForApproval(_paused())),
                should_use_streaming=AsyncMock(side_effect=streaming),
                typing_indicator=_noop_typing,
            ),
            patch.object(type(runner), "_continue_entity_call", AsyncMock(side_effect=resume_with_progress)),
            patch.object(ReplyStore, "write_ahead", record_write_ahead),
        ):
            await runner.generate_response(_plain_request(_target()))

        progress, final = _sent_bodies(bot)[-2:]
        assert progress.startswith("Earlier partial")
        assert "Reading document, then more" in progress
        # What each progress edit records holds the earlier work once, as Matrix shows it.
        assert recorded
        assert all(render_body(decode_presentation(shown))[0].count("Earlier partial") == 1 for shown in recorded)
        assert final.startswith("Earlier partial")
        assert final.endswith("Approved answer.")
        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.COMPLETED
        # What recovery restores for after-response hooks is what the room shows.
        frozen = await bot.journal_principal().load_matrix_delivery(delivery_id="$event", stage=DeliveryStage.FINAL)
        assert frozen is not None
        assert (await runner._approval_outcome_from_delivery(frozen)).final_visible_body == final


async def test_each_resume_continues_the_answer_its_reply_paused_with(tmp_path: Path) -> None:
    """A resume continues the source answer and trace its reply's records keep, not what Matrix showed."""
    trace = ToolTraceEntry(type="tool_call_started", tool_name="read_document", tool_call_id="call-run-1")
    first = replace(
        _paused(text="Reading document"),
        acknowledged_response_text="Reading the rendered document",
        tool_trace=(trace,),
    )
    continue_run = AsyncMock(
        side_effect=[
            _paused(run_id="run-2", text="Reading the second document"),
            CompletedApprovalRun("Both read.", {}),
        ],
    )
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        runner = unwrap_extracted_collaborator(bot._response_runner)
        with (
            patch_response_runner_module(
                ai_response=AsyncMock(side_effect=ResponsePausedForApproval(first)),
                should_use_streaming=AsyncMock(return_value=False),
                typing_indicator=_noop_typing,
            ),
            patch.object(type(runner._approval_execution), "continue_run", continue_run),
        ):
            await runner.generate_response(_plain_request(_target()))

        assert (await _reply(bot)).state is rl.ReplyState.COMPLETED
        assert [call.kwargs["paused_answer"] for call in continue_run.await_args_list] == [
            PausedAnswer(text="Reading document", tool_trace=(trace,)),
            PausedAnswer(text="Reading the second document"),
        ]
        assert _sent_bodies(bot) == [
            "Thinking...",
            "Reading the rendered document",
            "Reading the second document",
            "Both read.",
        ]


async def test_an_edit_of_a_paused_reply_runs_nothing(tmp_path: Path) -> None:
    """Without the edit regenerator's Stop, the approval holds its reply, so a regeneration claims nothing."""
    async with _approval_bot(tmp_path, requires_human=True) as bot:
        await _respond(bot)
        paused = await bot.journal_principal().approval_continuation_for_source("$event")
        assert paused is not None
        await _admit_edit(bot)
        runner = unwrap_extracted_collaborator(bot._response_runner)
        model = AsyncMock(return_value="Unreachable.")
        claimed = AsyncMock()
        sends = len(_sent_bodies(bot))
        with patch_response_runner_module(
            ai_response=model,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ):
            regeneration = replace(_regeneration(answer_event_id="$sent1"), on_reply_claimed=claimed)
            assert await runner.generate_response(regeneration) is None

        model.assert_not_awaited()
        claimed.assert_not_awaited()
        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.PAUSED
        assert reply.approval_id == paused.approval_id
        assert await bot.journal_principal().approval_continuation_for_source("$event") == paused
        assert await _span_kinds(bot) == [(rl.SpanKind.TURN, rl.SpanOutcome.PAUSED)]
        assert len(_sent_bodies(bot)) == sends


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
        # The failure note goes below what the reply showed.
        assert _sent_bodies(bot)[-1] == "Reading document\n\nCard publication failed"


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
        # The Stop commits on the reply before this settlement writes its note.
        assert await gateway.stop_reply("$sent1", 5, room_id=_target().room_id, may_wait=False)

        assert await runner._approval_responses.settle_failure(failing, "Card publication failed")

        reply = await _reply(bot)
        assert reply.state is rl.ReplyState.CANCELLED
        # The Stop keeps what the reply showed and adds its note, as on any reply.
        assert _sent_bodies(bot)[-1] == "Reading document\n\n**[Response cancelled by user]**"


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
