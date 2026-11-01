"""Tests for team-based agent collaboration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.authorization import get_available_responders_in_room
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.users import AgentMatrixUser
from mindroom.teams.formation import TeamIntent, TeamMemberStatus, TeamOutcome, TeamResolution, decide_team_formation
from mindroom.thread_utils import get_agents_in_thread
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    make_visible_message,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import actual_entity_usernames, entity_ids, entity_name_for_id, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Sequence


def _runtime_bound_config(config: Config, runtime_root: Path | None = None) -> Config:
    """Return a runtime-bound config for team tests."""
    runtime_paths = test_runtime_paths(runtime_root or Path(tempfile.mkdtemp()))
    bound_config = bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(
        bound_config,
        runtime_paths,
        usernames=actual_entity_usernames(bound_config),
    )
    return bound_config


def _agent_names(ids: list[object], config: Config) -> list[str]:
    runtime_paths = runtime_paths_for(config)
    return [entity_name_for_id(mid, config, runtime_paths) for mid in ids]


def _matrix_room(room_id: str, user_ids: list[str]) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id, "@test-user:localhost")
    room.members_synced = True
    for user_id in user_ids:
        room.add_member(user_id, None, None)
    return room


# Test fixtures for team agents
@pytest.fixture
def mock_research_agent() -> AgentMatrixUser:
    """Create a mock research agent."""
    return AgentMatrixUser(
        agent_name="research",
        user_id="@actual_research:localhost",
        display_name="ResearchAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_analyst_agent() -> AgentMatrixUser:
    """Create a mock analyst agent."""
    return AgentMatrixUser(
        agent_name="analyst",
        user_id="@actual_analyst:localhost",
        display_name="AnalystAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_code_agent() -> AgentMatrixUser:
    """Create a mock code agent."""
    return AgentMatrixUser(
        agent_name="code",
        user_id="@actual_code:localhost",
        display_name="CodeAgent",
        password=TEST_PASSWORD,
    )


@pytest.fixture
def mock_security_agent() -> AgentMatrixUser:
    """Create a mock security agent."""
    return AgentMatrixUser(
        agent_name="security",
        user_id="@actual_security:localhost",
        display_name="SecurityAgent",
        password=TEST_PASSWORD,
    )


class TestTeamFormation:
    """Test team formation logic."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = _runtime_bound_config(
            Config(
                agents={
                    "code": AgentConfig(display_name="Code", rooms=["!test:example.org"]),
                    "security": AgentConfig(display_name="Security", rooms=["!test:example.org"]),
                    "research": AgentConfig(display_name="Research", rooms=["!test:example.org"]),
                    "analyst": AgentConfig(display_name="Analyst", rooms=["!test:example.org"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

    @pytest.mark.asyncio
    async def test_multiple_agents_in_thread_form_team(
        self,
        mock_code_agent: AgentMatrixUser,
        mock_security_agent: AgentMatrixUser,
    ) -> None:
        """Test that multiple agents already in a thread form a team when no one is mentioned."""
        # Mock thread history showing both agents have participated
        thread_history = [
            make_visible_message(sender="@user:localhost", body="How should we implement authentication?"),
            make_visible_message(sender=mock_code_agent.user_id, body="I suggest using JWT tokens..."),
            make_visible_message(sender=mock_security_agent.user_id, body="We should also add rate limiting..."),
        ]

        # Message with no mentions would trigger team formation
        # (message_event setup omitted as it's tested via thread_history)

        # Verify both agents are in thread
        agents_in_thread = get_agents_in_thread(thread_history, self.config, runtime_paths_for(self.config))
        agent_names = _agent_names(agents_in_thread, self.config)
        assert "code" in agent_names
        assert "security" in agent_names
        assert len(agent_names) == 2


class TestTeamResponseBehavior:
    """Test specific team response behaviors."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = _runtime_bound_config(
            Config(
                agents={
                    "code": AgentConfig(display_name="Code", rooms=["!test:example.org"]),
                    "security": AgentConfig(display_name="Security", rooms=["!test:example.org"]),
                    "research": AgentConfig(display_name="Research", rooms=["!test:example.org"]),
                    "analyst": AgentConfig(display_name="Analyst", rooms=["!test:example.org"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

    @pytest.mark.asyncio
    async def test_single_agent_still_continues_conversation(
        self,
        mock_code_agent: AgentMatrixUser,
    ) -> None:
        """Test that single agent behavior remains unchanged."""
        # Thread with only one agent
        thread_history = [
            make_visible_message(sender="@user:localhost", body="Can you help with Python?"),
            make_visible_message(sender=mock_code_agent.user_id, body="Sure, I can help with Python!"),
        ]

        # No mentions in follow-up would cause single agent to continue

        agents_in_thread = get_agents_in_thread(thread_history, self.config, runtime_paths_for(self.config))
        agent_names = _agent_names(agents_in_thread, self.config)
        assert agent_names == ["code"]
        # Single agent should continue responding

    @pytest.mark.asyncio
    async def test_team_with_invited_agents(
        self,
        mock_research_agent: AgentMatrixUser,
        mock_analyst_agent: AgentMatrixUser,
    ) -> None:
        """Test team formation with invited agents."""
        # One agent is native to room, another is invited
        native_agent = mock_research_agent
        invited_agent = mock_analyst_agent

        # Both should form team when working together
        thread_with_both = [
            make_visible_message(sender=native_agent.user_id, body="Research findings..."),
            make_visible_message(sender=invited_agent.user_id, body="Analysis of findings..."),
        ]

        agents = get_agents_in_thread(thread_with_both, self.config, runtime_paths_for(self.config))
        agent_names = _agent_names(agents, self.config)
        assert len(agents) == 2
        assert "research" in agent_names
        assert "analyst" in agent_names

        # Large thread history approaching token limits
        # Team should coordinate to provide concise responses


class TestRouterTeamFormation:
    """Test router-initiated team formation."""

    @pytest.mark.asyncio
    async def test_dm_room_team_formation(self) -> None:
        """Test that multiple agents in a DM room form a team when no one is mentioned."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "agent1": AgentConfig(display_name="Agent 1", role="First agent"),
                    "agent2": AgentConfig(display_name="Agent 2", role="Second agent"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        ids = entity_ids(config, runtime_paths_for(config))
        room = _matrix_room("!dm:localhost", [ids["agent1"].full_id, ids["agent2"].full_id])

        # Test DM room with multiple agents and no mentions
        result = decide_team_formation(
            tagged_agents=[],  # No agents mentioned
            agents_in_thread=[],  # No agents have spoken yet
            all_mentioned_in_thread=[],  # No mentions in thread
            runtime_paths=runtime_paths_for(config),
            config=config,
            is_dm_room=True,  # This is a DM room
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_thread=False,
            materializable_agent_names=None,
        )

        # Should form a team with both agents
        assert result.outcome is TeamOutcome.TEAM
        assert result.intent is TeamIntent.DM_AUTO_TEAM
        agent_names = sorted(_agent_names(result.eligible_members, config))
        assert agent_names == ["agent1", "agent2"]

        # Test DM room with single agent (should not form team)
        room = _matrix_room("!dm:localhost", [ids["agent1"].full_id])
        result = decide_team_formation(
            tagged_agents=[],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            is_dm_room=True,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_thread=False,
            materializable_agent_names=None,
        )

        # Should not form a team with single agent
        assert result.outcome is TeamOutcome.NONE

    @pytest.mark.asyncio
    async def test_dm_room_thread_single_agent_no_team(self) -> None:
        """In a DM with multiple agents, a thread with a single agent should not form a team."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        # DM room with multiple agents
        room = _matrix_room(
            "!dm:localhost",
            [
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
            ],
        )

        # Thread has only calculator participating so far
        agents_in_thread = [entity_ids(config, runtime_paths_for(config))["calculator"]]

        # Should NOT form a team inside a thread with a single agent
        result = decide_team_formation(
            tagged_agents=[],
            agents_in_thread=agents_in_thread,
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            is_dm_room=True,
            is_thread=True,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.NONE

    @pytest.mark.asyncio
    async def test_dm_room_ignores_private_agents_for_team_formation(self) -> None:
        """A DM room's auto team leaves out a private agent and degrades to the one supported agent left."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "private_worker": AgentConfig(
                        display_name="PrivateWorker",
                        role="Private assistant",
                        private=AgentPrivateConfig(per="user", root="private_worker_data"),
                    ),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!dm:localhost",
            [
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["private_worker"].full_id,
            ],
        )

        result = decide_team_formation(
            tagged_agents=[],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            is_dm_room=True,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_thread=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.INDIVIDUAL
        assert result.intent is TeamIntent.DM_AUTO_TEAM
        assert _agent_names(result.eligible_members, config) == ["calculator"]

    @pytest.mark.asyncio
    async def test_thread_history_unavailable_agents_degrade_to_individual(self) -> None:
        """Implicit thread continuation should not reject when one historical agent is off-room."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!thread:localhost",
            [entity_ids(config, runtime_paths_for(config))["calculator"].full_id],
        )

        result = decide_team_formation(
            tagged_agents=[],
            agents_in_thread=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_thread=True,
            is_dm_room=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.INDIVIDUAL
        assert result.intent is TeamIntent.IMPLICIT_THREAD_TEAM
        assert _agent_names(result.eligible_members, config) == ["calculator"]

    @pytest.mark.asyncio
    async def test_thread_history_ignores_configured_team_participants(self) -> None:
        """Implicit thread teams must ignore configured team bots instead of treating them as leaf members."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "agent_alpha": AgentConfig(display_name="Agent Alpha", role="Alpha"),
                    "agent_beta": AgentConfig(display_name="Agent Beta", role="Beta"),
                    "agent_gamma": AgentConfig(display_name="Agent Gamma", role="Gamma"),
                },
                teams={
                    "meta_team": TeamConfig(
                        display_name="Meta Team",
                        role="Combined agent team",
                        agents=["agent_alpha", "agent_beta", "agent_gamma"],
                        mode="coordinate",
                    ),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!thread:localhost",
            [
                entity_ids(config, runtime_paths_for(config))["meta_team"].full_id,
                entity_ids(config, runtime_paths_for(config))["agent_alpha"].full_id,
                entity_ids(config, runtime_paths_for(config))["agent_beta"].full_id,
                entity_ids(config, runtime_paths_for(config))["agent_gamma"].full_id,
            ],
        )

        result = decide_team_formation(
            tagged_agents=[],
            agents_in_thread=[
                entity_ids(config, runtime_paths_for(config))["meta_team"],
                entity_ids(config, runtime_paths_for(config))["agent_alpha"],
                entity_ids(config, runtime_paths_for(config))["agent_beta"],
                entity_ids(config, runtime_paths_for(config))["agent_gamma"],
            ],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_thread=True,
            is_dm_room=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.TEAM
        assert result.intent is TeamIntent.IMPLICIT_THREAD_TEAM
        assert sorted(_agent_names(result.requested_members, config)) == [
            "agent_alpha",
            "agent_beta",
            "agent_gamma",
        ]
        assert sorted(_agent_names(result.eligible_members, config)) == [
            "agent_alpha",
            "agent_beta",
            "agent_gamma",
        ]

    @pytest.mark.asyncio
    async def test_previously_mentioned_off_room_agents_degrade_to_individual(self) -> None:
        """Implicit thread mentions should degrade instead of surfacing explicit-request rejection."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!thread:localhost",
            [entity_ids(config, runtime_paths_for(config))["calculator"].full_id],
        )

        result = decide_team_formation(
            tagged_agents=[],
            agents_in_thread=[],
            all_mentioned_in_thread=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_thread=True,
            is_dm_room=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.INDIVIDUAL
        assert result.intent is TeamIntent.IMPLICIT_THREAD_TEAM
        assert _agent_names(result.eligible_members, config) == ["calculator"]

    @pytest.mark.asyncio
    async def test_tagged_off_room_agents_reject_the_entire_team_request(self) -> None:
        """An explicit team request rejects a member the room does not hold, which the sender cannot reach there."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!room:localhost",
            [entity_ids(config, runtime_paths_for(config))["calculator"].full_id],
        )

        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_dm_room=False,
            is_thread=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.REJECT
        assert result.intent is TeamIntent.EXPLICIT_MEMBERS
        assert _agent_names(result.eligible_members, config) == ["calculator"]
        assert result.reason == "Team request includes agent 'general' that is not available to you in this room."
        assert {member.name: member.status for member in result.member_statuses} == {
            "calculator": TeamMemberStatus.ELIGIBLE,
            "general": TeamMemberStatus.HIDDEN_FROM_SENDER,
        }

    @pytest.mark.asyncio
    async def test_tagged_agents_reject_when_sender_can_talk_to_zero_agents(self) -> None:
        """A sender who can reach no responder in the room has every tagged agent rejected, named in one reason."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=[],
            is_dm_room=False,
            is_thread=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.REJECT
        assert result.eligible_members == []
        assert result.reason == (
            "Team request includes agents 'calculator', 'general' that are not available to you in this room."
        )

    @pytest.mark.asyncio
    async def test_tagged_off_room_agents_reject_without_collapsing_requested_members(self) -> None:
        """Explicit rejects should preserve the full requested-member failure state."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                    "research": AgentConfig(display_name="Research", role="Research"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["research"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=[entity_ids(config, runtime_paths_for(config))["calculator"]],
            materializable_agent_names={"calculator"},
            is_dm_room=False,
            is_thread=False,
        )

        assert result.outcome is TeamOutcome.REJECT
        assert {member.name: member.status for member in result.member_statuses} == {
            "general": TeamMemberStatus.HIDDEN_FROM_SENDER,
            "research": TeamMemberStatus.HIDDEN_FROM_SENDER,
        }
        assert result.reason == (
            "Team request includes agents 'general', 'research' that are not available to you in this room."
        )

    @pytest.mark.asyncio
    async def test_tagged_private_agent_can_join_explicit_ad_hoc_team(self) -> None:
        """Explicit shared/private mentions should form one ad hoc team."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                    "general": AgentConfig(display_name="General", role="General"),
                    "private_worker": AgentConfig(
                        display_name="PrivateWorker",
                        role="Private assistant",
                        private=AgentPrivateConfig(per="user", root="private_worker_data"),
                    ),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!room:localhost",
            [
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
                entity_ids(config, runtime_paths_for(config))["private_worker"].full_id,
            ],
        )
        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["private_worker"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_dm_room=False,
            is_thread=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.TEAM
        assert result.intent is TeamIntent.EXPLICIT_MEMBERS
        assert {member.name: member.status for member in result.member_statuses} == {
            "calculator": TeamMemberStatus.ELIGIBLE,
            "general": TeamMemberStatus.ELIGIBLE,
            "private_worker": TeamMemberStatus.ELIGIBLE,
        }
        assert _agent_names(result.eligible_members, config) == ["calculator", "general", "private_worker"]

    @pytest.mark.asyncio
    async def test_tagged_unsupported_non_materializable_member_keeps_requested_member_statuses(self) -> None:
        """Explicit rejects should keep requested-member eligibility separate from delivery ownership."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="Alpha", role="Assistant", delegate_to=["vault"]),
                    "vault": AgentConfig(
                        display_name="Vault",
                        role="Private assistant",
                        private=AgentPrivateConfig(per="user", root="vault_data"),
                    ),
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            materializable_agent_names={"calculator"},
            is_dm_room=False,
            is_thread=False,
        )

        assert result.outcome is TeamOutcome.REJECT
        assert {member.name: member.status for member in result.member_statuses} == {
            "alpha": TeamMemberStatus.UNSUPPORTED_FOR_TEAM,
            "calculator": TeamMemberStatus.ELIGIBLE,
        }
        assert result.eligible_members == [entity_ids(config, runtime_paths_for(config))["calculator"]]

    @pytest.mark.asyncio
    async def test_tagged_mixed_reject_causes_report_member_specific_reasons(self) -> None:
        """Mixed reject causes should explain the actual member failures instead of flattening them."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="Alpha", role="Assistant", delegate_to=["vault"]),
                    "vault": AgentConfig(
                        display_name="Vault",
                        role="Private assistant",
                        private=AgentPrivateConfig(per="user", root="vault_data"),
                    ),
                    "general": AgentConfig(display_name="General", role="General"),
                    "calculator": AgentConfig(display_name="Calculator", role="Math"),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            materializable_agent_names={"alpha", "calculator"},
            is_dm_room=False,
            is_thread=False,
        )

        assert result.outcome is TeamOutcome.REJECT
        assert result.reason == (
            "Team request cannot be satisfied: "
            "agent 'alpha' reaches private agent 'vault' via delegation and private delegation is not supported for teams; "
            "agent 'general' could not be materialized for this request"
        )

    @pytest.mark.asyncio
    async def test_tagged_agents_that_delegate_to_private_reject_the_entire_team_request(
        self,
    ) -> None:
        """Ad hoc team formation must reject explicit member sets that reach private agents."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="General",
                        role="Coordinator",
                        delegate_to=["research"],
                    ),
                    "code": AgentConfig(display_name="Code", role="Coder"),
                    "analyst": AgentConfig(display_name="Analyst", role="Analyst"),
                    "research": AgentConfig(
                        display_name="Research",
                        role="Private researcher",
                        private=AgentPrivateConfig(per="user", root="research_data"),
                    ),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )

        room = _matrix_room(
            "!room:localhost",
            [
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
                entity_ids(config, runtime_paths_for(config))["code"].full_id,
                entity_ids(config, runtime_paths_for(config))["analyst"].full_id,
                entity_ids(config, runtime_paths_for(config))["research"].full_id,
            ],
        )
        result = decide_team_formation(
            tagged_agents=[
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["code"],
                entity_ids(config, runtime_paths_for(config))["analyst"],
            ],
            agents_in_thread=[],
            all_mentioned_in_thread=[],
            runtime_paths=runtime_paths_for(config),
            config=config,
            available_responders_in_room=get_available_responders_in_room(room, config, runtime_paths_for(config)),
            is_dm_room=False,
            is_thread=False,
            materializable_agent_names=None,
        )

        assert result.outcome is TeamOutcome.REJECT


