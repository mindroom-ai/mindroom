"""Tests for agent self-managed room membership.

With the new self-managing agent pattern, agents handle their own room
memberships. This test module verifies that behavior.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks.matrix_admin import build_hook_matrix_admin
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    save_invited_rooms,
)
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.access_schema_support import with_responder_access
from tests.bot_helpers import FencedRoomRecorder, make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    install_send_response_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nio.responses import Response

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths


def _invited_rooms_path(config: Config, agent_name: str) -> Path:
    return invited_rooms_path(runtime_paths_for(config).storage_root, agent_name)


async def _handle_invite(bot: AgentBot, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
    room.inviter = event.sender
    bot.client.invited_rooms = {room.room_id: room}
    await bot._room_lifecycle.handle_invite(room, event.sender)


def _classic_invite_membership_response(
    room_id: str,
    *,
    next_batch: str,
) -> nio.SyncResponse:
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": next_batch,
            "rooms": {
                "invite": {room_id: {"invite_state": {"events": []}}},
                "join": {},
                "leave": {},
            },
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


async def _apply_classic_invite_membership(
    bot: AgentBot,
    room_id: str,
    *,
    next_batch: str,
) -> None:
    response = _classic_invite_membership_response(room_id, next_batch=next_batch)
    await bot._apply_own_room_membership_from_sync(response)


def _router_user() -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_config(tmp_path: Path) -> Config:
    """Create a mock config with agents and teams."""
    return bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["room1", "room2"],
                ),
                "agent2": AgentConfig(
                    display_name="Agent 2",
                    role="Another test agent",
                    rooms=["room1"],
                ),
            },
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Test team",
                    agents=["agent1", "agent2"],
                    rooms=["room2"],
                ),
            },
        ),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_agent_joins_configured_rooms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that agents join their configured rooms on startup."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Create the agent bot with configured rooms
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )
    install_runtime_journal_support(bot)

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track which rooms were joined
    joined_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> RoomJoinOutcome:
        joined_rooms.append(room_id)
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _conversation_reader: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))

    # Test that the bot joins its configured rooms
    await bot.join_configured_rooms()

    # Verify the bot joined both configured rooms
    assert len(joined_rooms) == 2
    assert "!room1:localhost" in joined_rooms
    assert "!room2:localhost" in joined_rooms


@pytest.mark.asyncio
async def test_agent_skips_rejoining_rooms_it_already_has(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Agents should skip redundant joins for rooms they are already in."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )
    install_runtime_journal_support(bot)

    mock_client = AsyncMock()
    mock_client.rooms = {"!room1:localhost": MagicMock()}
    bot.client = mock_client

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=["!room1:localhost"]))
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))

    await bot.join_configured_rooms()

    join_room.assert_awaited_once_with(mock_client, "!room2:localhost")


@pytest.mark.asyncio
async def test_join_configured_rooms_retries_when_membership_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unreadable joined-room inventory must not block idempotent join recovery."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=None))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    await bot.join_configured_rooms()

    assert {call.args[1] for call in join_room.await_args_list} == {
        "!room1:localhost",
        "!room2:localhost",
    }
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room1:localhost")
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room2:localhost")


@pytest.mark.asyncio
async def test_stale_client_room_after_leave_cannot_reopen_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only authoritative server membership can reopen a locally departed room."""
    room_id = "!room1:localhost"
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=[room_id],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {room_id: MagicMock()}
    join_room = AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    await bot._fence_left_room(room_id)
    await bot.join_configured_rooms()

    join_room.assert_awaited_once_with(bot.client, room_id)
    assert bot._local_departures_awaiting_sync == {room_id}
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


@pytest.mark.asyncio
async def test_agent_does_not_rejoin_absent_accepted_room_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accepted-room storage preserves current membership but cannot grant a new join."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=True,
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    invited_path = _invited_rooms_path(config, "agent1")
    invited_path.parent.mkdir(parents=True, exist_ok=True)
    invited_path.write_text('[\n  "!invited-room:localhost"\n]\n', encoding="utf-8")

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)

    mock_client = AsyncMock()
    bot.client = mock_client

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))

    await bot.join_configured_rooms()

    join_room.assert_not_awaited()
    assert load_invited_rooms(invited_path) == {"!invited-room:localhost"}


