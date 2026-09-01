"""Tests for agent self-managed room membership.

With the new self-managing agent pattern, agents handle their own room
memberships. This test module verifies that behavior.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks.matrix_admin import build_hook_matrix_admin
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    is_inviter_allowed,
    load_invited_rooms,
    load_pending_room_invites,
    pending_room_invites_path,
    save_invited_rooms,
    should_accept_invites,
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


def _invited_rooms_path(config: Config, agent_name: str) -> Path:
    return invited_rooms_path(runtime_paths_for(config).storage_root, agent_name)


def _pending_room_invites(config: Config, agent_name: str) -> dict[str, str]:
    path = pending_room_invites_path(runtime_paths_for(config).storage_root, agent_name)
    return load_pending_room_invites(path)


def _cache_current_invite(bot: AgentBot, room_id: str, sender: str) -> nio.MatrixInvitedRoom:
    """Mirror nio's current-invite cache before delivering its callback."""
    client = bot.client
    assert client is not None
    invited_rooms = client.invited_rooms
    if not isinstance(invited_rooms, dict):
        invited_rooms = {}
    current_room = invited_rooms.get(room_id)
    if not isinstance(current_room, nio.MatrixInvitedRoom):
        current_room = nio.MatrixInvitedRoom(room_id, bot.agent_user.user_id)
        invited_rooms[room_id] = current_room
    current_room.inviter = sender
    client.invited_rooms = invited_rooms
    return current_room


async def _handle_invite(bot: AgentBot, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
    current_room = _cache_current_invite(bot, room.room_id, event.sender)
    bot._room_lifecycle.record_pending_room_invite(room.room_id, event.sender)
    await bot._room_lifecycle.handle_recorded_invite(current_room, event.sender)


def _router_user() -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )


@pytest.mark.parametrize(
    ("policy", "sender_id", "expected"),
    [
        (True, "@anyone:anywhere.example", True),
        (False, "@owner:example.com", False),
        ([], "@owner:example.com", False),
        (["@owner:example.com"], "@owner:example.com", True),
        (["@*:trusted.example.com"], "@member:trusted.example.com", True),
        (["@owner:example.com"], "@outsider:example.com", False),
    ],
)
def test_invitation_policy_is_independent_for_every_responder(
    policy: bool | list[str],
    sender_id: str,
    expected: bool,
) -> None:
    """The dedicated invite policy must decide joins without responder access."""
    config = Config(
        router=RouterConfig(model="default", accept_invites=policy),
        agents={
            "research": AgentConfig(
                display_name="Research",
                accept_invites=policy,
            ),
        },
        teams={
            "reviewers": TeamConfig(
                display_name="Reviewers",
                role="Review work",
                agents=["research"],
                accept_invites=policy,
            ),
        },
    )
    assert is_inviter_allowed(config, ROUTER_AGENT_NAME, sender_id) is expected
    assert is_inviter_allowed(config, "research", sender_id) is expected
    assert is_inviter_allowed(config, "reviewers", sender_id) is expected
    assert should_accept_invites(config, ROUTER_AGENT_NAME) is bool(policy)
    assert should_accept_invites(config, "research") is bool(policy)
    assert should_accept_invites(config, "reviewers") is bool(policy)


def test_invitation_policy_resolves_aliases_before_matching() -> None:
    """An inviter alias must match the same canonical pattern as conversation access."""
    config = Config(
        router=RouterConfig(model="default", accept_invites=["@owner:example.com"]),
        agents={
            "research": AgentConfig(
                display_name="Research",
                accept_invites=["@owner:example.com"],
            ),
        },
        teams={
            "reviewers": TeamConfig(
                display_name="Reviewers",
                role="Review work",
                agents=["research"],
                accept_invites=["@owner:example.com"],
            ),
        },
    )
    config.authorization.aliases = {"@owner:example.com": ["@bridge-owner:example.com"]}
    assert is_inviter_allowed(config, ROUTER_AGENT_NAME, "@bridge-owner:example.com") is True
    assert is_inviter_allowed(config, "research", "@bridge-owner:example.com") is True
    assert is_inviter_allowed(config, "reviewers", "@bridge-owner:example.com") is True


