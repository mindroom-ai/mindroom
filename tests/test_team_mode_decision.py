"""Tests for team-mode decision functionality.

Team formation is pure and assigns a heuristic provisional mode; the AI-powered
mode selection runs separately at execution time via ``select_ad_hoc_team_mode``.
"""
# ruff: noqa: ANN001, ANN201, F841

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.teams import (
    TeamMode,
    TeamOutcome,
    TeamResolution,
    _TeamModeDecision,
    decide_team_formation,
    select_ad_hoc_team_mode,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import actual_entity_usernames, entity_ids, entity_name_for_id, persist_entity_accounts


def _add_room_users(room: nio.MatrixRoom, user_ids: list[str]) -> None:
    room.members_synced = True
    for user_id in user_ids:
        room.add_member(user_id, None, None)


def _matrix_room(config: Config, room_id: str = "!room:localhost", user_ids: list[str] | None = None) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id, "@test-user:localhost")
    if user_ids is None:
        runtime_paths = runtime_paths_for(config)
        user_ids = [agent_id.full_id for agent_id in entity_ids(config, runtime_paths).values() if agent_id is not None]
    _add_room_users(room, user_ids)
    return room


async def _select_team_mode_for_test(message: str, agent_names: list[str], config: Config) -> TeamMode:
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    return await select_ad_hoc_team_mode(message, [ids[name] for name in agent_names], config, runtime_paths)


def decide_team_formation_for_test(**kwargs: object) -> TeamResolution:
    """Run team-formation logic with the test config's bound runtime context."""
    config = kwargs.get("config")
    runtime_paths = kwargs.get("runtime_paths")
    if runtime_paths is None:
        if not isinstance(config, Config):
            msg = "config or runtime_paths is required"
            raise TypeError(msg)
        runtime_paths = runtime_paths_for(config)
    kwargs["runtime_paths"] = runtime_paths
    room = kwargs.get("room")
    if isinstance(config, Config) and isinstance(room, nio.MatrixRoom) and not room.users:
        # Team-formation tests should default to all configured agents being visible in the room.
        _add_room_users(
            room,
            [agent_id.full_id for agent_id in entity_ids(config, runtime_paths).values() if agent_id is not None],
        )
    return decide_team_formation(**kwargs)


def _agent_names(ids: list[object], config: Config) -> list[str]:
    runtime_paths = runtime_paths_for(config)
    return [entity_name_for_id(mid, config, runtime_paths) for mid in ids]


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
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
                    tools=["twilio"],
                    instructions=[],
                    rooms=[],
                    model="default",
                ),
                "research": AgentConfig(
                    display_name="ResearchAgent",
                    role="Research information",
                    tools=["duckduckgo"],
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
        ),
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames=actual_entity_usernames(config),
    )
    return config


class TestTeamModeDecision:
    """Test the TeamModeDecision model."""

    def test_team_mode_decision_coordinate(self):
        """Test creating a coordinate mode decision."""
        decision = _TeamModeDecision(
            mode="coordinate",
            reasoning="Tasks must be done sequentially",
        )
        assert decision.mode == "coordinate"
        assert decision.reasoning == "Tasks must be done sequentially"

    def test_team_mode_decision_collaborate(self):
        """Test creating a collaborate mode decision."""
        decision = _TeamModeDecision(
            mode="collaborate",
            reasoning="Tasks can be done in parallel",
        )
        assert decision.mode == "collaborate"
        assert decision.reasoning == "Tasks can be done in parallel"