@pytest.mark.asyncio
async def test_router_accepts_agent_invite_and_restores_setup_while_joined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Router should persist an accepted invite and restore setup while still joined."""
    config = bind_runtime_paths(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1", role="Test agent")},
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    fenced_during_join: list[bool] = []

    async def join_room_while_sync_is_live(_client: object, room_id: str) -> RoomJoinOutcome:
        fenced_during_join.append(bot._room_lifecycle.decrypt_notice_is_fenced(room_id))
        return RoomJoinOutcome.JOINED

    join_room = AsyncMock(side_effect=join_room_while_sync_is_live)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    welcome_message = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", welcome_message)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@mindroom_agent1:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    assert fenced_during_join == [True]
    welcome_message.assert_awaited_once_with("!router-invited:localhost", "@mindroom_agent1:localhost")
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!router-invited:localhost")
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!router-invited:localhost"\n]\n'
    )

    restarted_bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(restarted_bot)
    restarted_bot.client = AsyncMock()
    restarted_bot.client.rooms = {}
    join_room.reset_mock()
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!router-invited:localhost"]),
    )
    restored_setup = AsyncMock()
    monkeypatch.setattr(restarted_bot._room_lifecycle, "_on_configured_room_joined", restored_setup)

    await restarted_bot.join_configured_rooms()

    join_room.assert_not_awaited()
    restored_setup.assert_awaited_once_with("!router-invited:localhost")


@pytest.mark.asyncio
async def test_live_invite_forbidden_join_consumes_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed join consumes its live invite and creates no durable owner."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.rooms = {}
    bot.client.join = AsyncMock(return_value=nio.JoinError("forbidden", "M_FORBIDDEN"))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )

    await _handle_invite(
        bot,
        MagicMock(room_id="!failed:localhost", canonical_alias=None),
        MagicMock(sender="@owner:localhost"),
    )

    assert "!failed:localhost" not in bot.client.invited_rooms
    assert bot._room_lifecycle.invited_rooms == set()
    assert not bot._room_lifecycle.decrypt_notice_is_fenced("!failed:localhost")


@pytest.mark.asyncio
async def test_terminal_invite_join_failure_does_not_abort_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A permanently unjoinable room must not wedge later sync callbacks."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.join = AsyncMock(return_value=nio.JoinError("bad state", "M_BAD_STATE"))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)

    room = nio.MatrixInvitedRoom("!invalid-state:localhost", bot.agent_user.user_id)
    room.inviter = event.sender
    bot.client.invited_rooms = {room.room_id: room}
    await bot._on_invite_before_sync_certification(room, event)
    await _apply_classic_invite_membership(
        bot,
        room.room_id,
        next_batch="s_invalid_state",
    )
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    bot.client.join.assert_awaited_once_with("!invalid-state:localhost")
    assert await bot._journal_dispatcher.store.pending() == ()
    assert not bot._room_lifecycle.decrypt_notice_is_fenced("!invalid-state:localhost")


@pytest.mark.asyncio
async def test_absent_invite_has_no_recovery_work(
    tmp_path: Path,
) -> None:
    """A room missing from Matrix's invite cache cannot authorize or trigger work."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.invited_rooms = {}
    bot.client.join = AsyncMock(return_value=nio.JoinError("not invited", "M_FORBIDDEN"))
    await bot._room_lifecycle.reconcile_invites()

    bot.client.join.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_sync_invite_is_current_membership_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invite is current membership work during initial sync."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    welcome_message = AsyncMock()
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", welcome_message)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)
    room = nio.MatrixInvitedRoom("!invited:localhost", bot.agent_user.user_id)
    room.inviter = event.sender
    bot.client.invited_rooms = {room.room_id: room}

    await bot._on_invite_before_sync_certification(room, event)
    await _apply_classic_invite_membership(
        bot,
        room.room_id,
        next_batch="s_initial_invite",
    )
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_awaited_once_with(bot.client, room.room_id)
    welcome_message.assert_awaited_once_with(room.room_id, event.sender)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert await bot._journal_dispatcher.store.pending() == ()


