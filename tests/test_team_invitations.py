"""Tests for team room membership functionality."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.users import AgentMatrixUser
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    runtime_paths_for,
    test_runtime_paths,
)


class TestTeamRoomMembership:
    """Test team room membership functionality."""

    @pytest.mark.asyncio
    async def test_team_accepts_invite_without_agent_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Teams should still accept invites even though they have no AgentConfig entry."""
        team_user = AgentMatrixUser(
            agent_name="team1",
            user_id="@mindroom_team1:localhost",
            display_name="Team 1",
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
                teams={
                    "team1": TeamConfig(
                        display_name="Team 1",
                        role="Test team",
                        agents=["agent1"],
                    ),
                },
                router=RouterConfig(model="default"),
            ),
            test_runtime_paths(tmp_path),
        )
        config.defaults.enable_streaming = False
        bot = make_test_agent_bot(
            agent_user=team_user,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        install_runtime_journal_support(bot)
        bot.client = AsyncMock()

        join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
        monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

        room = nio.MatrixInvitedRoom("!team-room:localhost", team_user.user_id)
        event = MagicMock(sender="@user:localhost")
        room.inviter = event.sender
        bot.client.invited_rooms = {room.room_id: room}

        await bot._room_lifecycle._record_pending_room_invite(room.room_id, event.sender)
        await bot._room_lifecycle._handle_recorded_invite(room, event.sender)

        join_room.assert_awaited_once_with(bot.client, "!team-room:localhost")
