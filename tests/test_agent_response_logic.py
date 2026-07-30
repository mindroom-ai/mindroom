"""Tests for agent response decision logic.

This module comprehensively tests all agent response rules:
1. Mentioned available agents respond
2. A single eligible responder can continue directly
3. Multiple agents need explicit mentions
4. Smart routing selects among multiple eligible responders
5. Invited agents behave like native agents

These tests ensure no regressions in the core response logic.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC, THREAD_HISTORY_ERROR_DIAGNOSTIC
from mindroom.message_target import MessageTarget
from mindroom.teams import TeamIntent, TeamMode, TeamOutcome, TeamResolution
from mindroom.thread_utils import check_agent_mentioned, get_agents_in_thread, is_router_only_agent_mention
from mindroom.turn_policy import PreparedDispatch, ResponseAction, TurnPolicy, TurnPolicyDeps, _ResponderAvailability
from tests.conftest import (
    agent_response_should_respond,
    bind_runtime_paths,
    create_mock_room,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts


def _bind_runtime_config(config: Config, runtime_root: Path | None = None) -> Config:
    runtime_paths = test_runtime_paths(runtime_root or Path(tempfile.mkdtemp()))
    bound_config = bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(bound_config, runtime_paths_for(bound_config))
    return bound_config


def _message(
    *,
    sender: str,
    body: str,
    content: dict[str, Any] | None = None,
) -> ResolvedVisibleMessage:
    """Build one typed visible message for thread-history tests."""
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=f"${sender}-{body}".replace(" ", "_"),
        content=content,
    )


class TestAgentResponseLogic:
    """Test the agent response decision logic."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = _bind_runtime_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="General", rooms=["!room:localhost"]),
                    "agent1": AgentConfig(display_name="Agent1", rooms=["!room:localhost"]),
                    "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
        )
        self.config.authorization.default_room_access = True
        self.runtime_paths = runtime_paths_for(self.config)
        self.domain = self.config.get_domain(self.runtime_paths)
        self.sender = f"@user:{self.domain}"

    def agent_id(self, agent_name: str) -> str:
        """Return the current persisted Matrix ID for a configured entity."""
        return entity_ids(self.config, self.runtime_paths)[agent_name].full_id

    def test_mentioned_agent_always_responds(self) -> None:
        """If an agent is mentioned, it should always respond."""
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_mentioned_agent_blocked_by_reply_permissions(self) -> None:
        """Per-agent reply allowlist should block disallowed senders even when mentioned."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"@alice:{self.domain}"],
        }
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=f"@bob:{self.domain}",
        )
        assert should_respond is False

    def test_mentioned_agent_reply_permissions_honor_aliases(self) -> None:
        """Bridge aliases should inherit per-agent reply permissions."""
        canonical_user = f"@alice:{self.domain}"
        alias_user = f"@telegram_111:{self.domain}"
        self.config.authorization.agent_reply_permissions = {
            "calculator": [canonical_user],
        }
        self.config.authorization.aliases = {canonical_user: [alias_user]}
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=alias_user,
        )
        assert should_respond is True

    def test_materializable_responder_filter_preserves_team_reject_owner(self) -> None:
        """Runtime materialization filters concrete agents, not configured team responder bots."""
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        config = bind_runtime_paths(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="Alpha"),
                    "beta": AgentConfig(display_name="Beta"),
                },
                teams={"ops": TeamConfig(display_name="Ops", role="Operations", agents=["alpha", "beta"])},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
            runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)
        team_id = entity_ids(config, runtime_paths)["ops"]
        runtime = MagicMock()
        runtime.config = config
        runtime.orchestrator = None
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=MagicMock(),
                runtime_paths=runtime_paths,
                agent_name="ops",
                matrix_id=team_id,
            ),
        )

        responder_pool = policy.filter_materializable_responders(
            [team_id],
            _ResponderAvailability(materializable_agent_names={"alpha", "beta"}, live_entity_names=None),
        )
        action = policy.team_response_action(
            TeamResolution(
                intent=TeamIntent.EXPLICIT_MEMBERS,
                requested_members=[team_id],
                member_statuses=[],
                eligible_members=[],
                outcome=TeamOutcome.REJECT,
                reason="Team request includes no available members.",
            ),
            responder_pool,
        )

        assert responder_pool == [team_id]
        assert action is not None
        assert action.kind == "reject"
        assert action.rejection_message == "Team request includes no available members."

    def test_private_eligible_member_does_not_own_explicit_reject(self) -> None:
        """Explicit team rejections must be surfaced by a live shared responder."""
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        config = bind_runtime_paths(
            Config(
                agents={
                    "shared": AgentConfig(display_name="Shared", rooms=["!room:localhost"]),
                    "private_worker": AgentConfig(
                        display_name="PrivateWorker",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user"),
                    ),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
            runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        runtime = MagicMock()
        runtime.config = config
        runtime.orchestrator = None
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=MagicMock(),
                runtime_paths=runtime_paths,
                agent_name="shared",
                matrix_id=ids["shared"],
            ),
        )

        team_resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[ids["private_worker"], ids["shared"]],
            member_statuses=[],
            eligible_members=[ids["private_worker"]],
            outcome=TeamOutcome.REJECT,
            reason="Team request includes unsupported members.",
        )
        responder_pool = [ids["private_worker"], ids["shared"]]
        owner = policy.response_owner_for_team_resolution(team_resolution, responder_pool)
        action = policy.team_response_action(team_resolution, responder_pool)

        assert owner == ids["shared"]
        assert action is not None
        assert action.kind == "reject"
        assert action.rejection_message == "Team request includes unsupported members."

    def test_live_shared_responder_owns_all_private_explicit_team(self) -> None:
        """A live shared responder must surface explicitly requested all-private teams."""
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        config = bind_runtime_paths(
            Config(
                agents={
                    "shared": AgentConfig(display_name="Shared", rooms=["!room:localhost"]),
                    "ops_member": AgentConfig(display_name="OpsMember", rooms=["!room:localhost"]),
                    "private_one": AgentConfig(
                        display_name="PrivateOne",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user"),
                    ),
                    "private_two": AgentConfig(
                        display_name="PrivateTwo",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user"),
                    ),
                },
                teams={"ops": TeamConfig(display_name="Ops", role="Operations", agents=["ops_member"])},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
            runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        runtime = MagicMock()
        runtime.config = config
        runtime.orchestrator = None
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=MagicMock(),
                runtime_paths=runtime_paths,
                agent_name="shared",
                matrix_id=ids["shared"],
            ),
        )

        team_resolution = TeamResolution(
            intent=TeamIntent.EXPLICIT_MEMBERS,
            requested_members=[ids["private_one"], ids["private_two"]],
            member_statuses=[],
            eligible_members=[ids["private_one"], ids["private_two"]],
            outcome=TeamOutcome.TEAM,
            mode=TeamMode.COORDINATE,
        )
        owner = policy.response_owner_for_team_resolution(
            team_resolution,
            responder_pool=[ids["private_one"], ids["ops"], ids["shared"]],
        )
        action = policy.team_response_action(
            team_resolution,
            responder_pool=[ids["private_one"], ids["ops"], ids["shared"]],
        )

        assert owner == ids["shared"]
        assert action is not None
        assert action.kind == "team"

    @pytest.mark.asyncio
    async def test_single_visible_responder_replies_when_planning_history_degraded(self) -> None:
        """A degraded dispatch read must not silence the sole visible responder in a thread."""
        room = create_mock_room("!room:localhost", ["calculator", "general"], self.config)
        runtime = MagicMock()
        runtime.client = None
        runtime.config = self.config
        runtime.orchestrator = None
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=MagicMock(),
                runtime_paths=self.runtime_paths,
                agent_name="calculator",
                matrix_id=entity_ids(self.config, self.runtime_paths)["calculator"],
            ),
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread-root:localhost",
            thread_history=thread_history_result(
                [],
                is_full_history=False,
                diagnostics={
                    THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
                    THREAD_HISTORY_ERROR_DIAGNOSTIC: "dispatch_read_timeout",
                },
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        assert context.planning_thread_history_unavailable is True
        target = MessageTarget.resolve(room.room_id, context.thread_id, "$event")
        dispatch = PreparedDispatch(
            requester_user_id=self.sender,
            context=context,
            target=target,
            correlation_id="$event",
            envelope=request_envelope(
                room_id=room.room_id,
                reply_to_event_id="$event",
                thread_id=context.thread_id,
                prompt="continue",
                user_id=self.sender,
                target=target,
                agent_name="calculator",
            ),
        )

        candidate_ids = entity_ids(self.config, self.runtime_paths)
        with patch(
            "mindroom.turn_policy.responder_candidate_entities_for_room",
            new=AsyncMock(return_value=[candidate_ids["calculator"], candidate_ids["general"]]),
        ):
            multiple_visible_action = await policy.resolve_response_action(
                dispatch,
                room,
                False,
                has_active_response_for_target=lambda _target: False,
            )

        assert multiple_visible_action.kind == "skip"

        with patch(
            "mindroom.turn_policy.responder_candidate_entities_for_room",
            new=AsyncMock(return_value=[candidate_ids["calculator"]]),
        ):
            single_visible_action = await policy.resolve_response_action(
                dispatch,
                room,
                False,
                has_active_response_for_target=lambda _target: False,
            )

        assert single_visible_action.kind == "individual"

    @pytest.mark.asyncio
    async def test_response_policy_logs_multi_agent_thread_skip(self) -> None:
        """A multi-agent thread skip should leave a useful policy diagnostic."""
        room = create_mock_room("!room:localhost", ["calculator", "general"], self.config)
        runtime = MagicMock()
        runtime.client = None
        runtime.config = self.config
        runtime.orchestrator = None
        logger = MagicMock()
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=logger,
                runtime_paths=self.runtime_paths,
                agent_name="calculator",
                matrix_id=entity_ids(self.config, self.runtime_paths)["calculator"],
            ),
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread-root:localhost",
            thread_history=thread_history_result(
                [
                    _message(sender=self.agent_id("general"), body="I can help."),
                    _message(sender=self.agent_id("calculator"), body="I can also help."),
                    _message(sender=self.sender, body="What next?"),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        target = MessageTarget.resolve(room.room_id, context.thread_id, "$event")
        dispatch = PreparedDispatch(
            requester_user_id=self.sender,
            context=context,
            target=target,
            correlation_id="$event",
            envelope=request_envelope(
                room_id=room.room_id,
                reply_to_event_id="$event",
                thread_id=context.thread_id,
                prompt="continue",
                user_id=self.sender,
                target=target,
                agent_name="calculator",
            ),
        )
        candidate_ids = entity_ids(self.config, self.runtime_paths)

        with (
            patch(
                "mindroom.turn_policy.responder_candidate_entities_for_room",
                new=AsyncMock(return_value=[candidate_ids["calculator"], candidate_ids["general"]]),
            ),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(return_value=TeamResolution.none()),
            ),
        ):
            action = await policy.resolve_response_action(
                dispatch,
                room,
                False,
                has_active_response_for_target=lambda _target: False,
            )

        assert action.kind == "skip"
        matching_calls = [
            call
            for call in logger.info.call_args_list
            if call.args == ("Skipping response: multiple agents in thread require explicit mention",)
        ]
        assert matching_calls
        assert matching_calls[0].kwargs["agent_name"] == "calculator"
        assert matching_calls[0].kwargs["thread_id"] == "$thread-root:localhost"
        assert matching_calls[0].kwargs["agents_in_thread"] == [
            self.agent_id("general"),
            self.agent_id("calculator"),
        ]

    @pytest.mark.asyncio
    async def test_response_policy_does_not_log_multi_agent_skip_for_multi_human_thread(self) -> None:
        """Multi-human thread suppression should not be misreported as multi-agent suppression."""
        room = create_mock_room("!room:localhost", ["calculator", "general"], self.config)
        runtime = MagicMock()
        runtime.client = None
        runtime.config = self.config
        runtime.orchestrator = None
        logger = MagicMock()
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=logger,
                runtime_paths=self.runtime_paths,
                agent_name="calculator",
                matrix_id=entity_ids(self.config, self.runtime_paths)["calculator"],
            ),
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread-root:localhost",
            thread_history=thread_history_result(
                [
                    _message(sender=self.agent_id("general"), body="I can help."),
                    _message(sender="@other-human:localhost", body="I have context too."),
                    _message(sender=self.agent_id("calculator"), body="I can also help."),
                    _message(sender=self.sender, body="What next?"),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        target = MessageTarget.resolve(room.room_id, context.thread_id, "$event")
        dispatch = PreparedDispatch(
            requester_user_id=self.sender,
            context=context,
            target=target,
            correlation_id="$event",
            envelope=request_envelope(
                room_id=room.room_id,
                reply_to_event_id="$event",
                thread_id=context.thread_id,
                prompt="continue",
                user_id=self.sender,
                target=target,
                agent_name="calculator",
            ),
        )
        candidate_ids = entity_ids(self.config, self.runtime_paths)

        with patch(
            "mindroom.turn_policy.responder_candidate_entities_for_room",
            new=AsyncMock(return_value=[candidate_ids["calculator"], candidate_ids["general"]]),
        ):
            action = await policy.resolve_response_action(
                dispatch,
                room,
                False,
                has_active_response_for_target=lambda _target: False,
            )

        assert action.kind == "skip"
        matching_calls = [
            call
            for call in logger.info.call_args_list
            if call.args == ("Skipping response: multiple agents in thread require explicit mention",)
        ]
        assert not matching_calls

    @pytest.mark.asyncio
    async def test_response_policy_continues_after_router_handoff(self) -> None:
        """The main turn policy should ignore router handoff as conversational participation."""
        room = create_mock_room("!room:localhost", ["calculator", "general"], self.config)
        runtime = MagicMock()
        runtime.client = None
        runtime.config = self.config
        runtime.orchestrator = None
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=MagicMock(),
                runtime_paths=self.runtime_paths,
                agent_name="general",
                matrix_id=entity_ids(self.config, self.runtime_paths)["general"],
            ),
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread-root:localhost",
            thread_history=thread_history_result(
                [
                    _message(sender=self.sender, body="Can someone help?"),
                    _message(
                        sender=self.agent_id("router"),
                        body="@mindroom_general could you help?",
                        content={
                            "m.mentions": {
                                "user_ids": [self.agent_id("general")],
                            },
                        },
                    ),
                    _message(sender=self.agent_id("general"), body="I can help."),
                    _message(sender=self.sender, body="What is the next step?"),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        target = MessageTarget.resolve(room.room_id, context.thread_id, "$event")
        dispatch = PreparedDispatch(
            requester_user_id=self.sender,
            context=context,
            target=target,
            correlation_id="$event",
            envelope=request_envelope(
                room_id=room.room_id,
                reply_to_event_id="$event",
                thread_id=context.thread_id,
                prompt="continue",
                user_id=self.sender,
                target=target,
                agent_name="general",
            ),
        )
        candidate_ids = entity_ids(self.config, self.runtime_paths)

        with (
            patch(
                "mindroom.turn_policy.responder_candidate_entities_for_room",
                new=AsyncMock(return_value=[candidate_ids["calculator"], candidate_ids["general"]]),
            ),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(return_value=TeamResolution.none()),
            ),
        ):
            action = await policy.resolve_response_action(
                dispatch,
                room,
                False,
                has_active_response_for_target=lambda _target: False,
            )

        assert action.kind == "individual"

    def test_effective_response_action_does_not_convert_reject_to_configured_team(self) -> None:
        """Configured-team execution only upgrades individual actions."""
        runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
        config = bind_runtime_paths(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="Alpha"),
                    "beta": AgentConfig(display_name="Beta"),
                },
                teams={"ops": TeamConfig(display_name="Ops", role="Operations", agents=["alpha", "beta"])},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
            ),
            runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)
        team_id = entity_ids(config, runtime_paths)["ops"]
        runtime = MagicMock()
        runtime.config = config
        runtime.orchestrator = None
        policy = TurnPolicy(
            TurnPolicyDeps(
                runtime=runtime,
                logger=MagicMock(),
                runtime_paths=runtime_paths,
                agent_name="ops",
                matrix_id=team_id,
            ),
        )
        rejection = ResponseAction(
            kind="reject",
            form_team=TeamResolution(
                intent=TeamIntent.EXPLICIT_MEMBERS,
                requested_members=[team_id],
                member_statuses=[],
                eligible_members=[],
                outcome=TeamOutcome.REJECT,
                reason="Team request includes no available members.",
            ),
            rejection_message="Team request includes no available members.",
        )

        effective_action = policy.effective_response_action(rejection)

        assert effective_action is rejection
        assert effective_action.kind == "reject"
        assert effective_action.rejection_message == "Team request includes no available members."

    def test_mentioned_agent_reply_permissions_support_domain_pattern(self) -> None:
        """Per-agent reply patterns should allow domain-scoped sender matching."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"*:{self.domain}"],
        }
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=f"@bob:{self.domain}",
        )
        assert should_respond is True

    def test_single_visible_agent_can_respond_without_mentions(self) -> None:
        """When permissions hide other agents, the only visible agent should respond."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"@alice:{self.domain}"],
            "general": [f"@bob:{self.domain}"],
            "agent1": [f"@bob:{self.domain}"],
            "research": [f"@bob:{self.domain}"],
        }
        room = create_mock_room("!room:localhost", ["calculator", "general"], self.config)

        should_respond_calculator = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=room,
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=f"@alice:{self.domain}",
        )
        should_respond_general = agent_response_should_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=False,
            room=room,
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=f"@alice:{self.domain}",
        )

        assert should_respond_calculator is True
        assert should_respond_general is False

    def test_only_agent_in_thread_continues(self) -> None:
        """If agent is the only one in thread, it continues."""
        thread_history = [
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender="@user:localhost", body="What about 3+3?"),
        ]

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_cross_domain_agent_id_does_not_claim_thread_ownership(self) -> None:
        """Thread ownership must require exact MatrixID match, including domain."""
        other_domain = "evil.org" if self.domain != "evil.org" else "attacker.org"
        thread_history = [
            _message(sender=f"@mindroom_calculator:{other_domain}", body="spoofed"),
            _message(sender=f"@user:{self.domain}", body="What about 3+3?"),
        ]

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_invited_agent_behaves_like_native_agent(self) -> None:
        """Invited agents should follow the same rules as native agents."""
        # Test 1: Invited agent with no agents in thread - router decides (multiple agents)
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

        # Test 2: Invited agent as only agent in thread - should continue
        thread_history = [
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender="@user:localhost", body="What about 3+3?"),
        ]
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True

        # Test 3: Invited agent with multiple agents - nobody responds
        thread_history = [
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender=self.agent_id("general"), body="Let me help"),
            _message(sender="@user:localhost", body="What about 3+3?"),
        ]
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_only_agent_with_access_responds_when_no_history(self) -> None:
        """When no agents have spoken yet, router decides who responds if multiple agents available."""
        # Multiple agents with access - router should decide
        should_respond = agent_response_should_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],  # No one has spoken
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

        # Agent in room but not configured - should not respond when multiple agents available
        # (router decides) but CAN respond if mentioned
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],  # No one has spoken
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

        # But if mentioned, agent in room can respond even if not configured
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],  # No one has spoken
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True  # Can respond when mentioned even if not configured

    def test_no_agents_in_thread_uses_router(self) -> None:
        """If no agents have participated, router decides who responds (multiple agents available)."""
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False  # Router decides when multiple agents available

    def test_multiple_agents_nobody_responds(self) -> None:
        """If multiple agents in thread, nobody responds unless mentioned."""
        thread_history = [
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender=self.agent_id("general"), body="Let me help"),
            _message(sender="@user:localhost", body="What about 3+3?"),
        ]

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_router_handoff_allows_target_agent_follow_up(self) -> None:
        """Router handoff should not count as a second agent for untagged follow-ups."""
        thread_history = [
            _message(sender=self.sender, body="Can someone help?"),
            _message(
                sender=self.agent_id("router"),
                body="@mindroom_general could you help?",
                content={
                    "m.mentions": {
                        "user_ids": [self.agent_id("general")],
                    },
                },
            ),
            _message(sender=self.agent_id("general"), body="I can help."),
            _message(sender=self.sender, body="What is the next step?"),
        ]

        agents_in_thread = get_agents_in_thread(thread_history, self.config, self.runtime_paths)

        assert [agent.full_id for agent in agents_in_thread] == [self.agent_id("general")]
        assert (
            agent_response_should_respond(
                agent_name="general",
                am_i_mentioned=False,
                is_thread=True,
                room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
                thread_history=thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is True
        )

    def test_router_handoff_still_requires_mention_after_second_real_agent_participates(self) -> None:
        """Router handoff is ignored, but two real agent participants still suppress auto-follow-up."""
        thread_history = [
            _message(sender=self.sender, body="Can someone help?"),
            _message(
                sender=self.agent_id("router"),
                body="@mindroom_general could you help?",
                content={
                    "m.mentions": {
                        "user_ids": [self.agent_id("general")],
                    },
                },
            ),
            _message(sender=self.agent_id("general"), body="I can help."),
            _message(sender=self.agent_id("calculator"), body="I can also help with numbers."),
            _message(sender=self.sender, body="What is the next step?"),
        ]
        room = create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config)

        agents_in_thread = get_agents_in_thread(thread_history, self.config, self.runtime_paths)

        assert [agent.full_id for agent in agents_in_thread] == [
            self.agent_id("general"),
            self.agent_id("calculator"),
        ]
        assert (
            agent_response_should_respond(
                agent_name="general",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is False
        )
        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is False
        )

    def test_explicit_router_mention_still_detected(self) -> None:
        """Filtering router from participants must not hide explicit router mentions."""
        router_id = entity_ids(self.config, self.runtime_paths)["router"]
        event_source = {
            "content": {
                "body": "@mindroom_router route this",
                "msgtype": "m.text",
                "m.mentions": {
                    "user_ids": [router_id.full_id],
                },
            },
        }

        mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            event_source,
            router_id,
            self.config,
            self.runtime_paths,
        )

        assert mentioned_agents == [router_id]
        assert am_i_mentioned is True
        assert has_non_agent_mentions is False
        assert (
            is_router_only_agent_mention(
                mentioned_agents,
                has_non_agent_mentions=has_non_agent_mentions,
                config=self.config,
                runtime_paths=self.runtime_paths,
            )
            is True
        )

    def test_explicit_target_agent_mention_still_works_in_multi_agent_thread(self) -> None:
        """Explicitly mentioning the target agent overrides multi-agent follow-up suppression."""
        thread_history = [
            _message(sender=self.sender, body="Can someone help?"),
            _message(sender=self.agent_id("router"), body="@mindroom_general could you help?"),
            _message(sender=self.agent_id("general"), body="I can help."),
            _message(sender=self.agent_id("calculator"), body="I can also help with numbers."),
            _message(sender=self.sender, body="@mindroom_general what next?"),
        ]
        mentioned_agents = [entity_ids(self.config, self.runtime_paths)["general"]]

        assert (
            agent_response_should_respond(
                agent_name="general",
                am_i_mentioned=True,
                is_thread=True,
                room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
                thread_history=thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                mentioned_agents=mentioned_agents,
                sender_id=self.sender,
            )
            is True
        )

    def test_only_permitted_agent_in_thread_continues(self) -> None:
        """A permitted agent should continue when other thread participants are disallowed."""
        self.config.authorization.agent_reply_permissions = {
            "calculator": [f"@alice:{self.domain}"],
            "general": [f"@bob:{self.domain}"],
        }
        thread_history = [
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender=self.agent_id("general"), body="I'll help too"),
            _message(sender=f"@alice:{self.domain}", body="What about 3+3?"),
        ]

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=f"@alice:{self.domain}",
        )
        assert should_respond is True

    def test_thread_continuation_cannot_widen_configured_room_boundary(self) -> None:
        """Thread ownership must stay inside the configured-room responder boundary."""
        config = bind_runtime_paths(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator"),
                    "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": [f"@bob:{self.domain}"],
                        "research": [f"@alice:{self.domain}"],
                    },
                },
            ),
            self.runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)
        room = create_mock_room("!room:localhost", ["calculator", "research"], config)
        thread_history = [
            _message(sender=self.agent_id("calculator"), body="I can help."),
            _message(sender=f"@bob:{self.domain}", body="continue"),
        ]

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=room,
            thread_history=thread_history,
            config=config,
            runtime_paths=runtime_paths,
            sender_id=f"@bob:{self.domain}",
        )

        assert should_respond is False

    def test_explicit_mention_cannot_widen_configured_room_boundary(self) -> None:
        """Explicit mentions must stay inside the configured-room responder boundary."""
        config = bind_runtime_paths(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator"),
                    "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
                },
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                authorization={"default_room_access": True},
            ),
            self.runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "research"], config),
            thread_history=[],
            config=config,
            runtime_paths=runtime_paths,
            sender_id=f"@bob:{self.domain}",
        )

        assert should_respond is False

    def test_not_in_thread_uses_router(self) -> None:
        """If not in a thread, use router to determine response."""
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_agent_not_in_room_no_response(self) -> None:
        """If agent is not in room (native or invited), don't respond."""
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_mentioned_outside_thread_responds(self) -> None:
        """Agents respond when mentioned in room (will create thread)."""
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=True,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_agent_mentioned_in_thread_history(self) -> None:
        """When any agent is mentioned in thread, only mentioned agents respond."""
        # Thread history with agent mentions
        thread_history = [
            _message(
                sender="@user:localhost",
                body="@mindroom_calculator help",
                content={"m.mentions": {"user_ids": [self.agent_id("calculator")]}},
            ),
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender="@user:localhost", body="what about 3+3?"),
        ]

        # Non-mentioned agent should not respond
        should_respond = agent_response_should_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_router_selection_scenarios(self) -> None:
        """Test various scenarios where router should be used."""
        # Scenario 1: Empty thread, native agent
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

        # Scenario 2: Thread with only user messages
        thread_history = [
            _message(sender="@user:localhost", body="I need help with math"),
            _message(sender="@user:localhost", body="Can someone help?"),
        ]
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_room_message_no_access_no_response(self) -> None:
        """Agent without room access doesn't respond to room messages."""
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_edge_case_empty_configured_rooms(self) -> None:
        """Test agent with no configured rooms but invited to thread."""
        # Should behave same as native agent when invited
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_mixed_agent_and_user_messages(self) -> None:
        """Test thread with interleaved agent and user messages."""
        thread_history = [
            _message(sender="@user:localhost", body="Help with math"),
            _message(sender=self.agent_id("calculator"), body="I can help!"),
            _message(sender="@user:localhost", body="Great, what's 2+2?"),
            _message(sender=self.agent_id("calculator"), body="2+2=4"),
            _message(sender=self.agent_id("general"), body="I can also help"),
            _message(sender="@user:localhost", body="What about 3+3?"),
        ]

        # Multiple agents present, nobody should respond without mention
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is False

    def test_router_disabled_when_any_agent_mentioned(self) -> None:
        """Test that router is disabled when any agent is mentioned, not just the current one."""
        # Room message scenario - agent1 is NOT mentioned but agent2 IS mentioned
        should_respond = agent_response_should_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        # Agent1 should not respond and should NOT use router
        assert not should_respond

        # Now test when no agents are mentioned - router should be used
        should_respond = agent_response_should_respond(
            agent_name="agent1",
            am_i_mentioned=False,
            is_thread=False,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
            # No agents mentioned
        )
        # Agent1 should not respond but SHOULD use router
        assert not should_respond

        # Test when current agent is mentioned
        should_respond = agent_response_should_respond(
            agent_name="agent1",
            am_i_mentioned=True,
            is_thread=True,
            room=create_mock_room("!test:example.org", ["agent1", "calculator", "general"], self.config),
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        # Agent1 SHOULD respond and should NOT use router
        assert should_respond

    def test_single_agent_takes_ownership_of_empty_thread(self) -> None:
        """When an ad-hoc room has one agent with access to an empty thread, it takes ownership."""
        room = create_mock_room("!adhoc:localhost", ["calculator"], self.config)

        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=room,
            thread_history=[],
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True

        # Thread with only user messages - single agent should also take ownership
        thread_history = [
            _message(sender="@user:localhost", body="I need help"),
            _message(sender="@user:localhost", body="Anyone there?"),
        ]
        should_respond = agent_response_should_respond(
            agent_name="calculator",
            am_i_mentioned=False,
            is_thread=True,
            room=room,
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            sender_id=self.sender,
        )
        assert should_respond is True

    def test_multiple_non_agent_users_in_thread_require_mentions(self) -> None:
        """Require mention when multiple humans posted in a thread, but allow thread continuity."""
        room = create_mock_room("!adhoc:localhost", ["calculator"], self.config)

        # Thread with two different human senders and no agent yet → require mention
        multi_human_thread = [
            _message(sender="@alice:localhost", body="Can someone help?"),
            _message(sender="@bob:localhost", body="I also need help"),
        ]
        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=multi_human_thread,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is False
        )

        # Same thread but agent is explicitly mentioned → respond
        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=True,
                is_thread=True,
                room=room,
                thread_history=multi_human_thread,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is True
        )

        # Thread with only one human sender → auto-respond (single agent room)
        single_human_thread = [
            _message(sender="@alice:localhost", body="Can someone help?"),
        ]
        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=single_human_thread,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is True
        )

        # Agent already participating in multi-human thread → still require mention
        owned_thread_history = [
            _message(sender="@alice:localhost", body="help"),
            _message(sender="@bob:localhost", body="me too"),
            _message(sender=self.agent_id("calculator"), body="Sure, I can help."),
            _message(sender="@alice:localhost", body="Can you continue?"),
        ]
        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=owned_thread_history,
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is False
        )

    def test_non_agent_mention_suppresses_auto_response(self) -> None:
        """Agent should not auto-respond when a non-agent user is explicitly mentioned."""
        room = create_mock_room("!room:localhost", ["calculator"], self.config)

        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=False,
                room=room,
                thread_history=[],
                config=self.config,
                runtime_paths=self.runtime_paths,
                has_non_agent_mentions=True,
                sender_id=self.sender,
            )
            is False
        )

    def test_multi_human_room_non_thread_auto_responds(self) -> None:
        """Non-thread messages in multi-human rooms auto-respond (single agent)."""
        room = create_mock_room("!adhoc:localhost", ["calculator"], self.config)
        room.users["@alice:localhost"] = None
        room.users["@bob:localhost"] = None

        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=False,
                room=room,
                thread_history=[],
                config=self.config,
                runtime_paths=self.runtime_paths,
                sender_id=self.sender,
            )
            is True
        )

    def test_bot_account_excluded_from_multi_human_thread(self) -> None:
        """A bot_account posting in a thread should not count as a second human."""
        config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["!room:localhost"]),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            bot_accounts=["@telegram:localhost"],
        )
        config = bind_runtime_paths(config, test_runtime_paths(Path(tempfile.mkdtemp())))
        persist_entity_accounts(config, runtime_paths_for(config))
        runtime_paths = runtime_paths_for(config)
        config.authorization.default_room_access = True
        room = create_mock_room("!room:localhost", ["calculator"], config)

        thread_with_bot = [
            _message(sender="@alice:localhost", body="hello"),
            _message(sender="@telegram:localhost", body="relayed message"),
        ]
        # Only one real human — agent should auto-respond
        assert (
            agent_response_should_respond(
                agent_name="calculator",
                am_i_mentioned=False,
                is_thread=True,
                room=room,
                thread_history=thread_with_bot,
                config=config,
                runtime_paths=runtime_paths,
                sender_id="@alice:localhost",
            )
            is True
        )

    def test_agent_stops_when_user_mentions_other_agent(self) -> None:
        """Test that an agent stops responding when user mentions a different agent.

        This tests the specific bug where GeneralAgent continued responding
        after the user explicitly mentioned ResearchAgent.
        """
        # Thread history: GeneralAgent was initially mentioned by router and responded
        thread_history = [
            _message(sender="@user:localhost", body="hi"),
            _message(sender=self.agent_id("router"), body="@general could you help with this?"),
            _message(sender=self.agent_id("general"), body="Hello! How can I help?"),
        ]

        # GeneralAgent should NOT respond because ResearchAgent is mentioned
        should_respond = agent_response_should_respond(
            agent_name="general",
            am_i_mentioned=False,  # GeneralAgent is NOT mentioned
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            mentioned_agents=[
                entity_ids(self.config, runtime_paths_for(self.config))["research"],
            ],  # ResearchAgent is mentioned
            sender_id=self.sender,
        )
        assert should_respond is False  # Should NOT respond when another agent is mentioned

        # But if no agents are mentioned, general should continue the conversation
        should_respond = agent_response_should_respond(
            agent_name="general",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!room:localhost", ["calculator", "general", "agent1"], self.config),
            thread_history=thread_history,
            config=self.config,
            runtime_paths=self.runtime_paths,
            mentioned_agents=[],  # No agents mentioned
            sender_id=self.sender,
        )
        assert should_respond is True  # Should continue when no one is mentioned
