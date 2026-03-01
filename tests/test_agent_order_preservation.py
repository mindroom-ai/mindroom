"""Tests for agent order preservation in team formation."""

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.teams import TeamMode, decide_team_formation
from mindroom.thread_utils import (
    check_agent_mentioned,
    get_agents_in_thread,
    get_all_mentioned_agents_in_thread,
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

    def test_check_agent_mentioned_preserves_order(self, mock_config: Config) -> None:
        """Test that check_agent_mentioned preserves the order from user_ids."""
        domain = mock_config.domain
        event_source = {
            "content": {
                "m.mentions": {
                    "user_ids": [
                        f"@mindroom_phone:{domain}",
                        f"@mindroom_email:{domain}",
                        f"@mindroom_research:{domain}",
                    ],
                },
            },
        }

        agents, _, _ = check_agent_mentioned(event_source, None, mock_config)

        # Order should be preserved as phone, email, research
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["phone", "email", "research"]

    def test_get_agents_in_thread_preserves_order(self, mock_config: Config) -> None:
        """Test that get_agents_in_thread preserves order of first participation."""
        domain = mock_config.domain
        thread_history = [
            {"sender": f"@mindroom_research:{domain}", "content": {"body": "Starting research"}},
            {"sender": f"@mindroom_email:{domain}", "content": {"body": "Sending email"}},
            {"sender": f"@mindroom_phone:{domain}", "content": {"body": "Making call"}},
            {"sender": f"@mindroom_email:{domain}", "content": {"body": "Another email"}},  # Duplicate
            {"sender": f"@mindroom_analyst:{domain}", "content": {"body": "Analyzing"}},
        ]

        agents = get_agents_in_thread(thread_history, mock_config)

        # Order should be: research, email, phone, analyst (in order of first appearance)
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["research", "email", "phone", "analyst"]

    def test_get_agents_in_thread_excludes_router(self, mock_config: Config) -> None:
        """Test that router agent is excluded from thread participants."""
        domain = mock_config.domain
        thread_history = [
            {"sender": f"@mindroom_email:{domain}", "content": {"body": "Email"}},
            {"sender": f"@mindroom_{ROUTER_AGENT_NAME}:{domain}", "content": {"body": "Routing"}},
            {"sender": f"@mindroom_phone:{domain}", "content": {"body": "Phone"}},
        ]

        agents = get_agents_in_thread(thread_history, mock_config)

        # Router should be excluded
        # Convert MatrixID objects to agent names for comparison
        agent_names = [mid.agent_name(mock_config) for mid in agents]
        assert agent_names == ["email", "phone"]
        assert ROUTER_AGENT_NAME not in agent_names

    def test_get_all_mentioned_agents_preserves_order(self, mock_config: Config) -> None:
        """Test that get_all_mentioned_agents_in_thread preserves order of first mention."""
        domain = mock_config.domain
        thread_history = [
            {
                "content": {
                    "body": "First message",
                    "m.mentions": {
                        "user_ids": [f"@mindroom_phone:{domain}", f"@mindroom_email:{domain}"],
                    },
                },
            },
            {
                "content": {
                    "body": "Second message",
                    "m.mentions": {
                        "user_ids": [f"@mindroom_research:{domain}", f"@mindroom_phone:{domain}"],  # phone is duplicate
                    },
                },
            },
            {
                "content": {
                    "body": "Third message",
                    "m.mentions": {
                        "user_ids": [f"@mindroom_analyst:{domain}", f"@mindroom_email:{domain}"],  # email is duplicate
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
        domain = mock_config.domain
        thread_history = [
            {
                "content": {
                    "body": "Message 1",
                    "m.mentions": {
                        "user_ids": [
                            f"@mindroom_email:{domain}",
                            f"@mindroom_phone:{domain}",
                            f"@mindroom_email:{domain}",
                        ],
                    },
                },
            },
            {
                "content": {
                    "body": "Message 2",
                    "m.mentions": {
                        "user_ids": [
                            f"@mindroom_phone:{domain}",
                            f"@mindroom_research:{domain}",
                            f"@mindroom_email:{domain}",
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
        domain = mock_config.domain
        event_source1 = {
            "content": {
                "m.mentions": {
                    "user_ids": [f"@mindroom_email:{domain}", f"@mindroom_phone:{domain}"],
                },
            },
        }
        event_source2 = {
            "content": {
                "m.mentions": {
                    "user_ids": [f"@mindroom_phone:{domain}", f"@mindroom_email:{domain}"],
                },
            },
        }

        agents1, _, _ = check_agent_mentioned(event_source1, None, mock_config)
        agents2, _, _ = check_agent_mentioned(event_source2, None, mock_config)

        # Different orders should be preserved
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