def _live_router_invite_scenario(
    tmp_path: Path,
    *,
    room_id: str = "!invited:localhost",
    sender_id: str = "@owner:localhost",
) -> tuple[Config, AgentBot, nio.MatrixInvitedRoom, nio.InviteMemberEvent]:
    """Build one fresh router invitation scenario."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(current_room_members=True),
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
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room = nio.MatrixInvitedRoom(room_id, bot.agent_user.user_id)
    room.inviter = sender_id
    bot.client.invited_rooms = {room_id: room}
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": sender_id,
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)
    return config, bot, room, event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "sender_id", "accepted"),
    [
        (["@owner:localhost"], "@owner:localhost", True),
        (["@*:localhost"], "@owner:localhost", True),
        (["@other:localhost"], "@owner:localhost", False),
        ([], "@owner:localhost", False),
    ],
)
async def test_router_invitation_list_controls_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: list[str],
    sender_id: str,
    accepted: bool,
) -> None:
    """The room lifecycle must enforce the router's dedicated inviter patterns."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path, sender_id=sender_id)
    config.router.accept_invites = policy
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await _handle_invite(bot, room, event)

    if accepted:
        join_room.assert_awaited_once_with(bot.client, room.room_id)
        assert bot._room_lifecycle.invited_rooms == {room.room_id}
    else:
        join_room.assert_not_awaited()
        assert bot._room_lifecycle.invited_rooms == set()


@pytest.mark.asyncio
async def test_router_invitation_list_uses_current_inviter_at_join_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A replacement invite must own authorization before the join starts."""
    allowed_sender = "@owner:localhost"
    replacement_sender = "@outsider:localhost"
    config, bot, room, event = _live_router_invite_scenario(tmp_path, sender_id=allowed_sender)
    config.router.accept_invites = [allowed_sender]
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    update_join_fences = bot._sync_continuity_store.update_join_fences
    fence_started = threading.Event()
    release_fence = threading.Event()

    def block_fence_persistence(
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
        retain: tuple[str, ...] | None = None,
    ) -> object:
        if add:
            fence_started.set()
            assert release_fence.wait(timeout=2)
        return update_join_fences(add=add, remove=remove, retain=retain)

    monkeypatch.setattr(bot._sync_continuity_store, "update_join_fences", block_fence_persistence)
    task = asyncio.create_task(_handle_invite(bot, room, event))
    try:
        assert await asyncio.to_thread(fence_started.wait, 2)
        room.inviter = replacement_sender
        bot._room_lifecycle.record_pending_room_invite(room.room_id, replacement_sender)
    finally:
        release_fence.set()

    await task

    join_room.assert_not_awaited()
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {
        room.room_id: replacement_sender,
    }
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room.room_id)


@pytest.mark.asyncio
async def test_authoritative_departure_revokes_current_invite_before_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A leave observed while invite fencing is pending must prevent the join from starting."""
    _config, bot, room, event = _live_router_invite_scenario(tmp_path)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    update_join_fences = bot._sync_continuity_store.update_join_fences
    invite_fence_started = threading.Event()
    release_invite_fence = threading.Event()
    departure_started = asyncio.Event()
    release_departure = asyncio.Event()

    def block_invite_fence_persistence(
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
        retain: tuple[str, ...] | None = None,
    ) -> object:
        if add:
            invite_fence_started.set()
            assert release_invite_fence.wait(timeout=2)
        return update_join_fences(add=add, remove=remove, retain=retain)

    async def block_departure_fence(_fence: object, _departures: object) -> None:
        departure_started.set()
        await release_departure.wait()

    monkeypatch.setattr(bot._sync_continuity_store, "update_join_fences", block_invite_fence_persistence)
    monkeypatch.setattr(type(bot._membership_fence), "fence_reported_departures", block_departure_fence)
    invite_task = asyncio.create_task(_handle_invite(bot, room, event))
    departure_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(invite_fence_started.wait, 2)
        response = MagicMock(spec=nio.SyncResponse)
        response.next_batch = "s_after_cancel"
        response.rooms = MagicMock(join={}, invite={}, leave={room.room_id: MagicMock()})
        departure_task = asyncio.create_task(bot._apply_own_room_membership_from_sync(response))
        await asyncio.wait_for(departure_started.wait(), timeout=2)

        release_invite_fence.set()
        await invite_task

        join_room.assert_not_awaited()
        client = bot.client
        assert client is not None
        assert room.room_id not in client.invited_rooms
    finally:
        release_invite_fence.set()
        release_departure.set()
        if departure_task is not None:
            await departure_task


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
async def test_agent_rejoins_persisted_invited_rooms_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Persisted ad-hoc invited rooms should be reconciled during startup joins."""
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

    join_room.assert_awaited_once_with(mock_client, "!invited-room:localhost")


@pytest.mark.asyncio
async def test_router_accepts_agent_invite_persists_and_rejoins_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Router should auto-accept an internal agent invite as durable desired membership."""
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
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr(restarted_bot, "_post_join_room_setup", AsyncMock())

    await restarted_bot.join_configured_rooms()

    join_room.assert_awaited_once_with(restarted_bot.client, "!router-invited:localhost")


