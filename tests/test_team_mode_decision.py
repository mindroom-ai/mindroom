"""Tests for AI-powered team mode decision functionality."""
# ruff: noqa: ANN001, ANN201, F841, RET504

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config import AgentConfig, Config, DefaultsConfig
from mindroom.teams import (
    TeamMode,
    TeamModeDecision,
    decide_team_formation,
    select_team_mode,
)


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config(
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
    return config


class TestTeamModeDecision:
    """Test the TeamModeDecision model."""

    def test_team_mode_decision_coordinate(self):
        """Test creating a coordinate mode decision."""
        decision = TeamModeDecision(
            mode="coordinate",
            reasoning="Tasks must be done sequentially",
        )
        assert decision.mode == "coordinate"
        assert decision.reasoning == "Tasks must be done sequentially"

    def test_team_mode_decision_collaborate(self):
        """Test creating a collaborate mode decision."""
        decision = TeamModeDecision(
            mode="collaborate",
            reasoning="Tasks can be done in parallel",
        )
        assert decision.mode == "collaborate"
        assert decision.reasoning == "Tasks can be done in parallel"


class TestDetermineTeamMode:
    """Test the AI-powered team mode determination."""

    @pytest.mark.asyncio
    async def test_select_team_mode_coordinate(self, mock_config):
        """Test AI correctly identifies coordination tasks (different subtasks)."""
        with patch("mindroom.teams.get_model_instance") as mock_get_model:
            # Mock the AI agent response
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = TeamModeDecision(
                mode="coordinate",
                reasoning="Different agents handle different subtasks",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await select_team_mode(
                    "Send me an email then call me",
                    ["email", "phone"],
                    mock_config,
                )

                assert result == TeamMode.COORDINATE
                mock_agent.arun.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_team_mode_collaborate(self, mock_config):
        """Test AI correctly identifies collaboration tasks (same task for all)."""
        with patch("mindroom.teams.get_model_instance") as mock_get_model:
            # Mock the AI agent response
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = TeamModeDecision(
                mode="collaborate",
                reasoning="All agents work on the same brainstorming task",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await select_team_mode(
                    "What do you think about this idea?",
                    ["research", "analyst"],
                    mock_config,
                )

                assert result == TeamMode.COLLABORATE
                mock_agent.arun.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_team_mode_fallback_on_error(self, mock_config):
        """Test fallback to collaborate mode on AI error."""
        with patch("mindroom.teams.get_model_instance") as mock_get_model:
            # Mock the AI agent to raise an error
            mock_agent = AsyncMock()
            mock_agent.arun.side_effect = Exception("AI service unavailable")

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await select_team_mode(
                    "Do something",
                    ["email", "phone"],
                    mock_config,
                )

                # Should fallback to COLLABORATE on error
                assert result == TeamMode.COLLABORATE

    @pytest.mark.asyncio
    async def test_select_team_mode_unexpected_response(self, mock_config):
        """Test fallback when AI returns unexpected response type."""
        with patch("mindroom.teams.get_model_instance") as mock_get_model:
            # Mock the AI agent response with wrong type
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = "Just a string, not TeamModeDecision"
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await select_team_mode(
                    "Do something",
                    ["email", "phone"],
                    mock_config,
                )

                # Should fallback to COLLABORATE on unexpected response
                assert result == TeamMode.COLLABORATE


class TestShouldFormTeam:
    """Test the enhanced decide_team_formation function."""

    @pytest.mark.asyncio
    async def test_decide_team_formation_with_ai_decision(self, mock_config):
        """Test team formation with AI mode decision."""
        with patch("mindroom.teams.select_team_mode") as mock_determine:
            mock_determine.return_value = TeamMode.COORDINATE

            result = await decide_team_formation(
                agent=mock_config.ids["email"],
                tagged_agents=[mock_config.ids["email"], mock_config.ids["phone"]],
                agents_in_thread=[],
                all_mentioned_in_thread=[],
                room=MagicMock(spec=nio.MatrixRoom),
                message="Send email then call",
                config=mock_config,
                use_ai_decision=True,
            )

            assert result.should_form_team is True
            assert [mid.agent_name(mock_config) for mid in result.agents] == ["email", "phone"]
            assert result.mode == TeamMode.COORDINATE
            mock_determine.assert_called_once_with(
                "Send email then call",
                ["email", "phone"],
                mock_config,
            )

    @pytest.mark.asyncio
    async def test_decide_team_formation_without_ai_decision(self, mock_config):
        """Test team formation with hardcoded mode selection."""
        result = await decide_team_formation(
            agent=mock_config.ids["email"],
            tagged_agents=[mock_config.ids["email"], mock_config.ids["phone"]],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=MagicMock(spec=nio.MatrixRoom),
            message="Send email then call",
            config=mock_config,
            use_ai_decision=False,
        )

        assert result.should_form_team is True
        assert [mid.agent_name(mock_config) for mid in result.agents] == ["email", "phone"]
        # Hardcoded logic: multiple tagged agents = COORDINATE
        assert result.mode == TeamMode.COORDINATE

    @pytest.mark.asyncio
    async def test_decide_team_formation_no_message_fallback(self, mock_config):
        """Test fallback to hardcoded logic when message is None."""
        result = await decide_team_formation(
            agent=mock_config.ids["email"],
            tagged_agents=[mock_config.ids["email"], mock_config.ids["phone"]],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=MagicMock(spec=nio.MatrixRoom),
            message=None,  # No message provided
            config=mock_config,
            use_ai_decision=True,
        )

        assert result.should_form_team is True
        assert [mid.agent_name(mock_config) for mid in result.agents] == ["email", "phone"]
        # Should use hardcoded logic when message is None
        assert result.mode == TeamMode.COORDINATE

    @pytest.mark.asyncio
    async def test_decide_team_formation_no_config_fallback(self, mock_config):
        """Test fallback to hardcoded logic when config is None."""
        result = await decide_team_formation(
            agent=mock_config.ids["email"],
            tagged_agents=[mock_config.ids["email"], mock_config.ids["phone"]],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=MagicMock(spec=nio.MatrixRoom),
            message="Send email then call",
            config=None,  # No config provided
            use_ai_decision=True,
        )

        assert result.should_form_team is True
        assert [mid.agent_name(mock_config) for mid in result.agents] == ["email", "phone"]
        # Should use hardcoded logic when config is None
        assert result.mode == TeamMode.COORDINATE

    @pytest.mark.asyncio
    async def test_decide_team_formation_no_team_needed(self, mock_config):
        """Test when no team formation is needed."""
        result = await decide_team_formation(
            agent=mock_config.ids["email"],
            tagged_agents=[mock_config.ids["email"]],  # Only one agent
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=MagicMock(spec=nio.MatrixRoom),
            message="Send an email",
            config=None,
            use_ai_decision=True,
        )

        assert result.should_form_team is False
        assert result.agents == []
        assert result.mode == TeamMode.COLLABORATE

    @pytest.mark.asyncio
    async def test_decide_team_formation_thread_agents(self, mock_config):
        """Test team formation with agents from thread history."""
        with patch("mindroom.teams.select_team_mode") as mock_determine:
            mock_determine.return_value = TeamMode.COLLABORATE

            result = await decide_team_formation(
                agent=mock_config.ids["analyst"],
                tagged_agents=[],
                agents_in_thread=[mock_config.ids["research"], mock_config.ids["analyst"]],
                all_mentioned_in_thread=[],
                room=MagicMock(spec=nio.MatrixRoom),
                message="Continue the analysis",
                config=mock_config,
                use_ai_decision=True,
            )

            assert result.should_form_team is True
            assert [mid.agent_name(mock_config) for mid in result.agents] == ["research", "analyst"]
            assert result.mode == TeamMode.COLLABORATE

    @pytest.mark.asyncio
    async def test_decide_team_formation_mentioned_agents(self, mock_config):
        """Test team formation with previously mentioned agents."""
        with patch("mindroom.teams.select_team_mode") as mock_determine:
            mock_determine.return_value = TeamMode.COLLABORATE

            result = await decide_team_formation(
                agent=mock_config.ids["email"],
                tagged_agents=[],
                agents_in_thread=[],
                all_mentioned_in_thread=[
                    mock_config.ids["email"],
                    mock_config.ids["phone"],
                    mock_config.ids["research"],
                ],
                room=MagicMock(spec=nio.MatrixRoom),
                message="Let's continue",
                config=mock_config,
                use_ai_decision=True,
            )

            assert result.should_form_team is True
            assert [mid.agent_name(mock_config) for mid in result.agents] == ["email", "phone", "research"]
            assert result.mode == TeamMode.COLLABORATE


class TestIntegrationScenarios:
    """Test real-world integration scenarios."""

    @pytest.mark.asyncio
    async def test_email_then_call_scenario(self, mock_config):
        """Test the email-then-call scenario - coordinate mode for different tasks."""
        with patch("mindroom.teams.get_model_instance") as mock_get_model:
            # Mock the AI to recognize different subtasks
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = TeamModeDecision(
                mode="coordinate",
                reasoning="Different tasks: email agent sends email, phone agent makes call",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await decide_team_formation(
                    agent=mock_config.ids["email"],
                    tagged_agents=[mock_config.ids["email"], mock_config.ids["phone"]],
                    agents_in_thread=[],
                    all_mentioned_in_thread=[],
                    room=MagicMock(spec=nio.MatrixRoom),
                    message="Email me the details, then call me to discuss",
                    config=mock_config,
                    use_ai_decision=True,
                )

                assert result.should_form_team is True
                assert result.mode == TeamMode.COORDINATE
                assert {mid.agent_name(mock_config) for mid in result.agents} == {"email", "phone"}

    @pytest.mark.asyncio
    async def test_brainstorming_scenario(self, mock_config):
        """Test brainstorming scenario - collaborate mode for same task."""
        with patch("mindroom.teams.get_model_instance") as mock_get_model:
            # Mock the AI to recognize same task for all
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = TeamModeDecision(
                mode="collaborate",
                reasoning="All agents provide their perspective on the same question",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await decide_team_formation(
                    agent=mock_config.ids["analyst"],
                    tagged_agents=[mock_config.ids["research"], mock_config.ids["analyst"]],
                    agents_in_thread=[],
                    all_mentioned_in_thread=[],
                    room=MagicMock(spec=nio.MatrixRoom),
                    message="What are your thoughts on this approach?",
                    config=mock_config,
                    use_ai_decision=True,
                )

                assert result.should_form_team is True
                assert result.mode == TeamMode.COLLABORATE
                assert {mid.agent_name(mock_config) for mid in result.agents} == {"research", "analyst"}

    @pytest.mark.asyncio
    async def test_backwards_compatibility(self, mock_config):
        """Test that the function still works with old call signature."""
        # Old code might call without message and config
        result = await decide_team_formation(
            agent=mock_config.ids["email"],
            tagged_agents=[mock_config.ids["email"], mock_config.ids["phone"]],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=MagicMock(spec=nio.MatrixRoom),
        )

        # Should still work with hardcoded logic
        assert result.should_form_team is True
        assert [mid.agent_name(mock_config) for mid in result.agents] == ["email", "phone"]
        assert result.mode == TeamMode.COORDINATE  # Hardcoded for multiple tagged