@pytest.mark.parametrize("sync_mode", ["classic", "sliding"])
@pytest.mark.asyncio
async def test_sync_membership_applies_before_invite_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_mode: str,
) -> None:
    """One response's invite snapshot cannot revoke acceptance started from that response."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)
    room = nio.MatrixInvitedRoom("!ordered-invite:localhost", bot.agent_user.user_id)
    room.inviter = event.sender
    bot.client.invited_rooms = {room.room_id: room}

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    if sync_mode == "classic":
        response = nio.SyncResponse.from_dict(
            {
                "next_batch": "s_after_invite",
                "rooms": {
                    "invite": {room.room_id: {"invite_state": {"events": []}}},
                    "join": {},
                    "leave": {},
                },
            },
        )
        assert isinstance(response, nio.SyncResponse)
        await bot._apply_own_room_membership_from_sync(response)
    else:
        response = nio.SlidingSyncResponse.from_dict(
            {
                "pos": "s_after_invite",
                "rooms": {
                    room.room_id: {
                        "membership": "invite",
                        "timeline": [],
                    },
                },
            },
        )
        assert isinstance(response, nio.SlidingSyncResponse)
        await bot._apply_own_room_membership_from_sliding_sync(response)

    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    join_room.assert_awaited_once_with(bot.client, room.room_id)
    assert load_invited_rooms(_invited_rooms_path(config, ROUTER_AGENT_NAME)) == {room.room_id}


@pytest.mark.asyncio
async def test_config_reconciliation_waits_for_current_sync_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Config reconciliation cannot join from nio's half-applied sync cache."""
    inviter_id = "@owner:localhost"
    config = bind_runtime_paths(
        Config(
            administrators=[inviter_id],
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot._reply_membership_sync = AgentReplyMembershipSync(bot._runtime_view.agent_reply_memberships)
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixInvitedRoom("!config-race:localhost", bot.agent_user.user_id)
    room.inviter = inviter_id
    bot.client.invited_rooms = {room.room_id: room}
    response = _classic_invite_membership_response(room.room_id, next_batch="s_config_race")
    bot._before_sync_response_admission(response)
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", delayed_join)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    reconciliation = asyncio.create_task(bot.reconcile_live_invites())
    try:
        await asyncio.sleep(0)
        assert reconciliation.done()
        assert not join_started.is_set()
        await reconciliation

        event = nio.InviteEvent.parse_event(
            {
                "type": "m.room.member",
                "sender": inviter_id,
                "state_key": bot.agent_user.user_id,
                "content": {"membership": "invite"},
            },
        )
        assert isinstance(event, nio.InviteMemberEvent)
        await bot._on_invite_before_sync_certification(room, event)
        await bot._on_sync_response(response)
        await asyncio.wait_for(join_started.wait(), timeout=1)
    finally:
        release_join.set()
        if not reconciliation.done():
            reconciliation.cancel()
            with suppress(asyncio.CancelledError):
                await reconciliation
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert load_invited_rooms(_invited_rooms_path(config, ROUTER_AGENT_NAME)) == {room.room_id}


@pytest.mark.asyncio
async def test_sync_response_does_not_wait_for_invite_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sync-side invite reconciliation cannot wait for room network work."""
    inviter_id = "@owner:localhost"
    config = bind_runtime_paths(
        Config(
            administrators=[inviter_id],
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot._reply_membership_sync = AgentReplyMembershipSync(bot._runtime_view.agent_reply_memberships)
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixInvitedRoom("!nonblocking-sync:localhost", bot.agent_user.user_id)
    room.inviter = inviter_id
    bot.client.invited_rooms = {room.room_id: room}
    response = _classic_invite_membership_response(room.room_id, next_batch="s_nonblocking")
    bot._before_sync_response_admission(response)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": inviter_id,
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)
    await bot._on_invite_before_sync_certification(room, event)
    bot._router_reply_membership_sync.invalidate(config, reason="test_refresh")
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", delayed_join)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    sync_response = asyncio.create_task(bot._on_sync_response(response))
    await asyncio.wait_for(join_started.wait(), timeout=1)
    try:
        await asyncio.wait_for(asyncio.shield(sync_response), timeout=1)
    finally:
        release_join.set()
        await sync_response
        await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)


@pytest.mark.asyncio
async def test_invite_sync_callback_runs_live_join_in_background(tmp_path: Path) -> None:
    """Live invite admission must not hold the sync loop across network work."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_invite(_room: nio.MatrixRoom, _sender: str) -> None:
        join_started.set()
        await release_join.wait()

    bot._room_lifecycle.handle_invite = delayed_invite
    room = nio.MatrixRoom("!background-invite:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.matrix_id.full_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteEvent)

    callback_task = asyncio.create_task(
        bot._on_invite_before_sync_certification(room, event),
    )
    try:
        await callback_task
        assert not join_started.is_set()
        await _apply_classic_invite_membership(
            bot,
            room.room_id,
            next_batch="s_background_invite",
        )
        await asyncio.wait_for(join_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert callback_task.done()
        assert await bot._journal_dispatcher.store.pending() == ()
    finally:
        release_join.set()
        await callback_task

    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    assert await bot._journal_dispatcher.store.pending() == ()


@pytest.mark.asyncio
async def test_invite_sync_callback_ignores_non_membership_state(
    tmp_path: Path,
) -> None:
    """Invite metadata must not be mistaken for authenticated inviter evidence."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot._room_lifecycle.handle_invite = AsyncMock()
    room = nio.MatrixRoom("!failed-pending-save:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.name",
            "sender": "@metadata-author:localhost",
            "state_key": "",
            "content": {"name": "Project"},
        },
    )
    assert isinstance(event, nio.InviteNameEvent)

    await bot._on_invite_before_sync_certification(room, event)

    bot._room_lifecycle.handle_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_persistence_failure_compensates_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed accepted-room saves trigger one leave without durable retry work."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await _handle_invite(
        bot,
        MagicMock(room_id="!failed-save:localhost", canonical_alias=None),
        MagicMock(sender="@owner:localhost"),
    )

    leave_room.assert_awaited_once_with(bot.client, "!failed-save:localhost")


@pytest.mark.asyncio
async def test_router_invite_preserves_room_created_after_lifecycle_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A later invite must not overwrite a hook-created room missing from the lifecycle cache."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    creator_client = AsyncMock(spec=nio.AsyncClient)
    creator_client.homeserver = "http://localhost:8008"
    creator_client.user_id = bot.agent_user.user_id
    with monkeypatch.context() as patch_context:
        create_room = AsyncMock(return_value="!hook-created:localhost")
        patch_context.setattr("mindroom.hooks.matrix_admin.create_room", create_room)
        admin = build_hook_matrix_admin(
            creator_client,
            runtime_paths_for(config),
            config=config,
        )
        await admin.create_room(name="Hook-created room")

    assert bot._room_lifecycle.invited_rooms == set()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())
    room = MagicMock(room_id="!later-invite:localhost", canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")

    await _handle_invite(bot, room, event)

    expected_rooms = {"!hook-created:localhost", "!later-invite:localhost"}
    assert bot._room_lifecycle.invited_rooms == expected_rooms
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!hook-created:localhost",\n  "!later-invite:localhost"\n]\n'
    )


@pytest.mark.asyncio
async def test_router_cleanup_preserves_room_created_after_lifecycle_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup must refresh rooms persisted by hooks after lifecycle construction."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    creator_client = AsyncMock(spec=nio.AsyncClient)
    creator_client.homeserver = "http://localhost:8008"
    creator_client.user_id = bot.agent_user.user_id
    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            "mindroom.hooks.matrix_admin.create_room",
            AsyncMock(return_value="!hook-created:localhost"),
        )
        admin = build_hook_matrix_admin(
            creator_client,
            runtime_paths_for(config),
            config=config,
        )
        await admin.create_room(name="Hook-created room")

    assert bot._room_lifecycle.invited_rooms == set()

    left_room_ids: list[str] = []

    async def record_rooms_to_leave(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        on_room_left: Callable[[str], Awaitable[None]],
    ) -> list[str]:
        del on_room_left
        left_room_ids.extend(room_ids)
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!hook-created:localhost", "!stale:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_rooms", record_rooms_to_leave)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == set()
    assert left_room_ids == ["!stale:localhost"]


@pytest.mark.asyncio
async def test_router_cleanup_loads_invited_rooms_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Durable refresh must not perform file I/O on the event-loop thread."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    event_loop_thread_id = threading.get_ident()
    load_thread_ids: list[int] = []

    def record_load_thread(path: Path) -> set[str]:
        load_thread_ids.append(threading.get_ident())
        return load_invited_rooms(path)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.load_invited_rooms", record_load_thread)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    assert await bot._room_lifecycle._rooms_to_leave() == []
    assert len(load_thread_ids) == 1
    assert load_thread_ids[0] != event_loop_thread_id


@pytest.mark.asyncio
async def test_router_persistence_failure_compensates_without_retry_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed acceptance save leaves once and does not retain retry state."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    attempts = 0

    def fail_first_save(path: Path, room_ids: set[str]) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return save_invited_rooms(path, room_ids)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", fail_first_save)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())

    first_room = MagicMock(room_id="!first:localhost", canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")
    await _handle_invite(bot, first_room, event)

    await _handle_invite(
        bot,
        MagicMock(room_id="!second:localhost", canonical_alias=None),
        event,
    )

    leave_room.assert_awaited_once_with(bot.client, "!first:localhost")
    assert bot._room_lifecycle.invited_rooms == {"!second:localhost"}
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!second:localhost"\n]\n'
    )


@pytest.mark.asyncio
async def test_router_deduplicates_concurrent_invite_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Duplicate invite callbacks for one room should join and welcome only once."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_join_room(_client: AsyncMock, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.JOINED

    join_room = AsyncMock(side_effect=delayed_join_room)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!router-invited:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    first_invite = asyncio.create_task(_handle_invite(bot, room, event))
    await join_started.wait()
    second_invite = asyncio.create_task(_handle_invite(bot, room, event))
    release_join.set()

    await asyncio.gather(first_invite, second_invite)

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    bot.client.room_messages.assert_awaited_once()
    send_response.assert_awaited_once()
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}


@pytest.mark.asyncio
async def test_router_departure_allows_fresh_reinvite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A room departure must rejoin even when nio keeps the old room cached."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    room_id = "!router-reinvited:localhost"
    room = MagicMock(room_id=room_id, canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await _handle_invite(bot, room, event)
    bot.client.rooms[room_id] = MagicMock()
    bot._room_lifecycle.forget_invited_room(room_id)
    await _handle_invite(bot, room, event)

    assert join_room.await_count == 2
    assert bot.client.room_messages.await_count == 2
    assert send_response.await_count == 2


def test_agent_forgets_persisted_invited_room_after_being_kicked(
    tmp_path: Path,
) -> None:
    """An ephemeral call room cannot be rejoined after its creator removes the agent."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=True,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room_id = "!agent-call:localhost"
    bot._room_lifecycle._update_invited_room(room_id, remember=True)
    bot._room_lifecycle.forget_invited_room(room_id)

    assert bot._room_lifecycle.invited_rooms == set()
    assert _invited_rooms_path(config, "agent1").read_text(encoding="utf-8") == "[]\n"


def test_agent_retries_failed_persisted_invited_room_forget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed departure save must reject work until the durable room is removed."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=True,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room_id = "!agent-call:localhost"
    assert bot._room_lifecycle._update_invited_room(room_id, remember=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)

    with pytest.raises(OSError, match="Failed to forget invited room"):
        bot._room_lifecycle.forget_invited_room(room_id)

    restarted = make_test_agent_bot(
        agent_user=bot.agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    assert restarted._room_lifecycle.invited_rooms == {room_id}

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", save_invited_rooms)
    bot._room_lifecycle.forget_invited_room(room_id)
    restarted = make_test_agent_bot(
        agent_user=bot.agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    assert restarted._room_lifecycle.invited_rooms == set()


@pytest.mark.asyncio
async def test_failed_forget_keeps_durable_owner_without_mutating_live_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed durable forget cannot silently discard accepted ownership."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    invited_path = _invited_rooms_path(config, ROUTER_AGENT_NAME)
    invited_path.parent.mkdir(parents=True, exist_ok=True)
    invited_path.write_text('[\n  "!departed:localhost"\n]\n', encoding="utf-8")
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)

    with pytest.raises(OSError, match="Failed to forget invited room"):
        bot._room_lifecycle.forget_invited_room("!departed:localhost")

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!departed:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    assert await bot._room_lifecycle._rooms_to_leave() == []
    assert bot._room_lifecycle.invited_rooms == set()


def test_nonpersisting_agent_forget_clears_in_memory_room(tmp_path: Path) -> None:
    """Disabling invite persistence must not leave stale in-memory membership."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=False,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room_id = "!old-invite:localhost"
    bot._room_lifecycle.invited_rooms = {room_id}

    bot._room_lifecycle.forget_invited_room(room_id)

    assert bot._room_lifecycle.invited_rooms == set()
    assert not _invited_rooms_path(config, "agent1").exists()


@pytest.mark.asyncio
async def test_router_failed_welcome_does_not_create_retry_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed best-effort welcome must not keep invite work alive."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!router-invited:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value=None)
    install_send_response_mock(bot, send_response)

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    await _handle_invite(bot, room, event)
    await bot._room_lifecycle.reconcile_invites()

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    bot.client.room_messages.assert_awaited_once()
    send_response.assert_awaited_once()
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}


@pytest.mark.asyncio
async def test_redelivered_invite_does_not_retry_a_failed_welcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A redelivered callback cannot revive completed live invite work."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!router-invited:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value=None)
    install_send_response_mock(bot, send_response)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.matrix_id.full_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteEvent)
    room = nio.MatrixRoom("!router-invited:localhost", bot.matrix_id.full_id)
    room.inviter = event.sender
    bot.client.invited_rooms = {room.room_id: room}
    await bot._on_invite_before_sync_certification(room, event)
    await _apply_classic_invite_membership(
        bot,
        room.room_id,
        next_batch="s_first_redelivery",
    )
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    await bot._on_invite_before_sync_certification(room, event)
    await _apply_classic_invite_membership(
        bot,
        room.room_id,
        next_batch="s_second_redelivery",
    )
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_awaited_once_with(bot.client, room.room_id)
    send_response.assert_awaited_once()
    assert bot.client.invited_rooms == {}
    assert not await bot._journal_dispatcher.store.pending()


@pytest.mark.asyncio
async def test_router_welcome_send_is_idempotent_for_concurrent_empty_room_checks(
    tmp_path: Path,
) -> None:
    """Concurrent empty-room checks should not emit duplicate welcome messages."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!empty:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await asyncio.gather(
        bot._send_welcome_message_if_empty("!empty:localhost"),
        bot._send_welcome_message_if_empty("!empty:localhost"),
        bot._send_welcome_message_if_empty("!empty:localhost"),
    )

    bot.client.room_messages.assert_awaited_once()
    send_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_welcome_send_retries_after_delivery_failure(
    tmp_path: Path,
) -> None:
    """A failed welcome delivery should not suppress a later retry."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!empty:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(side_effect=[None, "$welcome"])
    install_send_response_mock(bot, send_response)

    assert not await bot._room_lifecycle.send_welcome_message_if_empty("!empty:localhost")
    await bot._send_welcome_message_if_empty("!empty:localhost")

    assert bot.client.room_messages.await_count == 2
    assert send_response.await_count == 2


@pytest.mark.asyncio
async def test_router_welcome_lookup_failure_propagates_for_retry(tmp_path: Path) -> None:
    """A failed history lookup must not complete invite delivery."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesError.from_dict(
            {
                "errcode": "M_UNKNOWN",
                "error": "history unavailable",
            },
            "!empty:localhost",
        ),
    )

    assert not await bot._room_lifecycle.send_welcome_message_if_empty("!empty:localhost")


@pytest.mark.asyncio
async def test_router_auto_welcome_lists_ad_hoc_present_responder(tmp_path: Path) -> None:
    """Automatic ad-hoc room welcomes should advertise live responder candidates."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Writes code",
                    access=ResponderAccessConfig(current_room_members=True),
                ),
            },
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room = nio.MatrixRoom(room_id="!adhoc:localhost", own_user_id="@mindroom_router:localhost")
    room.members_synced = False
    bot.client = AsyncMock()
    bot.client.rooms = {"!adhoc:localhost": room}
    bot.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[nio.RoomMember("@mindroom_code:localhost", "Code", None)],
            room_id="!adhoc:localhost",
        ),
    )
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await bot._send_welcome_message_if_empty("!adhoc:localhost", "@alice:localhost")

    response_text = send_response.await_args.kwargs["response_text"]
    assert "\u2022 **@code**: Writes code" in response_text
    bot.client.joined_members.assert_awaited_once_with("!adhoc:localhost")


@pytest.mark.asyncio
async def test_router_startup_welcome_without_requester_omits_responder_list(tmp_path: Path) -> None:
    """Startup welcomes should not use internal bot permissions to advertise responders."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Writes code",
                ),
            },
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room = nio.MatrixRoom(room_id="!startup:localhost", own_user_id="@mindroom_router:localhost")
    room.add_member("@mindroom_code:localhost", "Code", None)
    room.members_synced = True
    bot.client = AsyncMock()
    bot.client.rooms = {"!startup:localhost": room}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!startup:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await bot._send_welcome_message_if_empty("!startup:localhost")

    response_text = send_response.await_args.kwargs["response_text"]
    assert "\U0001f9e0 **Available agents and teams in this room:**" not in response_text
    assert "@mindroom_code" not in response_text


