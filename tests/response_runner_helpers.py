"""Shared bot/request scaffolding for the focused ResponseRunner test modules."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.response_attempt_helpers import install_direct_response_admission

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import EventJournalStore
    from mindroom.final_delivery import FinalDeliveryOutcome, _VisibleBodyState
    from mindroom.history.turn_recorder import TurnRecorder
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
        tmp_path,
        config,
        runtime_paths_for(config),
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


@asynccontextmanager
async def _noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


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
                assistant_text=text,
                completed_tools=(),
            )
        yield text

    return stream


def stream_transport(
    event_id: str | None = "$stream",
    body: str = "streamed body",
    *,
    visible_body_state: _VisibleBodyState = "visible_body",
    failure_reason: str | None = None,
) -> StreamTransportOutcome:
    """Return what a stream that ran to its end left in Matrix: its last event and the body it showed."""
    return StreamTransportOutcome(
        last_physical_stream_event_id=event_id,
        rendered_body=body,
        visible_body_state=visible_body_state,
        failure_reason=failure_reason,
    )


def delivered_turn(delivery: FinalDeliveryOutcome) -> AsyncMock:
    """Return a ``ResponseRunner._deliver_turn`` that records its turn completed, as the turn driver does.

    It delivers ``delivery``; its await arguments name the prepared turn it was given.
    """

    async def deliver(_turn: object, *, recorder: TurnRecorder, **_kwargs: object) -> FinalDeliveryOutcome:
        recorder.record_completed(
            run_id=None,
            run_metadata=None,
            assistant_text=delivery.final_visible_body or "",
            completed_tools=(),
        )
        return delivery

    return AsyncMock(side_effect=deliver)
