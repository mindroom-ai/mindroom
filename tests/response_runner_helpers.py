"""Shared bot/request scaffolding for the focused ResponseRunner test modules."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from mindroom.cancellation import classify_cancel_source
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.final_delivery import Cancelled, Completed, FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history.interrupted_replay import is_interrupted
from mindroom.history.request_messages import replayable_messages
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.reply_lifecycle import SpanOutcome
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import _handled_response_event_id, make_test_agent_bot
from tests.bot_helpers import _noop_typing_indicator as _noop_typing  # noqa: F401
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    message_origin,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import EventJournalStore
    from mindroom.final_delivery import _VisibleBodyState
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.response_runner import ResponseRunner
    from mindroom.response_turn import TurnSinks


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={"general": AgentConfig(display_name="General", rooms=["!room:localhost"])},
                teams={},
                models={"default": ModelConfig(provider="openai", id="test-model")},
                # A finished response would otherwise persist memory through a real embedding request.
                memory={"backend": "none"},
            ),
        ),
        test_runtime_paths(tmp_path),
    )


def _bot(tmp_path: Path, *, journal_store: EventJournalStore | None = None) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = make_test_agent_bot(
        agent_user,
        config,
        rooms=["!room:localhost"],
        journal_store=journal_store,
    )
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_journal_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


def _target(*, thread_id: str | None = None, reply_to_event_id: str = "$event") -> MessageTarget:
    return MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        room_mode=thread_id is None,
    )


def _envelope(target: MessageTarget, *, source_event_id: str = "$event") -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=source_event_id,
        target=target,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
    )


def _plain_request(target: MessageTarget, *, source_event_id: str = "$event") -> ResponseRequest:
    return ResponseRequest(
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id=source_event_id),
        sources=ResponseSources(
            pending_event_ids=(source_event_id,),
            logical_source_event_ids=(source_event_id,),
        ),
    )


def streamed_answer(answer: Callable[..., Awaitable[str]]) -> Callable[..., AsyncIterator[str]]:
    """Return a turn stream (``stream_agent_response`` or ``team_response_stream``) that yields ``answer``'s text.

    ``answer`` receives the stream's arguments; like the turn driver, the stream records the turn completed before it
    yields the text that ends it.
    """

    async def stream(*args: object, sinks: TurnSinks | None = None, **kwargs: object) -> AsyncIterator[str]:
        text = await answer(*args, sinks=sinks, **kwargs)
        if sinks is not None and sinks.turn_recorder is not None:
            sinks.turn_recorder.record_completed(
                run_id=None,
                run_metadata=None,
                completed_tools=(),
            )
        yield text

    return stream


def stream_transport(
    event_id: str | None = "$stream",
    body: str = "streamed body",
    *,
    visible_body_state: _VisibleBodyState = "visible_body",
    failed: bool = False,
) -> StreamTransportOutcome:
    """Return what a stream that ran to its end left in Matrix: its last event and the body it showed."""
    return StreamTransportOutcome(
        last_physical_stream_event_id=event_id,
        rendered_body=body,
        visible_body_state=visible_body_state,
        failed=failed,
    )


def delivered_turn(delivery: FinalDeliveryOutcome) -> AsyncMock:
    """Return a ``ResponseRunner._deliver_turn`` that records its turn completed, as the turn driver does.

    It delivers ``delivery``; its await arguments name the prepared turn it was given.
    """

    async def deliver(_turn: object, *, recorder: TurnRecorder, **_kwargs: object) -> FinalDeliveryOutcome:
        recorder.record_completed(
            run_id=None,
            run_metadata=None,
            completed_tools=(),
        )
        return delivery

    return AsyncMock(side_effect=deliver)


def attempt_in_place(*, cancel_after: asyncio.Event | None = None) -> AsyncMock:
    """Return a ``LockedTurns._run_cancellable_response`` that runs the turn's attempt with no Stop button.

    Like the attempt runner, it returns a cancellation of the attempt as its ending.
    Given ``cancel_after``, the attempt runs as its own task, which it cancels once that event is set, as a Stop does.
    """

    async def run(**kwargs: object) -> FinalDeliveryOutcome | Cancelled:
        response_function = cast(
            "Callable[[str | None], Coroutine[Any, Any, FinalDeliveryOutcome]]",
            kwargs["response_function"],
        )
        coroutine = response_function(None)
        attempt: Awaitable[FinalDeliveryOutcome] = coroutine
        if cancel_after is not None:
            task = asyncio.create_task(coroutine)
            await cancel_after.wait()
            task.cancel()
            attempt = task
        try:
            return await attempt
        except asyncio.CancelledError as cancelled:
            return Cancelled(classify_cancel_source(cancelled))

    return AsyncMock(side_effect=run)


def completed_outcome(event_id: str = "$response", body: str = "ok") -> FinalDeliveryOutcome:
    """Return a completed delivery that sent ``body`` as ``event_id``."""
    return FinalDeliveryOutcome(
        ending=Completed(),
        event_id=event_id,
        is_visible_response=True,
        final_visible_body=body,
        delivery_kind="sent",
    )


async def recorded_reply_event(bot: AgentBot, source_event_id: str) -> str | None:
    """Return the event the reply answering ``source_event_id`` shows, from its durable record."""
    reply = await bot._reply_runtime.store.replies.for_sources((source_event_id,))
    assert reply is not None
    return reply.event_id


def answering_event(effects: AsyncMock) -> str | None:
    """Return the event that answers the turn, from the one ending its post-response effects received."""
    [call] = effects.await_args_list
    return _handled_response_event_id(call.args[0])


async def turn_left_to_replay(coordinator: ResponseRunner, source_event_id: str = "$user_msg") -> bool:
    """Return whether the turn's span ended without its interruption note, which leaves its sources for a replay."""
    reply = await coordinator.deps.replies.store.replies.for_sources((source_event_id,))
    assert reply is not None
    spans = await coordinator.deps.replies.store.replies.spans(reply.reply_id)
    return spans[-1].outcome not in {SpanOutcome.FAILED, SpanOutcome.CANCELLED}


def assert_interrupted_messages(run: AnyRunRecord, *, replayed: Sequence[tuple[str, object]]) -> None:
    """Assert ``run`` is its interrupted turn's own record: the user's ``Hello`` from ``$user_msg``, then ``replayed``.

    ``replayed`` holds the role and content of each message history replays of the run after that prompt.
    """
    assert is_interrupted(run)
    prompt, *rest = replayable_messages([run])
    assert prompt.role == "user"
    assert 'event_id="$user_msg"' in cast("str", prompt.content)
    assert "Hello" in cast("str", prompt.content)
    shown = [(message.role, message.content) for message in rest]
    assert shown == list(replayed), shown
