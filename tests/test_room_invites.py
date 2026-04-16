"""Tests for agent self-managed room membership.

With the new self-managing agent pattern, agents handle their own room
memberships. This test module verifies that behavior.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from nio.responses import Response


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

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track which rooms were joined
    joined_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        **_kwargs: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)
    monkeypatch.setattr("mindroom.bot.get_joined_rooms", AsyncMock(return_value=[]))

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
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )

    mock_client = AsyncMock()
    mock_client.rooms = {"!room1:localhost": MagicMock()}
    bot.client = mock_client

    join_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot.join_room", join_room)
    monkeypatch.setattr("mindroom.bot.get_joined_rooms", AsyncMock(return_value=["!room1:localhost"]))
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))

    await bot.join_configured_rooms()

    join_room.assert_awaited_once_with(mock_client, "!room2:localhost")


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

    bot = AgentBot(
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

    # Test that the bot leaves unconfigured rooms
    await bot.leave_unconfigured_rooms()

    # Verify the bot left room2 (unconfigured) but not room1 (configured)
    assert len(left_rooms) == 1
    assert "!room2:localhost" in left_rooms


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
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )

    mock_client = AsyncMock()
    bot.client = mock_client

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(_client: AsyncMock, room_ids: list[str]) -> None:
        left_room_ids.extend(room_ids)

    monkeypatch.setattr(
        "mindroom.bot.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!space:localhost", "!room2:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr(
        "mindroom.bot.MatrixState.load",
        lambda **_kwargs: MatrixState(space_room_id="!space:localhost"),
    )

    await bot.leave_unconfigured_rooms()

    assert set(left_room_ids) == {"!room2:localhost"}
    assert "!space:localhost" not in left_room_ids


@pytest.mark.asyncio
async def test_router_preserves_persisted_ad_hoc_rooms_when_leaving_unconfigured_rooms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The router should keep persisted ad-hoc rooms during cleanup."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )

    mock_client = AsyncMock()
    bot.client = mock_client

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(_client: AsyncMock, room_ids: list[str]) -> None:
        left_room_ids.extend(room_ids)

    monkeypatch.setattr(
        "mindroom.bot.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!adhoc:localhost", "!room2:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr(
        "mindroom.bot.MatrixState.load",
        lambda **_kwargs: MatrixState(router_ad_hoc_room_ids={"!adhoc:localhost"}),
    )

    await bot.leave_unconfigured_rooms()

    assert set(left_room_ids) == {"!room2:localhost"}
    assert "!adhoc:localhost" not in left_room_ids


@pytest.mark.asyncio
async def test_non_router_retries_router_auto_invite_on_next_message(tmp_path: Path) -> None:
    """A later message in an ad-hoc room should retry reconciliation with the current sender."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    orchestrator = MagicMock()
    orchestrator.handle_bot_joined_invite_room = AsyncMock(return_value="retry")
    bot.orchestrator = orchestrator
    bot._maybe_handle_tool_approval_reply = AsyncMock(return_value=False)
    bot._turn_controller.handle_text_event = AsyncMock()
    state = MatrixState(router_ad_hoc_inviter_ids={"!adhoc:localhost": "@original-owner:localhost"})

    room = MagicMock(room_id="!adhoc:localhost")
    event = MagicMock(sender="@later-speaker:localhost")

    with patch("mindroom.bot.MatrixState.load", return_value=state):
        await bot._on_message(room, event)

    orchestrator.handle_bot_joined_invite_room.assert_awaited_once_with(
        bot,
        "!adhoc:localhost",
        actor_id="@later-speaker:localhost",
        invite_is_direct=False,
    )
    bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)


