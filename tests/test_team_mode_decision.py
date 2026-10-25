"""Tests for team-mode decision functionality.

Team formation is pure and assigns a heuristic provisional mode; the AI-powered
mode selection runs separately at execution time via ``select_ad_hoc_team_mode``.
"""
# ruff: noqa: ANN001, ANN201

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.llm.errors import ProviderError
from mindroom.teams.formation import (
    TeamOutcome,
    TeamResolution,
    _TeamModeDecision,
    decide_team_formation,
    select_ad_hoc_team_mode,
)
from mindroom.teams.spec import TeamMode
from mindroom.usage.stats import collect_admin_usage
from tests.conftest import bind_runtime_paths, install_model_providers, runtime_paths_for, test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, usage
from tests.identity_helpers import actual_entity_usernames, entity_ids, entity_name_for_id, persist_entity_accounts

if TYPE_CHECKING:
    from mindroom.llm.usage import MessageMetrics


def _mode_model(mode: str, reasoning: str, *, metrics: MessageMetrics | None = None) -> ScriptedModel:
    """Return a model that answers the team-mode choice with ``mode``."""
    return ScriptedModel([answer(json.dumps({"mode": mode, "reasoning": reasoning}), metrics)])


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


def decide_team_formation_for_test(*, config: Config, room: nio.MatrixRoom, **kwargs: object) -> TeamResolution:
    """Run team-formation logic with the test config's bound runtime context."""
    runtime_paths = runtime_paths_for(config)
    if not room.users:
        # Team-formation tests should default to all configured agents being visible in the room.
        _add_room_users(
            room,
            [agent_id.full_id for agent_id in entity_ids(config, runtime_paths).values() if agent_id is not None],
        )
    return decide_team_formation(config=config, room=room, runtime_paths=runtime_paths, **kwargs)


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
    """Test the _TeamModeDecision model."""

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
    async def test_select_team_mode_coordinate(self, mock_config, monkeypatch: pytest.MonkeyPatch):
        """Test AI correctly identifies coordination tasks (different subtasks)."""
        model = _mode_model("coordinate", "Different agents handle different subtasks")
        install_model_providers(monkeypatch, {"default": model})
        result = await _select_team_mode_for_test(
            "Send me an email then call me",
            ["email", "phone"],
            mock_config,
        )

        assert result == TeamMode.COORDINATE
        assert len(model.requests) == 1

    @pytest.mark.asyncio
    async def test_select_team_mode_collaborate(self, mock_config, monkeypatch: pytest.MonkeyPatch):
        """Test AI correctly identifies collaboration tasks (same task for all)."""
        model = _mode_model("collaborate", "All agents work on the same brainstorming task")
        install_model_providers(monkeypatch, {"default": model})
        result = await _select_team_mode_for_test(
            "What do you think about this idea?",
            ["research", "analyst"],
            mock_config,
        )

        assert result == TeamMode.COLLABORATE
        assert len(model.requests) == 1

    @pytest.mark.asyncio
    async def test_select_team_mode_fallback_on_error(self, mock_config, monkeypatch: pytest.MonkeyPatch):
        """Test fallback to collaborate mode on AI error."""

        async def fail() -> None:
            msg = "AI service unavailable"
            raise ProviderError(msg, provider="Synthetic")

        install_model_providers(monkeypatch, {"default": ScriptedModel([[Await(fail)]])})
        with patch("mindroom.teams.formation.logger") as mock_logger:
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
    async def test_select_team_mode_unexpected_response(self, mock_config, monkeypatch: pytest.MonkeyPatch):
        """Test fallback when AI returns unexpected response type."""
        model = ScriptedModel([answer("Just a string, not _TeamModeDecision")])
        install_model_providers(monkeypatch, {"default": model})
        result = await _select_team_mode_for_test(
            "Do something",
            ["email", "phone"],
            mock_config,
        )

        # Should fallback to COLLABORATE on unexpected response
        assert result == TeamMode.COLLABORATE

    @pytest.mark.asyncio
    async def test_team_mode_choice_is_counted_as_internal_usage(self, mock_config, monkeypatch: pytest.MonkeyPatch):
        """The mode choice is a paid model call, so organization usage reports it under system:internal."""
        model = _mode_model("coordinate", "Different subtasks", metrics=usage(600, 20))
        install_model_providers(monkeypatch, {"default": model})
        result = await _select_team_mode_for_test("Send me an email then call me", ["email", "phone"], mock_config)

        assert result == TeamMode.COORDINATE
        report = collect_admin_usage(config=mock_config, runtime_paths=runtime_paths_for(mock_config))
        assert report.totals.total_tokens == 620
        assert [(row.key, row.totals.total_tokens) for row in report.breakdown] == [("system:internal", 620)]


class TestShouldFormTeam:
    """Test the pure decide_team_formation function."""

    def test_decide_team_formation_never_calls_ai(self, mock_config):
        """Formation is pure: it must never reach for the AI mode selector."""
        with patch(
            "mindroom.teams.formation._select_team_mode",
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

    def test_decide_team_formation_no_team_needed(self, mock_config):
        """Test when no team formation is needed."""
        result = decide_team_formation_for_test(
            tagged_agents=[entity_ids(mock_config, runtime_paths_for(mock_config))["email"]],  # Only one agent
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            room=_matrix_room(mock_config),
            config=mock_config,
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
    async def test_email_then_call_scenario(self, mock_config, monkeypatch: pytest.MonkeyPatch):
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

        model = _mode_model("coordinate", "Different tasks: email agent sends email, phone agent makes call")
        install_model_providers(monkeypatch, {"default": model})
        mode = await select_ad_hoc_team_mode(
            "Email me the details, then call me to discuss",
            result.eligible_members,
            mock_config,
            runtime_paths_for(mock_config),
        )

        assert mode == TeamMode.COORDINATE

    @pytest.mark.asyncio
    async def test_brainstorming_scenario(self, mock_config, monkeypatch: pytest.MonkeyPatch):
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

        model = _mode_model("collaborate", "All agents provide their perspective on the same question")
        install_model_providers(monkeypatch, {"default": model})
        mode = await select_ad_hoc_team_mode(
            "What are your thoughts on this approach?",
            result.eligible_members,
            mock_config,
            runtime_paths_for(mock_config),
        )

        assert mode == TeamMode.COLLABORATE