@pytest.mark.asyncio
async def test_router_invite_welcome_filters_ad_hoc_responders_for_inviter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invite welcomes should advertise responders visible to the inviting user."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Writes code",
                    access=ResponderAccessConfig(users=["@alice:localhost"]),
                ),
                "research": AgentConfig(
                    display_name="Research",
                    role="Finds sources",
                    access=ResponderAccessConfig(users=["@bob:localhost"]),
                ),
            },
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[
                nio.RoomMember("@mindroom_code:localhost", "Code", None),
                nio.RoomMember("@mindroom_research:localhost", "Research", None),
            ],
            room_id="!adhoc:localhost",
        ),
    )
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )

    room = MagicMock(room_id="!adhoc:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@alice:localhost")

    await _handle_invite(bot, room, event)

    response_text = send_response.await_args.kwargs["response_text"]
    assert "\u2022 **@code**: Writes code" in response_text
    assert "@mindroom_research" not in response_text


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_invite_welcome_requires_current_reply_authorization(
    tmp_path: Path,
) -> None:
    """Joining an invite must not let the router welcome a reply-denied inviter."""
    sender_id = "@alice:localhost"
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(users=[]),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)
    room = MagicMock(room_id="!adhoc:localhost", canonical_alias=None)

    await bot._send_welcome_message_if_empty(room.room_id, sender_id)

    send_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_invite_welcome_waits_for_replacement_authorization(
    tmp_path: Path,
) -> None:
    """Welcome delivery must use the policy published after a closed reload gate."""
    sender_id = "@alice:localhost"
    config = bind_runtime_paths(
        with_responder_access(
            Config(router=RouterConfig(model="default", accept_invites=True)),
            ROUTER_AGENT_NAME,
            users=[sender_id],
        ),
        test_runtime_paths(tmp_path),
    )
    denied_config = config.model_copy(deep=True)
    with_responder_access(denied_config, ROUTER_AGENT_NAME, users=[])
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)
    gate = bot.admission_gate
    assert gate.close_if_idle()

    welcome_task = asyncio.create_task(
        bot._room_lifecycle.send_welcome_message_if_empty("!adhoc:localhost", sender_id),
    )
    try:
        await asyncio.sleep(0)
        assert not welcome_task.done()
        bot.config = denied_config
    finally:
        gate.reopen()
        await welcome_task

    send_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_ignores_invite_when_accept_invites_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Routers can opt out of accepting room invites."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=False)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert bot._room_lifecycle.invited_rooms == set()
    assert not _invited_rooms_path(config, ROUTER_AGENT_NAME).exists()


