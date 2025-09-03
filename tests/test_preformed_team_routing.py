"""Tests for predefined team mentions and routing behavior.

These tests ensure that mentioning a predefined team:
- Does NOT trigger router routing
- Does cause the TeamBot to respond using its configured team members
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot, TeamBot
from mindroom.config import AgentConfig, Config, RouterConfig, TeamConfig
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config_with_team() -> Config:
    """Minimal config with two agents and one predefined team in a room."""
    return Config(
        agents={
            "a1": AgentConfig(display_name="Agent One", role="", rooms=["room_x"]),
            "a2": AgentConfig(display_name="Agent Two", role="", rooms=["room_x"]),
        },
        teams={
            "t1": TeamConfig(
                display_name="Team One",
                role="Test preformed team",
                agents=["a1", "a2"],
                rooms=["room_x"],
                mode="coordinate",
            ),
        },
        router=RouterConfig(model="default"),
    )


def _mock_room(room_id: str, member_ids: list[str]) -> MagicMock:
    room = MagicMock()
    room.room_id = room_id
    room.name = room_id
    room.users = member_ids
    return room


def _mock_event_with_team_mention(team_user_id: str, body: str = "@team please help") -> MagicMock:
    ev = MagicMock()
    ev.sender = "@user:localhost"
    ev.body = body
    ev.event_id = "$evt1"
    ev.source = {
        "content": {
            "body": body,
            "m.mentions": {"user_ids": [team_user_id]},
        },
    }
    return ev


@pytest.mark.asyncio
async def test_router_does_not_route_when_preformed_team_is_mentioned(config_with_team: Config, tmp_path: Path) -> None:
    """Router must not route if the message mentions a predefined team."""
    # Router bot setup
    # Use config-derived IDs to match domain in this environment
    router_user = AgentMatrixUser(
        agent_name="router",
        user_id=config_with_team.ids["router"].full_id,
        display_name="Router",
        password="p",  # noqa: S106
    )
    router = AgentBot(router_user, tmp_path, config_with_team)
    router.client = AsyncMock()

    # Room has router + team + two agents and the human user
    team_user_id = config_with_team.ids["t1"].full_id
    a1_id = config_with_team.ids["a1"].full_id
    a2_id = config_with_team.ids["a2"].full_id
    room = _mock_room("!room:localhost", [router_user.user_id, team_user_id, a1_id, a2_id, "@user:localhost"])

    # Event mentions the team
    event = _mock_event_with_team_mention(team_user_id)

    # Ensure no thread history fetch is attempted
    with (
        patch("mindroom.bot.fetch_thread_history", new=AsyncMock(return_value=[])),
        # Also patch suggest_agent_for_message to detect accidental routing
        patch("mindroom.bot.suggest_agent_for_message", new=AsyncMock(return_value="a1")),
    ):
        await router._on_message(room, event)

    # Router must not send any message (i.e., must not route)
    router.client.room_send.assert_not_called()


@pytest.mark.asyncio
async def test_preformed_team_bot_responds_when_mentioned(config_with_team: Config, tmp_path: Path) -> None:
    """TeamBot should respond with team response when the team is mentioned."""
    team_user = AgentMatrixUser(
        agent_name="t1",
        user_id=config_with_team.ids["t1"].full_id,
        display_name="Team One",
        password="p",  # noqa: S106
    )
    # Convert agent names to MatrixID objects
    team_matrix_ids = [
        MatrixID.from_username("a1", config_with_team.domain),
        MatrixID.from_username("a2", config_with_team.domain),
    ]
    bot = TeamBot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config_with_team,
        rooms=["!room:localhost"],
        team_agents=team_matrix_ids,
        team_mode="coordinate",
        enable_streaming=False,
    )
    bot.client = AsyncMock()

    # Minimal orchestrator stub is fine because we patch team_response
    bot.orchestrator = MagicMock()

    team_user_id = config_with_team.ids["t1"].full_id
    room = _mock_room("!room:localhost", [team_user_id, "@user:localhost"])
    event = _mock_event_with_team_mention(team_user_id)

    # No thread context in this test
    with patch("mindroom.bot.fetch_thread_history", new=AsyncMock(return_value=[])):
        # Patch team_response to avoid invoking Agno, return deterministic text
        async def fake_team_response(*_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
            return "🤝 Team Response (a1, a2):\n\n**a1**: ok\n\n**a2**: ok"

        with (
            patch("mindroom.bot.team_response", new=fake_team_response),
            patch(
                "mindroom.bot.should_agent_respond",
                return_value=True,
            ),
        ):
            await bot._on_message(room, event)

    # Team bot should have sent exactly one message
    assert bot.client.room_send.call_count == 2  # initial + streaming updates for team
    args, kwargs = bot.client.room_send.call_args
    # kwargs contains content with formatted body
    assert "🤝 Team Response" in kwargs["content"]["formatted_body"]


@pytest.mark.asyncio
async def test_team_does_not_respond_to_different_domain_mention(config_with_team: Config, tmp_path: Path) -> None:
    """TeamBot should NOT respond to mentions of the same username on a different domain.

    This is a security test - @mindroom_t1:evil.org should not trigger @mindroom_t1:localhost.
    """
    team_user = AgentMatrixUser(
        agent_name="t1",
        user_id=config_with_team.ids["t1"].full_id,
        display_name="Team One",
        password="p",  # noqa: S106
    )
    # Convert agent names to MatrixID objects
    team_matrix_ids = [
        MatrixID.from_username("a1", config_with_team.domain),
        MatrixID.from_username("a2", config_with_team.domain),
    ]
    bot = TeamBot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config_with_team,
        rooms=["!room:localhost"],
        team_agents=team_matrix_ids,
        team_mode="coordinate",
        enable_streaming=False,
    )
    bot.client = AsyncMock()
    bot.orchestrator = MagicMock()

    # Craft a mention using a DIFFERENT domain than the bot's MatrixID
    # This simulates someone trying to impersonate the team
    other_domain = "evil.org"
    if team_user.matrix_id.domain == other_domain:
        other_domain = "attacker.com"
    mentioned_id = f"@mindroom_t1:{other_domain}"

    room = _mock_room("!room:localhost", [team_user.user_id, "@user:localhost"])
    event = _mock_event_with_team_mention(mentioned_id, body=f"{mentioned_id} ping")

    with patch("mindroom.bot.fetch_thread_history", new=AsyncMock(return_value=[])):

        async def fake_team_response(*_args: Any, **_kwargs: Any) -> str:  # noqa: ANN401
            return "🤝 Team Response (a1, a2): ok"

        with patch("mindroom.bot.team_response", new=fake_team_response):
            await bot._on_message(room, event)

    # Team bot should NOT have responded - different domain!
    assert bot.client.room_send.call_count == 0
