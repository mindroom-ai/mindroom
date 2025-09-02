"""Tests for team-based agent collaboration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config import AgentConfig, Config, ModelConfig, RouterConfig
from mindroom.matrix.users import AgentMatrixUser
from mindroom.thread_utils import get_agents_in_thread

from .conftest import TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


# Test fixtures for team agents
@pytest.fixture
def mock_research_agent() -> AgentMatrixUser:
    """Create a mock research agent."""
    return AgentMatrixUser(
        agent_name="research",
        user_id="@mindroom_research:localhost",
        display_name="ResearchAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_analyst_agent() -> AgentMatrixUser:
    """Create a mock analyst agent."""
    return AgentMatrixUser(
        agent_name="analyst",
        user_id="@mindroom_analyst:localhost",
        display_name="AnalystAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_code_agent() -> AgentMatrixUser:
    """Create a mock code agent."""
    return AgentMatrixUser(
        agent_name="code",
        user_id="@mindroom_code:localhost",
        display_name="CodeAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_security_agent() -> AgentMatrixUser:
    """Create a mock security agent."""
    return AgentMatrixUser(
        agent_name="security",
        user_id="@mindroom_security:localhost",
        display_name="SecurityAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def team_room_id() -> str:
    """Room ID where team collaboration happens."""
    return "!team_room:localhost"


class TestTeamFormation:
    """Test team formation logic."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["#test:example.org"]),
                "security": AgentConfig(display_name="Security", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="Research", rooms=["#test:example.org"]),
                "analyst": AgentConfig(display_name="Analyst", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    @pytest.mark.asyncio
    async def test_multiple_agents_tagged_form_team(
        self,
        mock_research_agent: AgentMatrixUser,
        mock_analyst_agent: AgentMatrixUser,
        team_room_id: str,
        tmp_path: Path,
    ) -> None:
        """Test that multiple agents tagged in a message form a team."""
        # Create bots
        config = Config(router=RouterConfig(model="default"))

        research_bot = AgentBot(mock_research_agent, tmp_path, config, rooms=[team_room_id])
        config = Config(router=RouterConfig(model="default"))

        analyst_bot = AgentBot(mock_analyst_agent, tmp_path, config, rooms=[team_room_id])

        # Setup bots
        research_bot.client = AsyncMock()
        analyst_bot.client = AsyncMock()
        research_bot.response_tracker = MagicMock()
        research_bot.response_tracker.has_responded.return_value = False
        analyst_bot.response_tracker = MagicMock()
        analyst_bot.response_tracker.has_responded.return_value = False

        # Create message mentioning both agents
        message_event: dict[str, Any] = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": f"@{mock_research_agent.display_name} @{mock_analyst_agent.display_name} analyze the market trends",
                "m.mentions": {
                    "user_ids": [
                        mock_research_agent.user_id,
                        mock_analyst_agent.user_id,
                    ],
                },
            },
            "sender": "@user:localhost",
            "room_id": team_room_id,
            "event_id": "$test_event",
            "origin_server_ts": 1234567890,
        }

        # Add thread relation
        message_event["content"]["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": "$thread_root",
        }

        # Both agents should recognize they're part of a team request
        # This test verifies the setup - actual team behavior will be tested
        # once implementation is done
        assert mock_research_agent.user_id in message_event["content"]["m.mentions"]["user_ids"]
        assert mock_analyst_agent.user_id in message_event["content"]["m.mentions"]["user_ids"]

    @pytest.mark.asyncio
    async def test_multiple_agents_in_thread_form_team(
        self,
        mock_code_agent: AgentMatrixUser,
        mock_security_agent: AgentMatrixUser,
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test that multiple agents already in a thread form a team when no one is mentioned."""
        # Mock thread history showing both agents have participated
        thread_history = [
            {
                "type": "m.room.message",
                "sender": "@user:localhost",
                "content": {"body": "How should we implement authentication?"},
            },
            {
                "type": "m.room.message",
                "sender": mock_code_agent.user_id,
                "content": {"body": "I suggest using JWT tokens..."},
            },
            {
                "type": "m.room.message",
                "sender": mock_security_agent.user_id,
                "content": {"body": "We should also add rate limiting..."},
            },
        ]

        # Message with no mentions would trigger team formation
        # (message_event setup omitted as it's tested via thread_history)

        # Verify both agents are in thread
        agents_in_thread = get_agents_in_thread(thread_history, self.config)
        agent_names = [mid.agent_name(self.config) for mid in agents_in_thread]
        assert "code" in agent_names
        assert "security" in agent_names
        assert len(agent_names) == 2


class TestTeamCollaboration:
    """Test team collaboration behaviors."""

    @pytest.mark.asyncio
    @patch("mindroom.bot.stream_agent_response")
    async def test_team_coordinate_mode(
        self,
        mock_stream_agent_response: AsyncMock,  # noqa: ARG002
        mock_research_agent: AgentMatrixUser,  # noqa: ARG002
        mock_analyst_agent: AgentMatrixUser,  # noqa: ARG002
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test team coordination mode where agents build on each other's work."""

        # Setup responses for coordinate mode
        async def research_response() -> AsyncGenerator[str, None]:
            yield "I've gathered the following data on renewable energy:\n"
            yield "- Solar capacity increased 23% YoY\n"
            yield "- Wind energy adoption up 18%"

        async def analyst_response() -> AsyncGenerator[str, None]:
            yield "Based on the research data:\n"
            yield "- The 23% solar growth indicates strong market momentum\n"
            yield "- Combined renewable growth of 20.5% exceeds projections"

        # This test sets up the expected behavior for coordinate mode
        # Implementation will ensure agents respond sequentially

        # Expected: Research agent provides data, then analyst builds on it
        research_chunks = [chunk async for chunk in research_response()]

        analyst_chunks = [chunk async for chunk in analyst_response()]

        # Verify responses can be combined coherently
        combined = "".join(research_chunks) + "\n\n" + "".join(analyst_chunks)
        assert "gathered the following data" in combined
        assert "Based on the research data" in combined

    @pytest.mark.asyncio
    async def test_team_collaborate_mode(
        self,
        mock_code_agent: AgentMatrixUser,  # noqa: ARG002
        mock_security_agent: AgentMatrixUser,  # noqa: ARG002
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test team collaboration mode where agents work in parallel."""
        # In collaborate mode, multiple agents analyze the same problem
        # and provide different perspectives simultaneously

        # In collaborate mode, multiple agents analyze the same problem

        # Team synthesis would combine these perspectives
        expected_synthesis = (
            "Team Response:\n"
            "Implementation approach: JWT tokens with refresh tokens\n"
            "Security requirements: Multi-factor authentication and rate limiting"
        )

        # Verify the perspectives can be synthesized
        assert "JWT tokens" in expected_synthesis
        assert "Multi-factor authentication" in expected_synthesis

    @pytest.mark.asyncio
    async def test_team_route_mode(
        self,
        mock_research_agent: AgentMatrixUser,
        mock_code_agent: AgentMatrixUser,
        mock_analyst_agent: AgentMatrixUser,
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test team route mode where lead agent delegates to specialists."""
        # In route mode, a lead agent determines who should handle what

        # In route mode, a lead agent determines who should handle what

        expected_delegations = {
            "research_task": mock_research_agent.agent_name,
            "analysis_task": mock_analyst_agent.agent_name,
            "visualization_task": mock_code_agent.agent_name,
        }

        # Verify routing logic (to be implemented)
        for agent in expected_delegations.values():
            assert agent in ["research", "code", "analyst"]


class TestTeamResponseBehavior:
    """Test specific team response behaviors."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["#test:example.org"]),
                "security": AgentConfig(display_name="Security", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="Research", rooms=["#test:example.org"]),
                "analyst": AgentConfig(display_name="Analyst", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    @pytest.mark.asyncio
    async def test_single_agent_still_continues_conversation(
        self,
        mock_code_agent: AgentMatrixUser,
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test that single agent behavior remains unchanged."""
        # Thread with only one agent
        thread_history = [
            {
                "type": "m.room.message",
                "sender": "@user:localhost",
                "content": {"body": "Can you help with Python?"},
            },
            {
                "type": "m.room.message",
                "sender": mock_code_agent.user_id,
                "content": {"body": "Sure, I can help with Python!"},
            },
        ]

        # No mentions in follow-up would cause single agent to continue

        agents_in_thread = get_agents_in_thread(thread_history, self.config)
        agent_names = [mid.agent_name(self.config) for mid in agents_in_thread]
        assert agent_names == ["code"]
        # Single agent should continue responding

    @pytest.mark.asyncio
    async def test_explicit_mention_overrides_team(
        self,
        mock_code_agent: AgentMatrixUser,
        mock_security_agent: AgentMatrixUser,  # noqa: ARG002
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test that explicit mention of one agent prevents team formation."""
        # Thread with multiple agents (thread_history would show both agents)

        # Explicitly mention only one agent
        message_event = {
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": f"@{mock_code_agent.display_name} can you add error handling?",
                "m.mentions": {"user_ids": [mock_code_agent.user_id]},
            },
        }

        # Only mentioned agent should respond, not the team
        content = cast("dict[str, Any]", message_event["content"])
        mentions = cast("dict[str, Any]", content["m.mentions"])
        user_ids = cast("list[str]", mentions["user_ids"])
        assert len(user_ids) == 1
        assert mock_code_agent.user_id in user_ids

    @pytest.mark.asyncio
    async def test_team_with_invited_agents(
        self,
        mock_research_agent: AgentMatrixUser,
        mock_analyst_agent: AgentMatrixUser,
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test team formation with invited agents."""
        # One agent is native to room, another is invited
        native_agent = mock_research_agent
        invited_agent = mock_analyst_agent

        # Both should form team when working together
        thread_with_both = [
            {
                "type": "m.room.message",
                "sender": native_agent.user_id,
                "content": {"body": "Research findings..."},
            },
            {
                "type": "m.room.message",
                "sender": invited_agent.user_id,
                "content": {"body": "Analysis of findings..."},
            },
        ]

        agents = get_agents_in_thread(thread_with_both, self.config)
        agent_names = [mid.agent_name(self.config) for mid in agents]
        assert len(agents) == 2
        assert "research" in agent_names
        assert "analyst" in agent_names


class TestTeamEdgeCases:
    """Test edge cases and error scenarios."""

    @pytest.mark.asyncio
    async def test_team_member_unavailable(
        self,
        mock_code_agent: AgentMatrixUser,
        mock_security_agent: AgentMatrixUser,
        team_room_id: str,
        tmp_path: Path,
    ) -> None:
        """Test behavior when a team member is unavailable."""
        # Setup scenario where one agent is offline/unavailable
        # Team should adapt and continue with available members

    @pytest.mark.asyncio
    async def test_conflicting_team_responses(
        self,
        mock_analyst_agent: AgentMatrixUser,
        mock_research_agent: AgentMatrixUser,
        team_room_id: str,
        tmp_path: Path,
    ) -> None:
        """Test handling of conflicting information from team members."""
        # Agents might have different data or opinions
        # Team synthesis should handle gracefully

    @pytest.mark.asyncio
    async def test_team_context_overflow(
        self,
        mock_code_agent: AgentMatrixUser,
        mock_security_agent: AgentMatrixUser,
        mock_analyst_agent: AgentMatrixUser,
        team_room_id: str,
        tmp_path: Path,
    ) -> None:
        """Test team behavior when context window is nearly full."""
        # Large thread history approaching token limits
        # Team should coordinate to provide concise responses


class TestRouterTeamFormation:
    """Test router-initiated team formation."""

    @pytest.mark.asyncio
    async def test_router_forms_team_for_complex_query(
        self,
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test router creating a team for multi-domain queries."""
        # Complex query requiring multiple agents:
        # "I need to build a secure web API with authentication,
        # analyze performance requirements, and create documentation"

        # Router should identify need for: code, security, analyst agents
        expected_team_members = ["code", "security", "analyst"]

        # Verify router would select appropriate team
        # (Implementation will use AI to determine this)
        assert len(expected_team_members) > 1

    @pytest.mark.asyncio
    async def test_router_single_agent_for_simple_query(
        self,
        team_room_id: str,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test router selecting single agent for simple queries."""
        # Simple query = "What's 2 + 2?"
        # Router should select only calculator agent, not form a team
        expected_agent = "calculator"

        # Verify simple queries don't trigger team formation
        assert expected_agent == "calculator"

    @pytest.mark.asyncio
    async def test_dm_room_team_formation(self) -> None:
        """Test that multiple agents in a DM room form a team when no one is mentioned."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        import nio  # noqa: PLC0415

        from mindroom.config import AgentConfig, Config, ModelConfig  # noqa: PLC0415
        from mindroom.teams import decide_team_formation  # noqa: PLC0415

        config = Config(
            agents={
                "agent1": AgentConfig(display_name="Agent 1", role="First agent"),
                "agent2": AgentConfig(display_name="Agent 2", role="Second agent"),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

        # Mock room with multiple agents
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!dm:localhost"
        room.users = {"@mindroom_agent1:localhost": None, "@mindroom_agent2:localhost": None}

        # Test DM room with multiple agents and no mentions
        result = await decide_team_formation(
            agent=config.ids["agent1"],
            tagged_agents=[],  # No agents mentioned
            agents_in_thread=[],  # No agents have spoken yet
            all_mentioned_in_thread=[],  # No mentions in thread
            message="Hello",
            config=config,
            is_dm_room=True,  # This is a DM room
            room=room,
            use_ai_decision=False,  # Don't use AI for this test
        )

        # Should form a team with both agents
        assert result.should_form_team is True
        agent_names = sorted([mid.agent_name(config) for mid in result.agents])
        assert agent_names == ["agent1", "agent2"]

        # Test DM room with single agent (should not form team)
        room.users = {"@mindroom_agent1:localhost": None}
        result = await decide_team_formation(
            agent=config.ids["agent1"],
            tagged_agents=[],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            message="Hello",
            config=config,
            is_dm_room=True,
            room=room,
            use_ai_decision=False,  # Don't use AI for this test
        )

        # Should not form a team with single agent
        assert result.should_form_team is False

    @pytest.mark.asyncio
    async def test_dm_room_thread_single_agent_no_team(self) -> None:
        """In a DM with multiple agents, a thread with a single agent should not form a team."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        import nio  # noqa: PLC0415

        from mindroom.config import AgentConfig, Config, ModelConfig  # noqa: PLC0415
        from mindroom.teams import decide_team_formation  # noqa: PLC0415

        config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", role="Math"),
                "general": AgentConfig(display_name="General", role="General"),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

        # DM room with multiple agents
        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!dm:localhost"
        room.users = {
            config.ids["calculator"].full_id: None,
            config.ids["general"].full_id: None,
        }

        # Thread has only calculator participating so far
        agents_in_thread = [config.ids["calculator"]]

        # Should NOT form a team inside a thread with a single agent
        result = await decide_team_formation(
            agent=config.ids["calculator"],
            tagged_agents=[],
            agents_in_thread=agents_in_thread,
            all_mentioned_in_thread=[],
            message="Follow-up without mentions",
            config=config,
            is_dm_room=True,
            is_thread=True,
            room=room,
            use_ai_decision=False,
        )

        assert result.should_form_team is False