@pytest.mark.asyncio
async def test_router_leave_unconfigured_rooms_preserves_persisted_invited_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Router cleanup should preserve a previously accepted invited room."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, ROUTER_AGENT_NAME)
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_text('[\n  "!router-invited:localhost"\n]\n', encoding="utf-8")
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!configured-room:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        on_room_left: Callable[[str], Awaitable[None]],
    ) -> list[str]:
        left_room_ids.extend(room_ids)
        for room_id in room_ids:
            await on_room_left(room_id)
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(
            return_value=[
                "!configured-room:localhost",
                "!router-invited:localhost",
                "!old-room:localhost",
            ],
        ),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}
    assert left_room_ids == ["!old-room:localhost"]


@pytest.mark.asyncio
async def test_orphan_cleanup_preserves_router_persisted_invited_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Orphan cleanup should not kick the router from an accepted invited room."""
    client = AsyncMock()
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, ROUTER_AGENT_NAME)
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_text('[\n  "!router-invited:localhost"\n]\n', encoding="utf-8")

    monkeypatch.setattr(
        "mindroom.matrix.room_cleanup.get_joined_rooms",
        AsyncMock(return_value=["!router-invited:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.room_cleanup.get_room_members",
        AsyncMock(return_value=["@mindroom_router:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.room_cleanup._get_all_known_bot_user_ids",
        lambda _config, _runtime_paths: {"@mindroom_router:localhost"},
    )
    monkeypatch.setattr("mindroom.matrix.room_cleanup.is_dm_room", AsyncMock(return_value=False))
    client.room_kick = AsyncMock(return_value=nio.RoomKickResponse())

    result = await cleanup_all_orphaned_bots(client, config, runtime_paths_for(config))

    assert result == {}
    client.room_kick.assert_not_called()


@pytest.mark.asyncio
async def test_agent_leaves_unconfigured_rooms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG001
    """Test that agents leave rooms they're no longer configured for."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Create the agent bot with only room1 configured
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],  # Only configured for room1
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Mock joined_rooms to return both room1 and room2 (agent is in both)
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room2:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Track which rooms were left
    left_rooms = []

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    mock_client.room_leave = mock_room_leave
    install_runtime_journal_support(bot)
    recorder = FencedRoomRecorder()
    bot._membership_fence.store = recorder
    fenced_room_ids = recorder.fenced_room_ids

    # Test that the bot leaves unconfigured rooms
    await bot.leave_unconfigured_rooms()

    # Verify the bot left room2 (unconfigured) but not room1 (configured)
    assert len(left_rooms) == 1
    assert "!room2:localhost" in left_rooms
    assert fenced_room_ids == ["!room2:localhost"]