def _formation_config(**agents: AgentConfig) -> Config:
    return _runtime_bound_config(
        Config(agents=agents, models={"default": ModelConfig(provider="ollama", id="test-model")}),
    )


def _decide(
    config: Config,
    *,
    room_members: list[str],
    tagged: Sequence[str] = (),
    mentioned_in_thread: Sequence[str] = (),
    sender_visible: Sequence[str] | None = None,
) -> TeamResolution:
    """Decide formation in a room whose members are ``room_members``, by agent name, all of whom the sender sees.

    ``sender_visible`` narrows the sender's view of the room's responders.
    """
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    return decide_team_formation(
        tagged_agents=[ids[name] for name in tagged],
        agents_in_thread=[],
        all_mentioned_in_thread=[ids[name] for name in mentioned_in_thread],
        runtime_paths=runtime_paths,
        config=config,
        is_thread=bool(mentioned_in_thread),
        available_responders_in_room=[
            ids[name] for name in (room_members if sender_visible is None else sender_visible)
        ],
        is_dm_room=False,
        materializable_agent_names=None,
    )


def test_an_agent_mentioned_twice_is_requested_once() -> None:
    """One agent named twice in a message forms no team with itself, and joins a team of two once."""
    config = _formation_config(
        calculator=AgentConfig(display_name="Calculator", role="Math"),
        general=AgentConfig(display_name="General", role="General"),
    )

    alone = _decide(config, room_members=["calculator", "general"], tagged=["calculator", "calculator"])
    paired = _decide(config, room_members=["calculator", "general"], tagged=["calculator", "general", "calculator"])

    assert alone.outcome is TeamOutcome.NONE
    assert paired.outcome is TeamOutcome.TEAM
    assert _agent_names(paired.requested_members, config) == ["calculator", "general"]


