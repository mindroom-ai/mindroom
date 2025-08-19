"""Tests for agent order preservation in team formation."""
# ruff: noqa: ANN001, ANN201

from __future__ import annotations

import pytest

from mindroom.config import AgentConfig, Config, DefaultsConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.teams import TeamMode, should_form_team
from mindroom.thread_utils import (
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
    get_mentioned_agents,
)


@pytest.fixture
def mock_config():
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

    def test_get_mentioned_agents_preserves_order(self, mock_config):
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
        assert agents == ["phone", "email", "research"]

    def test_get_agents_in_thread_preserves_order(self, mock_config):
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
        assert agents == ["research", "email", "phone", "analyst"]

    def test_get_agents_in_thread_excludes_router(self, mock_config):
        """Test that router agent is excluded from thread participants."""
        thread_history = [
            {"sender": "@mindroom_email:localhost", "content": {"body": "Email"}},
            {"sender": f"@mindroom_{ROUTER_AGENT_NAME}:localhost", "content": {"body": "Routing"}},
            {"sender": "@mindroom_phone:localhost", "content": {"body": "Phone"}},
        ]

        agents = get_agents_in_thread(thread_history, mock_config)

        # Router should be excluded
        assert agents == ["email", "phone"]
        assert ROUTER_AGENT_NAME not in agents

    def test_get_all_mentioned_agents_preserves_order(self, mock_config):
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
        assert agents == ["phone", "email", "research", "analyst"]

    def test_no_duplicates_in_mentioned_agents(self, mock_config):
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
        assert agents == ["email", "phone", "research"]
        assert len(agents) == len(set(agents))  # No duplicates

    def test_empty_thread_returns_empty_list(self, mock_config):
        """Test that empty thread returns empty list."""
        assert get_agents_in_thread([], mock_config) == []
        assert get_all_mentioned_agents_in_thread([], mock_config) == []

    def test_order_matters_for_coordinate_mode(self, mock_config):
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
        assert agents1 == ["email", "phone"]
        assert agents2 == ["phone", "email"]
        assert agents1 != agents2  # Order matters!


class TestIntegrationWithTeamFormation:
    """Test integration with team formation to ensure order flows through."""

    @pytest.mark.asyncio
    async def test_coordinate_mode_respects_order(self, mock_config) -> None:
        """Test that coordinate mode will execute agents in the preserved order."""
        # When agents are tagged in specific order
        tagged_agents = ["phone", "email", "research"]  # User tagged in this order

        result = await should_form_team(
            tagged_agents=tagged_agents,
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=None,  # type: ignore[assignment]
            message="Call me, then email the details, then research more info",
            config=mock_config,
            use_ai_decision=False,  # Use hardcoded logic for predictable test
        )

        # Agents should be in the same order as tagged
        assert result.agents == ["phone", "email", "research"]
        assert result.mode == TeamMode.COORDINATE  # Multiple tagged = coordinate

        # This order should flow through to team execution
        # meaning phone agent acts first, then email, then research