@pytest.mark.asyncio
async def test_router_preserves_root_space_when_leaving_unconfigured_rooms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The router should not leave the managed root Space during room cleanup."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    install_runtime_journal_support(bot)

    mock_client = AsyncMock()
    bot.client = mock_client

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        on_room_left: Callable[[str], Awaitable[None]],
    ) -> list[str]:
        left_room_ids.extend(room_ids)
        for room_id in room_ids:
            await on_room_left(room_id)
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!space:localhost", "!room2:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(space_room_id="!space:localhost"),
    )

    await bot.leave_unconfigured_rooms()

    assert set(left_room_ids) == {"!room2:localhost"}
    assert "!space:localhost" not in left_room_ids


@pytest.mark.asyncio
async def test_agent_manages_rooms_on_config_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that agents update their room memberships when configuration changes."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Start with agent configured for room1 only
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    install_runtime_journal_support(bot)

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track room operations
    joined_rooms = []
    left_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> RoomJoinOutcome:
        joined_rooms.append(room_id)
        return RoomJoinOutcome.JOINED

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)
    mock_client.room_leave = mock_room_leave

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _conversation_reader: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock joined_rooms to return room1 and room3 (agent is in both)
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room3:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Update configuration: now configured for room1 and room2 (not room3)
    bot.rooms = ["!room1:localhost", "!room2:localhost"]

    # Apply room updates
    await bot.join_configured_rooms()
    await bot.leave_unconfigured_rooms()

    # Verify:
    # - Joined room2 (newly configured)
    # - Left room3 (no longer configured)
    # - Stayed in room1 (still configured)
    assert "!room2:localhost" in joined_rooms
    assert "!room3:localhost" in left_rooms
    assert "!room1:localhost" not in left_rooms  # Should stay in room1