def test_a_team_request_names_every_agent_missing_from_the_room() -> None:
    """An explicit request with several members off the room rejects with all their names."""
    config = _formation_config(
        calculator=AgentConfig(display_name="Calculator", role="Math"),
        general=AgentConfig(display_name="General", role="General"),
        research=AgentConfig(display_name="Research", role="Research"),
    )

    result = _decide(config, room_members=["calculator"], tagged=["calculator", "general", "research"])

    assert result.outcome is TeamOutcome.REJECT
    assert result.reason == (
        "Team request includes agents 'general', 'research' that are not available to you in this room."
    )


@pytest.mark.parametrize(
    ("private_targets", "room_members", "sender_visible", "expected_reason"),
    [
        (
            ["research"],
            ["code"],
            None,
            "Team request cannot be satisfied: "
            "agent 'code' reaches private agent 'research' via delegation and private delegation is not supported for "
            "teams; agent 'general' is not available to you in this room",
        ),
        (
            ["research", "vault"],
            ["code", "general"],
            ["code"],
            "Team request cannot be satisfied: "
            "agent 'code' reaches private agents 'research', 'vault' via delegation and private delegation is not "
            "supported for teams; agent 'general' is not available to you in this room",
        ),
    ],
)
def test_a_mixed_reject_explains_private_delegation_and_unavailability_per_member(
    private_targets: list[str],
    room_members: list[str],
    sender_visible: list[str] | None,
    expected_reason: str,
) -> None:
    """A member that delegates to private agents and one the sender cannot reach in the room each get their own reason."""
    config = _formation_config(
        code=AgentConfig(display_name="Code", role="Coder", delegate_to=private_targets),
        general=AgentConfig(display_name="General", role="General"),
        **{
            name: AgentConfig(
                display_name=name.title(),
                role="Private assistant",
                private=AgentPrivateConfig(per="user", root=f"{name}_data"),
            )
            for name in private_targets
        },
    )

    result = _decide(config, room_members=room_members, tagged=["code", "general"], sender_visible=sender_visible)

    assert result.outcome is TeamOutcome.REJECT
    assert result.reason == expected_reason