@pytest.mark.asyncio
async def test_live_invite_forbidden_join_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live invite's ambiguous forbidden join must remain retryable."""
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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )

    with pytest.raises(RuntimeError, match="Failed to join invited room"):
        await _handle_invite(
            bot,
            MagicMock(room_id="!failed:localhost", canonical_alias=None),
            MagicMock(sender="@owner:localhost"),
        )
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {
        "!failed:localhost": "@owner:localhost",
    }
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!failed:localhost")


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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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

    room = _cache_current_invite(bot, "!invalid-state:localhost", event.sender)
    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    bot.client.join.assert_awaited_once_with("!invalid-state:localhost")
    assert await bot._journal_dispatcher.store.pending() == ()
    assert not bot._room_lifecycle.decrypt_notice_is_fenced("!invalid-state:localhost")


@pytest.mark.asyncio
async def test_recovered_invite_waits_for_current_matrix_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A durable pending record must not supply current inviter authority."""
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
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    room_id = "!revoked-invite:localhost"
    bot._room_lifecycle.record_pending_room_invite(room_id, "@owner:localhost")

    await bot._room_lifecycle.reconcile_pending_invites()
    await bot._room_lifecycle.reconcile_pending_invites()

    bot.client.join.assert_not_awaited()
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {
        room_id: "@owner:localhost",
    }
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
    room = _cache_current_invite(bot, "!invited:localhost", event.sender)

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_awaited_once_with(bot.client, room.room_id)
    welcome_message.assert_awaited_once_with(room.room_id, event.sender)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert await bot._journal_dispatcher.store.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "state_key", "membership"),
    [
        ("m.room.name", "", None),
        ("m.room.member", "@other:localhost", "invite"),
        ("m.room.member", "self", "leave"),
    ],
    ids=["room-metadata", "other-member", "non-invite-membership"],
)
async def test_only_authenticated_self_invites_start_invite_work(
    tmp_path: Path,
    event_type: str,
    state_key: str,
    membership: str | None,
) -> None:
    """Unrelated invite-state callbacks must not create join authority or work."""
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
    bot._room_lifecycle.handle_recorded_invite = AsyncMock()
    room = nio.MatrixInvitedRoom("!metadata:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": event_type,
            "sender": "@event-sender:localhost",
            "state_key": bot.matrix_id.full_id if state_key == "self" else state_key,
            "content": {"membership": membership} if membership is not None else {"name": "Project"},
        },
    )
    assert isinstance(event, nio.InviteEvent)

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    bot._room_lifecycle.handle_recorded_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_sync_callback_runs_durable_join_in_background(tmp_path: Path) -> None:
    """Durable invite admission must not hold the sync loop across network work."""
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

    bot._room_lifecycle.handle_recorded_invite = delayed_invite
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
        await asyncio.wait_for(join_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert callback_task.done()
        assert bot._room_lifecycle._pending_room_invites == {room.room_id: event.sender}
        # The dedicated pending-invite store owns recovery, so no journal row is needed.
        assert await bot._journal_dispatcher.store.pending() == ()
    finally:
        release_join.set()
        await callback_task

    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    assert await bot._journal_dispatcher.store.pending() == ()


@pytest.mark.asyncio
async def test_invite_sync_callback_does_not_start_work_when_identity_save_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invite work must not outlive a failed durable identity write."""
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
    bot._room_lifecycle.handle_recorded_invite = AsyncMock()
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_pending_room_invites", lambda *_args: False)
    room = nio.MatrixRoom("!failed-pending-save:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.matrix_id.full_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteEvent)

    with pytest.raises(OSError, match="Failed to persist pending room invite"):
        await bot._on_invite_before_sync_certification(room, event)

    bot._room_lifecycle.handle_recorded_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_persistence_failure_propagates_to_sync_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed invited-room saves must leave invite work retryable."""
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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())

    with pytest.raises(OSError, match="Failed to persist invited room"):
        await _handle_invite(
            bot,
            MagicMock(room_id="!failed-save:localhost", canonical_alias=None),
            MagicMock(sender="@owner:localhost"),
        )


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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", record_rooms_to_leave)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == {"!hook-created:localhost"}
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
async def test_router_invite_keeps_memory_after_transient_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed save must not let the next invite erase the first room from memory."""
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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())

    first_room = MagicMock(room_id="!first:localhost", canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")
    with pytest.raises(OSError, match="Failed to persist invited room"):
        await _handle_invite(bot, first_room, event)

    await _handle_invite(bot, first_room, event)
    await _handle_invite(
        bot,
        MagicMock(room_id="!second:localhost", canonical_alias=None),
        event,
    )

    expected_rooms = {"!first:localhost", "!second:localhost"}
    assert bot._room_lifecycle.invited_rooms == expected_rooms
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!first:localhost",\n  "!second:localhost"\n]\n'
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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
async def test_cleanup_does_not_resurrect_room_pending_durable_forget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed durable forget must still make the departed room eligible for cleanup."""
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

    assert await bot._room_lifecycle._rooms_to_leave() == ["!departed:localhost"]
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
async def test_router_duplicate_invite_retries_failed_welcome_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Duplicate invite callbacks should retry welcome delivery after a failed first send."""
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
    send_response = AsyncMock(side_effect=[None, "$welcome"])
    install_send_response_mock(bot, send_response)

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    with pytest.raises(RuntimeError, match="Failed to complete welcome message"):
        await _handle_invite(bot, room, event)
    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    assert bot.client.room_messages.await_count == 2
    assert send_response.await_count == 2
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}