@pytest.mark.asyncio
async def test_agent_refuses_invite_when_accept_invites_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Opted-out agents should reject room invites before joining."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=False,
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!invited-room:localhost")
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert not _invited_rooms_path(config, "agent1").exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_agent_refuses_invite_from_unauthorized_sender(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unauthorized users should not be able to force durable membership via invites."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!invited-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@intruder:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert bot._room_lifecycle.invited_rooms == set()
    assert not _invited_rooms_path(config, "agent1").exists()


@dataclass(frozen=True)
class _GrantInviteScenario:
    config: Config
    runtime_paths: RuntimePaths
    orchestrator: _MultiAgentOrchestrator
    agent_user: AgentMatrixUser
    bot: AgentBot
    router_bot: AgentBot
    join_room: AsyncMock
    sender_id: str
    grant_room_id: str
    invited_room_id: str


async def _grant_invite_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> _GrantInviteScenario:
    """Build a denied invite whose first authorized join will fail."""
    sender_id = "@alice:localhost"
    grant_room_id = "!canonical:localhost"
    invited_room_id = "!invited-room:localhost"
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["canonical"],
                    access=ResponderAccessConfig(
                        current_room_members=False,
                        members_of_rooms=["canonical"],
                    ),
                ),
            },
            router=RouterConfig(
                model="default",
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("canonical", grant_room_id, "#canonical:localhost", "Canonical")
    state.save(runtime_paths=runtime_paths)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=orchestrator.agent_reply_memberships,
    )
    install_runtime_journal_support(bot)
    router_bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=orchestrator.agent_reply_memberships,
        agent_reply_membership_sync=orchestrator.agent_reply_membership_sync,
    )
    bot.orchestrator = orchestrator
    router_bot.orchestrator = orchestrator
    orchestrator.agent_bots = {"agent1": bot, ROUTER_AGENT_NAME: router_bot}
    orchestrator.reconcile_reply_authorized_calls = AsyncMock()

    bot.client = make_matrix_client_mock(user_id=agent_user.user_id)
    invited_room = nio.MatrixInvitedRoom(invited_room_id, agent_user.user_id)
    invited_room.inviter = sender_id
    bot.client.invited_rooms = {invited_room_id: invited_room}
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id)
    router_bot.client.invited_rooms = {}
    router_bot.client.joined_rooms = AsyncMock(
        return_value=nio.JoinedRoomsResponse(rooms=[grant_room_id]),
    )
    router_bot.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(members=[], room_id=grant_room_id),
    )
    join_room = AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": sender_id,
            "state_key": agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)

    await orchestrator.agent_reply_memberships.refresh(config, runtime_paths, router_bot.client)
    await _handle_invite(bot, invited_room, event)
    assert bot._room_lifecycle.invited_rooms == set()
    assert bot.client.invited_rooms == {invited_room_id: invited_room}
    return _GrantInviteScenario(
        config=config,
        runtime_paths=runtime_paths,
        orchestrator=orchestrator,
        agent_user=agent_user,
        bot=bot,
        router_bot=router_bot,
        join_room=join_room,
        sender_id=sender_id,
        grant_room_id=grant_room_id,
        invited_room_id=invited_room_id,
    )