def test_an_implicit_thread_team_whose_members_all_left_the_room_forms_no_team() -> None:
    """Earlier mentions of agents no longer in the room neither form a team nor reject the message."""
    config = _formation_config(
        calculator=AgentConfig(display_name="Calculator", role="Math"),
        general=AgentConfig(display_name="General", role="General"),
        research=AgentConfig(display_name="Research", role="Research"),
    )

    result = _decide(config, room_members=["research"], mentioned_in_thread=["calculator", "general"])

    assert result.outcome is TeamOutcome.NONE
    assert result.intent is TeamIntent.IMPLICIT_THREAD_TEAM
    assert result.eligible_members == []
    assert {member.name: member.status for member in result.member_statuses} == {
        "calculator": TeamMemberStatus.HIDDEN_FROM_SENDER,
        "general": TeamMemberStatus.HIDDEN_FROM_SENDER,
    }


def test_an_implicit_thread_team_names_only_its_members_still_in_the_room() -> None:
    """A team formed without a member who left the room names its members by agent name, in their order."""
    config = _formation_config(
        calculator=AgentConfig(display_name="Calculator", role="Math"),
        general=AgentConfig(display_name="General", role="General"),
        research=AgentConfig(display_name="Research", role="Research"),
    )

    result = _decide(
        config,
        room_members=["research", "calculator"],
        mentioned_in_thread=["research", "general", "calculator"],
    )

    assert result.outcome is TeamOutcome.TEAM
    assert result.eligible_member_names == ("research", "calculator")
    assert list(result.eligible_member_names) == _agent_names(result.eligible_members, config)