@pytest.mark.asyncio
async def test_redelivered_invite_retries_a_failed_welcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A redelivered invite must retry a welcome whose delivery failed."""
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
    send_response = AsyncMock(side_effect=[None, "$welcome"])
    install_send_response_mock(bot, send_response)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
    room = _cache_current_invite(bot, "!router-invited:localhost", event.sender)
    await bot._on_invite_before_sync_certification(room, event)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    # The welcome failed. An invite the bot has not finished acting on is
    # still in the next sync response, so the retry arrives as a redelivery
    # rather than from an in-process retry loop.
    await bot._on_invite_before_sync_certification(room, event)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_awaited_with(bot.client, room.room_id)
    assert send_response.await_count == 2
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
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)
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
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)
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
@pytest.mark.parametrize("private", [False, True], ids=["shared", "requester-private"])
async def test_agent_accepts_invite_independently_of_conversation_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    private: bool,
) -> None:
    """Joining must not grant or require permission to converse with an agent."""
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
                    private=AgentPrivateConfig(per="user") if private else None,
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
    room.canonical_alias = None
    event = MagicMock(sender="@intruder:localhost")
    assert not is_sender_allowed_for_responder(
        event.sender,
        "agent1",
        room.room_id,
        config,
        runtime_paths_for(config),
        bot._runtime_view.agent_reply_memberships,
    )

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, room.room_id)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert not is_sender_allowed_for_responder(
        event.sender,
        "agent1",
        room.room_id,
        config,
        runtime_paths_for(config),
        bot._runtime_view.agent_reply_memberships,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(
    ("policy", "access_users", "expected_join"),
    [
        (["@inviter:localhost"], [], True),
        (["@someone-else:localhost"], ["@inviter:localhost"], False),
    ],
    ids=["invite-policy-allows-access-denies", "invite-policy-denies-access-allows"],
)
async def test_team_invitation_policy_is_independent_of_conversation_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: list[str],
    access_users: list[str],
    expected_join: bool,
) -> None:
    """Team invitation admission must not reuse its post-join conversation policy."""
    sender = "@inviter:localhost"
    team_name = "reviewers"
    config = bind_runtime_paths(
        Config(
            agents={"research": AgentConfig(display_name="Research")},
            teams={
                team_name: TeamConfig(
                    display_name="Reviewers",
                    role="Review work",
                    agents=["research"],
                    accept_invites=policy,
                    access=ResponderAccessConfig(
                        current_room_members=False,
                        members_of_rooms=[],
                        users=access_users,
                    ),
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    team_user = AgentMatrixUser(
        agent_name=team_name,
        user_id="@mindroom_reviewers:localhost",
        display_name="Reviewers",
        password=TEST_PASSWORD,
    )
    bot = make_test_agent_bot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = make_matrix_client_mock(user_id=team_user.user_id)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    room = MagicMock(room_id="!team-invited:localhost", canonical_alias=None)
    event = MagicMock(sender=sender)

    await _handle_invite(bot, room, event)

    assert join_room.await_count == int(expected_join)
    assert (room.room_id in bot._room_lifecycle.invited_rooms) is expected_join


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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
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
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)

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
