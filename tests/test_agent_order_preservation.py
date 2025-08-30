"""Tests for agent order preservation in team formation."""

from __future__ import annotations

import pytest

from mindroom.config import AgentConfig, Config, DefaultsConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.teams import TeamMode, decide_team_formation
from mindroom.thread_utils import (
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_mentioned_agents,
)


@pytest.fixture
def mock_config() -> Config:
    """Create a mock config for testing."""
    return Config(
        defaults=DefaultsConfig(),
        agents={
            "email": AgentConfig(
                display_name="EmailAgent",
                role="Send emails",
                tools=["email"],
                instructions=[],
                rooms=[],
                model="default",
            ),
            "phone": AgentConfig(
                display_name="PhoneAgent",
                role="Make phone calls",
                tools=["phone"],
                instructions=[],
                rooms=[],
                model="default",
            ),
            "research": AgentConfig(
                display_name="ResearchAgent",
                role="Research information",
                tools=["search"],
                instructions=[],
                rooms=[],
                model="default",
            ),
            "analyst": AgentConfig(
                display_name="AnalystAgent",
                role="Analyze data",
                tools=["calculator"],
                instructions=[],
                rooms=[],
                model="default",
            ),
        },
    )


class TestAgentOrderPreservation:
    """Test that agent order is preserved in various functions."""

    def test_get_mentioned_agents_preserves_order(self, mock_config: Config) -> None:
        """Test that get_mentioned_agents preserves the order from user_ids."""
        mentions = {
            "user_ids": [
                "@mindroom_phone:localhost",
                "@mindroom_email:localhost",
                "@mindroom_research:localhost",
            ],
        }

        agents = get_mentioned_agents(mentions, mock_config)

        # Order should be preserved as phone, email, research
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["phone", "email", "research"]

    def test_get_agents_in_thread_preserves_order(self, mock_config: Config) -> None:
        """Test that get_agents_in_thread preserves order of first participation."""
        thread_history = [
            {"sender": "@mindroom_research:localhost", "content": {"body": "Starting research"}},
            {"sender": "@mindroom_email:localhost", "content": {"body": "Sending email"}},
            {"sender": "@mindroom_phone:localhost", "content": {"body": "Making call"}},
            {"sender": "@mindroom_email:localhost", "content": {"body": "Another email"}},  # Duplicate
            {"sender": "@mindroom_analyst:localhost", "content": {"body": "Analyzing"}},
        ]

        agents = get_agents_in_thread(thread_history, mock_config)

        # Order should be: research, email, phone, analyst (in order of first appearance)
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["research", "email", "phone", "analyst"]

    def test_get_agents_in_thread_excludes_router(self, mock_config: Config) -> None:
        """Test that router agent is excluded from thread participants."""
        thread_history = [
            {"sender": "@mindroom_email:localhost", "content": {"body": "Email"}},
            {"sender": f"@mindroom_{ROUTER_AGENT_NAME}:localhost", "content": {"body": "Routing"}},
            {"sender": "@mindroom_phone:localhost", "content": {"body": "Phone"}},
        ]

        agents = get_agents_in_thread(thread_history, mock_config)

        # Router should be excluded
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["email", "phone"]
        assert ROUTER_AGENT_NAME not in agent_names

    def test_get_all_mentioned_agents_preserves_order(self, mock_config: Config) -> None:
        """Test that get_all_mentioned_agents_in_thread preserves order of first mention."""
        thread_history = [
            {
                "content": {
                    "body": "First message",
                    "m.mentions": {
                        "user_ids": ["@mindroom_phone:localhost", "@mindroom_email:localhost"],
                    },
                },
            },
            {
                "content": {
                    "body": "Second message",
                    "m.mentions": {
                        "user_ids": ["@mindroom_research:localhost", "@mindroom_phone:localhost"],  # phone is duplicate
                    },
                },
            },
            {
                "content": {
                    "body": "Third message",
                    "m.mentions": {
                        "user_ids": ["@mindroom_analyst:localhost", "@mindroom_email:localhost"],  # email is duplicate
                    },
                },
            },
        ]

        agents = get_all_mentioned_agents_in_thread(thread_history, mock_config)

        # Order should be: phone, email, research, analyst (in order of first mention)
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["phone", "email", "research", "analyst"]

    def test_no_duplicates_in_mentioned_agents(self, mock_config: Config) -> None:
        """Test that duplicates are removed while preserving order."""
        thread_history = [
            {
                "content": {
                    "body": "Message 1",
                    "m.mentions": {
                        "user_ids": [
                            "@mindroom_email:localhost",
                            "@mindroom_phone:localhost",
                            "@mindroom_email:localhost",
                        ],
                    },
                },
            },
            {
                "content": {
                    "body": "Message 2",
                    "m.mentions": {
                        "user_ids": [
                            "@mindroom_phone:localhost",
                            "@mindroom_research:localhost",
                            "@mindroom_email:localhost",
                        ],
                    },
                },
            },
        ]

        agents = get_all_mentioned_agents_in_thread(thread_history, mock_config)

        # Should have no duplicates, order preserved from first mention
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["email", "phone", "research"]
        assert len(agent_names) == len(set(agent_names))  # No duplicates

    def test_empty_thread_returns_empty_list(self, mock_config: Config) -> None:
        """Test that empty thread returns empty list."""
        assert get_agents_in_thread([], mock_config) == []
        assert get_all_mentioned_agents_in_thread([], mock_config) == []

    def test_order_matters_for_coordinate_mode(self, mock_config: Config) -> None:
        """Test that order preservation is important for sequential execution."""
        # Simulate a user message: "@email @phone Send details then call"
        mentions_order1 = {
            "user_ids": ["@mindroom_email:localhost", "@mindroom_phone:localhost"],
        }

        # Simulate a different order: "@phone @email Call then send details"
        mentions_order2 = {
            "user_ids": ["@mindroom_phone:localhost", "@mindroom_email:localhost"],
        }

        agents1 = get_mentioned_agents(mentions_order1, mock_config)
        agents2 = get_mentioned_agents(mentions_order2, mock_config)

        # Different orders should be preserved
        # Convert MatrixID objects to agent names for comparison
        agent_names1 = [mid.agent_name(mock_config) for mid in agents1]
        agent_names2 = [mid.agent_name(mock_config) for mid in agents2]
        assert agent_names1 == ["email", "phone"]
        assert agent_names2 == ["phone", "email"]
        assert agent_names1 != agent_names2  # Order matters!


class TestIntegrationWithTeamFormation:
    """Test integration with team formation to ensure order flows through."""

    @pytest.mark.asyncio
    async def test_coordinate_mode_respects_order(self, mock_config: Config) -> None:
        """Test that coordinate mode will execute agents in the preserved order."""
        # When agents are tagged in specific order - use MatrixID objects
        tagged_agents = [
            mock_config.ids["phone"],
            mock_config.ids["email"],
            mock_config.ids["research"],
        ]  # User tagged in this order

        result = await decide_team_formation(
            agent=mock_config.ids["email"],  # The agent calling this function
            tagged_agents=tagged_agents,
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=None,
            message="Call me, then email the details, then research more info",
            config=mock_config,
            use_ai_decision=False,  # Use hardcoded logic for predictable test
        )

        # Agents should be in the same order as tagged
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in result.agents]
        assert agent_names == ["phone", "email", "research"]
        assert result.mode == TeamMode.COORDINATE  # Multiple tagged = coordinate

        # This order should flow through to team execution
        # meaning phone agent acts first, then email, then research