@pytest.mark.asyncio
async def test_non_router_pending_inviter_lifecycle_clears_after_successful_retry(tmp_path: Path) -> None:
    """Invite reconciliation should store the inviter, retry later, then clear it on success."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    orchestrator = MagicMock()
    orchestrator.handle_bot_joined_invite_room = AsyncMock(side_effect=["retry", "reconciled"])
    bot.orchestrator = orchestrator
    bot._maybe_handle_tool_approval_reply = AsyncMock(return_value=False)
    bot._turn_controller.handle_text_event = AsyncMock()
    bot._post_join_room_setup = AsyncMock()
    bot.client = AsyncMock()
    state = MatrixState()

    room = MagicMock(room_id="!adhoc:localhost")
    invite_event = MagicMock(sender="@original-owner:localhost", content={})
    message_event = MagicMock(sender="@later-speaker:localhost")

    with (
        patch("mindroom.bot.join_room", new=AsyncMock(return_value=True)),
        patch("mindroom.bot.MatrixState.load", return_value=state),
    ):
        await bot._on_invite(room, invite_event)
        assert state.router_ad_hoc_inviter_ids == {"!adhoc:localhost": "@original-owner:localhost"}

        await bot._on_message(room, message_event)

    assert orchestrator.handle_bot_joined_invite_room.await_count == 2
    assert orchestrator.handle_bot_joined_invite_room.await_args_list[0].kwargs == {
        "actor_id": "@original-owner:localhost",
        "invite_is_direct": False,
    }
    assert orchestrator.handle_bot_joined_invite_room.await_args_list[1].kwargs == {
        "actor_id": "@later-speaker:localhost",
        "invite_is_direct": False,
    }
    assert state.router_ad_hoc_inviter_ids == {}
    bot._turn_controller.handle_text_event.assert_awaited_once_with(room, message_event)


@pytest.mark.asyncio
async def test_router_keeps_persisted_ad_hoc_rooms_with_human_members_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup should keep persisted ad-hoc rooms when humans still share the room with the router."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )

    mock_client = AsyncMock()
    mock_client.rooms = {"!room1:localhost": MagicMock(), "!adhoc:localhost": MagicMock()}
    bot.client = mock_client
    install_runtime_cache_support(bot)

    state = MatrixState(router_ad_hoc_room_ids={"!adhoc:localhost"})
    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(_client: AsyncMock, room_ids: list[str]) -> None:
        left_room_ids.extend(room_ids)

    monkeypatch.setattr(
        "mindroom.bot.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!adhoc:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.bot.get_room_members",
        AsyncMock(return_value={"@mindroom_router:localhost", "@owner:localhost"}),
    )
    monkeypatch.setattr("mindroom.bot.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))
    monkeypatch.setattr("mindroom.bot.config_confirmation.restore_pending_changes", AsyncMock(return_value=0))
    monkeypatch.setattr("mindroom.bot.AgentBot._send_welcome_message_if_empty", AsyncMock())
    monkeypatch.setattr("mindroom.bot.MatrixState.load", lambda **_kwargs: state)

    await bot.ensure_rooms()

    assert state.router_ad_hoc_room_ids == {"!adhoc:localhost"}
    assert left_room_ids == []


@pytest.mark.asyncio
async def test_router_prunes_router_only_ad_hoc_room_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup should forget persisted ad-hoc rooms once the router is the only remaining member."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )

    mock_client = AsyncMock()
    mock_client.rooms = {"!room1:localhost": MagicMock(), "!adhoc:localhost": MagicMock()}
    bot.client = mock_client
    install_runtime_cache_support(bot)

    state = MatrixState(router_ad_hoc_room_ids={"!adhoc:localhost"})
    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(_client: AsyncMock, room_ids: list[str]) -> None:
        left_room_ids.extend(room_ids)

    monkeypatch.setattr(
        "mindroom.bot.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!adhoc:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.bot.get_room_members",
        AsyncMock(return_value={"@mindroom_router:localhost"}),
    )
    monkeypatch.setattr("mindroom.bot.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))
    monkeypatch.setattr("mindroom.bot.config_confirmation.restore_pending_changes", AsyncMock(return_value=0))
    monkeypatch.setattr("mindroom.bot.AgentBot._send_welcome_message_if_empty", AsyncMock())
    monkeypatch.setattr("mindroom.bot.MatrixState.load", lambda **_kwargs: state)

    await bot.ensure_rooms()

    assert state.router_ad_hoc_room_ids == set()
    assert left_room_ids == ["!adhoc:localhost"]


@pytest.mark.asyncio
async def test_router_keeps_ad_hoc_room_when_member_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup prune should not drop persisted ad-hoc rooms on member lookup failures."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )

    mock_client = AsyncMock()
    mock_client.rooms = {"!room1:localhost": MagicMock(), "!adhoc:localhost": MagicMock()}
    bot.client = mock_client
    install_runtime_cache_support(bot)

    state = MatrixState(router_ad_hoc_room_ids={"!adhoc:localhost"})
    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(_client: AsyncMock, room_ids: list[str]) -> None:
        left_room_ids.extend(room_ids)

    monkeypatch.setattr(
        "mindroom.bot.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!adhoc:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot.get_room_members", AsyncMock(side_effect=RuntimeError("boom members")))
    monkeypatch.setattr("mindroom.bot.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))
    monkeypatch.setattr("mindroom.bot.config_confirmation.restore_pending_changes", AsyncMock(return_value=0))
    monkeypatch.setattr("mindroom.bot.AgentBot._send_welcome_message_if_empty", AsyncMock())
    monkeypatch.setattr("mindroom.bot.MatrixState.load", lambda **_kwargs: state)

    await bot.ensure_rooms()

    assert state.router_ad_hoc_room_ids == {"!adhoc:localhost"}
    assert left_room_ids == []


@pytest.mark.asyncio
async def test_router_removes_ad_hoc_room_when_membership_leaves(tmp_path: Path) -> None:
    """Router membership leave events should clear persisted ad-hoc room state."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    state = MatrixState(router_ad_hoc_room_ids={"!adhoc:localhost"})
    room = MagicMock(room_id="!adhoc:localhost")
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "sender": "@admin:localhost",
            "state_key": "@mindroom_router:localhost",
            "content": {"membership": "leave"},
            "prev_content": {"membership": "join"},
            "unsigned": {"prev_content": {"membership": "join"}},
            "event_id": "$leave",
            "origin_server_ts": 123,
        },
    )

    with patch("mindroom.bot.MatrixState.load", return_value=state):
        await bot._on_room_member(room, event)

    assert state.router_ad_hoc_room_ids == set()


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

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track room operations
    joined_rooms = []
    left_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    mock_client.room_leave = mock_room_leave

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        **_kwargs: object,
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
