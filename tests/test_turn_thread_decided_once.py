"""A turn's thread is decided once, and dispatch context agrees with the target it answers in."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.coalescing.batch import requester_coalescing_key
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.users import AgentMatrixUser
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    replace_turn_deps,
    replace_turn_policy_deps,
    runtime_paths_for,
    sync_bot_runtime_state,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.turn_dispatch_helpers import make_test_turn

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot

ROOM_ID = "!room:localhost"


def _bot(tmp_path: Path, *, thread_mode: str) -> AgentBot:
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=[ROOM_ID], thread_mode=thread_mode)},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    with_current_room_member_access(config)
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="code",
            password=TEST_PASSWORD,
            display_name="Code",
            user_id="@mindroom_code:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    install_runtime_journal_support(bot)
    sync_bot_runtime_state(bot)
    wrap_extracted_collaborators(bot)
    replace_turn_policy_deps(
        bot,
        resolver=bot._conversation_resolver,
        response_runner=bot._response_runner,
        delivery_gateway=bot._delivery_gateway,
    )
    replace_turn_deps(
        bot,
        resolver=bot._conversation_resolver,
        normalizer=bot._inbound_turn_normalizer,
        turn_policy=bot._turn_policy,
        response_runner=bot._response_runner,
        delivery_gateway=bot._delivery_gateway,
        state_writer=bot._conversation_state_writer,
    )
    return bot


@pytest.mark.asyncio
async def test_a_threaded_turn_replayed_after_the_room_switched_to_room_mode_is_answered_and_planned_at_room_level(
    tmp_path: Path,
) -> None:
    """Context follows the target: neither may name the persisted thread while the other answers in the room."""
    bot = _bot(tmp_path, thread_mode="room")
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@mindroom_code:localhost")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$threaded-event",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": "@mindroom_code:localhost hello",
                "m.mentions": {"user_ids": ["@mindroom_code:localhost"]},
                "m.relates_to": {"event_id": "$thread-root", "rel_type": "m.thread"},
            },
        },
    )

    prepared = await bot._turn_dispatcher._prepare_dispatch(
        make_test_turn(
            room,
            event,
            facts=bot._ingress_validator.classify(event),
            handled_turn=TurnRecord.create([event.event_id]),
            # Decided by ingress while the room was still in thread mode.
            coalescing_key=requester_coalescing_key(ROOM_ID, "$thread-root", "@user:localhost"),
        ),
    )

    assert prepared is not None
    assert prepared.target.resolved_thread_id is None
    assert (prepared.context.is_thread, prepared.context.thread_id) == (False, None)