class TestSelectAdHocTeamMode:
    """Test the AI-powered execution-time team mode selection."""

    @pytest.mark.asyncio
    async def test_select_team_mode_coordinate(self, mock_config):
        """Test AI correctly identifies coordination tasks (different subtasks)."""
        with patch("mindroom.model_loading.get_model_instance") as mock_get_model:
            # Mock the AI agent response
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = _TeamModeDecision(
                mode="coordinate",
                reasoning="Different agents handle different subtasks",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await _select_team_mode_for_test(
                    "Send me an email then call me",
                    ["email", "phone"],
                    mock_config,
                )

                assert result == TeamMode.COORDINATE
                mock_agent.arun.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_team_mode_collaborate(self, mock_config):
        """Test AI correctly identifies collaboration tasks (same task for all)."""
        with patch("mindroom.model_loading.get_model_instance") as mock_get_model:
            # Mock the AI agent response
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = _TeamModeDecision(
                mode="collaborate",
                reasoning="All agents work on the same brainstorming task",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await _select_team_mode_for_test(
                    "What do you think about this idea?",
                    ["research", "analyst"],
                    mock_config,
                )

                assert result == TeamMode.COLLABORATE
                mock_agent.arun.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_team_mode_fallback_on_error(self, mock_config):
        """Test fallback to collaborate mode on AI error."""
        with patch("mindroom.model_loading.get_model_instance") as mock_get_model:
            # Mock the AI agent to raise an error
            mock_agent = AsyncMock()
            mock_agent.arun.side_effect = Exception("AI service unavailable")

            with (
                patch("mindroom.teams.Agent", return_value=mock_agent),
                patch("mindroom.teams.logger") as mock_logger,
            ):
                result = await _select_team_mode_for_test(
                    "Do something",
                    ["email", "phone"],
                    mock_config,
                )

                # Should fallback to COLLABORATE on error
                assert result == TeamMode.COLLABORATE
                mock_logger.exception.assert_not_called()
                mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_team_mode_unexpected_response(self, mock_config):
        """Test fallback when AI returns unexpected response type."""
        with patch("mindroom.model_loading.get_model_instance") as mock_get_model:
            # Mock the AI agent response with wrong type
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = "Just a string, not TeamModeDecision"
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                result = await _select_team_mode_for_test(
                    "Do something",
                    ["email", "phone"],
                    mock_config,
                )

                # Should fallback to COLLABORATE on unexpected response
                assert result == TeamMode.COLLABORATE


class TestShouldFormTeam:
    """Test the pure decide_team_formation function."""

    def test_decide_team_formation_never_calls_ai(self, mock_config):
        """Formation is pure: it must never reach for the AI mode selector."""
        with patch(
            "mindroom.teams._select_team_mode",
            side_effect=AssertionError("formation must not call AI"),
        ):
            result = decide_team_formation_for_test(
                tagged_agents=[
                    entity_ids(mock_config, runtime_paths_for(mock_config))["email"],
                    entity_ids(mock_config, runtime_paths_for(mock_config))["phone"],
                ],
                agents_in_thread=[],
                all_mentioned_in_thread=[],
                room=_matrix_room(mock_config),
                config=mock_config,
            )

        assert result.outcome is TeamOutcome.TEAM
        assert _agent_names(result.eligible_members, mock_config) == ["email", "phone"]
        # Provisional heuristic: multiple tagged agents = COORDINATE
        assert result.mode == TeamMode.COORDINATE

    def test_decide_team_formation_no_config_fallback(self, mock_config):
        """Test team formation when config is None."""
        result = decide_team_formation_for_test(
            tagged_agents=[
                entity_ids(mock_config, runtime_paths_for(mock_config))["email"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["phone"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            config=None,  # No config provided
            runtime_paths=runtime_paths_for(mock_config),
        )

        assert result.outcome is TeamOutcome.TEAM
        assert _agent_names(result.eligible_members, mock_config) == ["email", "phone"]
        assert result.mode == TeamMode.COORDINATE

    def test_decide_team_formation_no_team_needed(self, mock_config):
        """Test when no team formation is needed."""
        result = decide_team_formation_for_test(
            tagged_agents=[entity_ids(mock_config, runtime_paths_for(mock_config))["email"]],  # Only one agent
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            config=None,
            runtime_paths=runtime_paths_for(mock_config),
        )

        assert result.outcome is TeamOutcome.NONE
        assert result.eligible_members == []
        assert result.mode is None

    def test_decide_team_formation_thread_agents(self, mock_config):
        """Test team formation with agents from thread history."""
        result = decide_team_formation_for_test(
            tagged_agents=[],
            agents_in_thread=[
                entity_ids(mock_config, runtime_paths_for(mock_config))["research"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["analyst"],
            ],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            config=mock_config,
        )

        assert result.outcome is TeamOutcome.TEAM
        assert _agent_names(result.eligible_members, mock_config) == ["research", "analyst"]
        # Provisional heuristic: no tagged agents = COLLABORATE
        assert result.mode == TeamMode.COLLABORATE

    def test_decide_team_formation_mentioned_agents(self, mock_config):
        """Test team formation with previously mentioned agents."""
        result = decide_team_formation_for_test(
            tagged_agents=[],
            agents_in_thread=[],
            all_mentioned_in_thread=[
                entity_ids(mock_config, runtime_paths_for(mock_config))["email"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["phone"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["research"],
            ],
            room=_matrix_room(mock_config),
            config=mock_config,
        )

        assert result.outcome is TeamOutcome.TEAM
        assert _agent_names(result.eligible_members, mock_config) == ["email", "phone", "research"]
        assert result.mode == TeamMode.COLLABORATE


class TestIntegrationScenarios:
    """Test formation plus execution-time mode refinement together."""

    @pytest.mark.asyncio
    async def test_email_then_call_scenario(self, mock_config):
        """Test the email-then-call scenario - coordinate mode for different tasks."""
        result = decide_team_formation_for_test(
            tagged_agents=[
                entity_ids(mock_config, runtime_paths_for(mock_config))["email"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["phone"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            config=mock_config,
        )
        assert result.outcome is TeamOutcome.TEAM
        assert set(_agent_names(result.eligible_members, mock_config)) == {"email", "phone"}

        with patch("mindroom.model_loading.get_model_instance") as mock_get_model:
            # Mock the AI to recognize different subtasks
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = _TeamModeDecision(
                mode="coordinate",
                reasoning="Different tasks: email agent sends email, phone agent makes call",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                mode = await select_ad_hoc_team_mode(
                    "Email me the details, then call me to discuss",
                    result.eligible_members,
                    mock_config,
                    runtime_paths_for(mock_config),
                )

        assert mode == TeamMode.COORDINATE

    @pytest.mark.asyncio
    async def test_brainstorming_scenario(self, mock_config):
        """Test brainstorming scenario - collaborate mode for same task."""
        result = decide_team_formation_for_test(
            tagged_agents=[
                entity_ids(mock_config, runtime_paths_for(mock_config))["research"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["analyst"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            config=mock_config,
        )
        assert result.outcome is TeamOutcome.TEAM
        assert set(_agent_names(result.eligible_members, mock_config)) == {"research", "analyst"}

        with patch("mindroom.model_loading.get_model_instance") as mock_get_model:
            # Mock the AI to recognize same task for all
            mock_agent = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = _TeamModeDecision(
                mode="collaborate",
                reasoning="All agents provide their perspective on the same question",
            )
            mock_agent.arun.return_value = mock_response

            with patch("mindroom.teams.Agent", return_value=mock_agent):
                mode = await select_ad_hoc_team_mode(
                    "What are your thoughts on this approach?",
                    result.eligible_members,
                    mock_config,
                    runtime_paths_for(mock_config),
                )

        assert mode == TeamMode.COLLABORATE

    def test_optional_config_defaults(self, mock_config):
        """Test that optional config input still falls back cleanly."""
        result = decide_team_formation_for_test(
            tagged_agents=[
                entity_ids(mock_config, runtime_paths_for(mock_config))["email"],
                entity_ids(mock_config, runtime_paths_for(mock_config))["phone"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            runtime_paths=runtime_paths_for(mock_config),
        )

        # Should still work with the provisional heuristic
        assert result.outcome is TeamOutcome.TEAM
        assert _agent_names(result.eligible_members, mock_config) == ["email", "phone"]
        assert result.mode == TeamMode.COORDINATE  # Heuristic for multiple tagged