async def _restart_grant_invite_scenario(
    scenario: _GrantInviteScenario,
    tmp_path: Path,
) -> AgentBot:
    """Rebuild the bot fleet with the still-live Matrix invite."""
    restarted_orchestrator = _MultiAgentOrchestrator(runtime_paths=scenario.runtime_paths)
    restarted_orchestrator.config = scenario.config
    recovered_bot = make_test_agent_bot(
        agent_user=scenario.agent_user,
        storage_path=tmp_path,
        config=scenario.config,
        runtime_paths=scenario.runtime_paths,
        agent_reply_memberships=restarted_orchestrator.agent_reply_memberships,
    )
    install_runtime_journal_support(recovered_bot)
    restarted_router = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=scenario.config,
        runtime_paths=scenario.runtime_paths,
        agent_reply_memberships=restarted_orchestrator.agent_reply_memberships,
        agent_reply_membership_sync=restarted_orchestrator.agent_reply_membership_sync,
    )
    recovered_bot.orchestrator = restarted_orchestrator
    restarted_router.orchestrator = restarted_orchestrator
    restarted_orchestrator.agent_bots = {
        "agent1": recovered_bot,
        ROUTER_AGENT_NAME: restarted_router,
    }
    recovered_bot.client = make_matrix_client_mock(user_id=scenario.agent_user.user_id)
    invited_room = nio.MatrixInvitedRoom(scenario.invited_room_id, scenario.agent_user.user_id)
    invited_room.inviter = scenario.sender_id
    recovered_bot.client.invited_rooms = {scenario.invited_room_id: invited_room}
    restarted_router.client = make_matrix_client_mock(user_id=restarted_router.agent_user.user_id)
    restarted_router.client.joined_rooms = AsyncMock(
        return_value=nio.JoinedRoomsResponse(rooms=[scenario.grant_room_id]),
    )
    restarted_router.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[nio.RoomMember(scenario.sender_id, None, None)],
            room_id=scenario.grant_room_id,
        ),
    )
    await restarted_orchestrator.refresh_agent_reply_memberships()
    return recovered_bot


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("restart", [False, True], ids=["event-replay", "process-restart"])
async def test_grant_room_join_consumes_one_attempt_from_the_live_invite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    restart: bool,
) -> None:
    """A newly authorized live invite owns one join attempt, not a retry loop."""
    scenario = await _grant_invite_scenario(monkeypatch, tmp_path)

    join_event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": "$alice-joined-canonical",
            "sender": scenario.sender_id,
            "state_key": scenario.sender_id,
            "origin_server_ts": 1,
            "content": {"membership": "join"},
            "unsigned": {"prev_content": {"membership": "invite"}},
        },
    )
    assert isinstance(join_event, nio.RoomMemberEvent)
    if restart:
        recovered_bot = await _restart_grant_invite_scenario(scenario, tmp_path)
    else:
        recovered_bot = scenario.bot
        await scenario.router_bot._apply_live_reply_membership_transition(scenario.grant_room_id, join_event)
        scenario.orchestrator.reconcile_reply_authorized_calls.assert_awaited_once_with()

    assert is_sender_allowed_for_responder(
        scenario.sender_id,
        "agent1",
        scenario.invited_room_id,
        scenario.config,
        scenario.runtime_paths,
        recovered_bot._runtime_view.agent_reply_memberships,
    )
    await recovered_bot.reconcile_live_invites()
    assert await wait_for_background_tasks(timeout=1, owner=recovered_bot._runtime_view)
    assert recovered_bot._room_lifecycle.invited_rooms == set()
    assert recovered_bot.client.invited_rooms == {}
    first_attempt_client = recovered_bot.client if restart else scenario.bot.client
    assert scenario.join_room.await_args_list == [((first_attempt_client, scenario.invited_room_id),)]


@pytest.mark.asyncio
async def test_unknown_entity_refuses_invite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Entities removed from config should reject new invites."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default")),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!invited-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert not _invited_rooms_path(config, "agent1").exists()


@pytest.mark.asyncio
async def test_agent_persists_non_dm_invited_room(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Opted-in agents should persist non-DM invited rooms after joining."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!project-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!project-room:localhost")
    assert bot._room_lifecycle.invited_rooms == {"!project-room:localhost"}
    assert _invited_rooms_path(config, "agent1").read_text(encoding="utf-8") == '[\n  "!project-room:localhost"\n]\n'


@pytest.mark.asyncio
async def test_agent_invite_does_not_auto_add_router_to_ad_hoc_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ad-hoc invites should stay agent-scoped unless the router already manages the room."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_agent1:localhost")

    router_bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
    )
    router_bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_bot.join_configured_rooms = AsyncMock()

    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config
    orchestrator.agent_bots = {"agent1": bot, ROUTER_AGENT_NAME: router_bot}
    bot.orchestrator = orchestrator
    router_bot.orchestrator = orchestrator

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    invite_router = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_invite",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr("mindroom.orchestrator.invite_to_room", invite_router)

    room = MagicMock(room_id="!project-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!project-room:localhost")
    invite_router.assert_not_awaited()
    router_bot.join_configured_rooms.assert_not_awaited()
    assert router_bot._room_lifecycle.invited_rooms == set()
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).exists() is False


@pytest.mark.asyncio
async def test_leave_unconfigured_rooms_preserves_persisted_invited_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup should preserve one previously invited non-DM room."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["!configured-room:localhost"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, "agent1")
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_text('[\n  "!invited-room:localhost"\n]\n', encoding="utf-8")
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!configured-room:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        on_room_left: Callable[[str], Awaitable[None]],
    ) -> list[str]:
        left_room_ids.extend(room_ids)
        for room_id in room_ids:
            await on_room_left(room_id)
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(
            return_value=[
                "!configured-room:localhost",
                "!invited-room:localhost",
                "!old-room:localhost",
            ],
        ),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_rooms", mock_leave_non_dm_rooms)

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == {"!invited-room:localhost"}
    assert left_room_ids == ["!old-room:localhost"]


def test_load_invited_rooms_returns_empty_set_for_invalid_utf8(tmp_path: Path) -> None:
    """Invalid UTF-8 in the persisted invite file should be ignored."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, "agent1")
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_bytes(b"\x80")
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )

    assert bot._room_lifecycle._load_invited_rooms() == set()
    assert bot._room_lifecycle.invited_rooms == set()
