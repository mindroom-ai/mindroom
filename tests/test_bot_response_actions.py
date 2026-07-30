"""Turn-policy response action resolution through the TurnPolicy seam."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, TeamBot
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import (
    ROUTER_AGENT_NAME,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    EXTERNAL_TRIGGER_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.hooks import (
    MessageEnvelope,
)
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.teams import TeamIntent, TeamMemberStatus, TeamOutcome, TeamResolution, TeamResolutionMember
from mindroom.thread_utils import AgentResponseDecision
from mindroom.turn_policy import PreparedDispatch, _DispatchPlan
from tests.bot_helpers import (
    AgentBotTestBase,
    _hook_envelope,
    _install_runtime_cache_support,
    _matrix_room,
    _policy_dispatch,
    _runtime_bound_config,
    make_mock_agent_user,
)
from tests.conftest import (
    TEST_PASSWORD,
    message_origin,
    runtime_paths_for,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_decide_team_for_sender_passes_sender_filtered_dm_agents(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """DM team fallback should only see agents allowed for the requester."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!dm:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!dm:localhost"]),
                },
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": ["@alice:localhost"],
                        "general": ["@bob:localhost"],
                    },
                },
            ),
            tmp_path,
        )

        context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        room = _matrix_room(
            room_id="!dm:localhost",
            own_user_id=mock_agent_user.user_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
            ],
        )

        with patch("mindroom.turn_policy.decide_team_formation", new_callable=MagicMock) as mock_decide:
            mock_decide.return_value = TeamResolution.none()
            bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
            bot.orchestrator = MagicMock()
            bot.orchestrator.agent_bots = {"calculator": MagicMock()}

            await bot._turn_policy.decide_team_for_sender(
                agents_in_thread=[],
                context=context,
                room=room,
                requester_user_id="@alice:localhost",
                is_dm=True,
                availability=bot._turn_policy.responder_availability(),
            )

        assert mock_decide.call_count == 1
        assert mock_decide.call_args.kwargs["available_responders_in_room"] == [
            entity_ids(config, runtime_paths_for(config))["calculator"],
        ]
        assert mock_decide.call_args.kwargs["materializable_agent_names"] == {"calculator"}

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_instead_of_falling_through_to_individual_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicitly rejected team requests must not fall through to individual replies."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(own_user_id=bot.matrix_id.full_id, user_ids=[bot.matrix_id.full_id])
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[bot.matrix_id],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[bot.matrix_id],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=bot.matrix_id,
                                name=bot.agent_name,
                                status=TeamMemberStatus.ELIGIBLE,
                            ),
                        ],
                        eligible_members=[bot.matrix_id],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes private agent 'mind'; private agents are only supported in explicit Matrix ad hoc teams with requester identity",
                    ),
                ),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(True),
            ) as mock_decide_agent_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "help me"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "reject"
        assert (
            "private agents are only supported in explicit Matrix ad hoc teams with requester identity"
            in action.rejection_message
        )
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_when_explicit_mentions_include_hidden_agent(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit mixed mentions should reject instead of collapsing to one visible agent."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": ["@alice:localhost"],
                        "general": ["@bob:localhost"],
                    },
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            has_non_agent_mentions=False,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@alice:localhost", "calculator and general, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes agent 'general' that is not available to you in this room."
        )
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_when_only_unrequested_visible_bot_can_surface_reject(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit rejects should not go silent when stale room members sort before the live fallback bot."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {"calculator": MagicMock()}
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
                entity_ids(config, runtime_paths_for(config))["research"].full_id,
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["research"],
            ],
            has_non_agent_mentions=False,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "general and research, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes agents 'general', 'research' that could not be materialized for this request."
        )
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_rejects_non_running_requested_member(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit team requests must treat stopped bots as unavailable."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="AlphaAgent", rooms=["!room:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {
            "alpha": MagicMock(running=False),
            "calculator": MagicMock(running=True),
        }
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["alpha"].full_id,
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "alpha and calculator, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "reject"
        assert action.rejection_message == (
            "Team request includes agent 'alpha' that could not be materialized for this request."
        )
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_skips_when_explicit_mentions_are_all_hidden(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit mixed mentions must not fall through when sender-visible agents are []."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": ["@bob:localhost"],
                        "general": ["@bob:localhost"],
                    },
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["general"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["calculator"],
                entity_ids(config, runtime_paths_for(config))["general"],
            ],
            has_non_agent_mentions=False,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@alice:localhost", "calculator and general, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "skip"
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_configured_room_boundary_for_direct_reply(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Unmentioned direct replies must use the same configured-room boundary as routing."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                authorization={
                    "default_room_access": True,
                    "agent_reply_permissions": {
                        "calculator": ["@bob:localhost"],
                        "research": ["@alice:localhost"],
                    },
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
                entity_ids(config, runtime_paths_for(config))["research"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with patch("mindroom.turn_policy.decide_team_formation", new=MagicMock(return_value=TeamResolution.none())):
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@bob:localhost", "can someone help?"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "skip"

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_configured_room_boundary_for_explicit_mention(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Explicit mentions must not let unconfigured bots answer in configured rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths)["calculator"].full_id,
                entity_ids(config, runtime_paths)["research"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[entity_ids(config, runtime_paths)["calculator"]],
            has_non_agent_mentions=False,
        )

        action = await bot._turn_policy.resolve_response_action(
            _policy_dispatch(bot, room, context, "@bob:localhost", "calculator, help"),
            room,
            False,
            has_active_response_for_target=bot._response_runner.has_active_response_for_target,
        )

        assert action.kind == "skip"

    @pytest.mark.asyncio
    async def test_resolve_response_action_allows_explicit_private_agent_mention(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """External triggers may explicitly address one private agent in its bound room."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="CalculatorAgent",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user", root="calculator_data"),
                    ),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[entity_ids(config, runtime_paths)["calculator"].full_id],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[entity_ids(config, runtime_paths)["calculator"]],
            has_non_agent_mentions=False,
        )

        action = await bot._turn_policy.resolve_response_action(
            _policy_dispatch(
                bot,
                room,
                context,
                "@owner:localhost",
                "@CalculatorAgent campground opened",
                source_kind=EXTERNAL_TRIGGER_SOURCE_KIND,
            ),
            room,
            False,
            has_active_response_for_target=bot._response_runner.has_active_response_for_target,
        )

        assert action.kind == "individual"

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_configured_room_boundary_for_team_mention(
        self,
        tmp_path: Path,
    ) -> None:
        """Explicit team mentions must not let unconfigured teams answer in configured rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Ops workflow",
                        agents=["calculator"],
                    ),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        team_user = AgentMatrixUser(
            agent_name="ops",
            user_id=entity_ids(config, runtime_paths)["ops"].full_id,
            display_name="Ops Team",
            password=TEST_PASSWORD,
        )
        bot = TeamBot(
            team_user,
            tmp_path,
            config=config,
            runtime_paths=runtime_paths,
            team_mode="coordinate",
        )
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths)["ops"].full_id,
                entity_ids(config, runtime_paths)["research"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[entity_ids(config, runtime_paths)["ops"]],
            has_non_agent_mentions=False,
        )

        action = await bot._turn_policy.resolve_response_action(
            _policy_dispatch(bot, room, context, "@bob:localhost", "ops, help"),
            room,
            False,
            has_active_response_for_target=bot._response_runner.has_active_response_for_target,
        )

        assert action.kind == "skip"

    @pytest.mark.asyncio
    async def test_resolve_response_action_ignores_non_materializable_owner_candidates(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reject ownership should stay with a live bot instead of a missing requested member."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="AlphaAgent", rooms=["!room:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["alpha"].full_id,
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[
                            entity_ids(config, runtime_paths_for(config))["alpha"],
                            entity_ids(config, runtime_paths_for(config))["calculator"],
                        ],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=entity_ids(config, runtime_paths_for(config))["alpha"],
                                name="alpha",
                                status=TeamMemberStatus.NOT_MATERIALIZABLE,
                            ),
                            TeamResolutionMember(
                                agent=entity_ids(config, runtime_paths_for(config))["calculator"],
                                name="calculator",
                                status=TeamMemberStatus.ELIGIBLE,
                            ),
                        ],
                        eligible_members=[entity_ids(config, runtime_paths_for(config))["calculator"]],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes agent 'alpha' that is not available right now.",
                    ),
                ),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(True),
            ) as mock_decide_agent_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "alpha and calculator, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "reject"
        assert action.rejection_message == "Team request includes agent 'alpha' that is not available right now."
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_ignores_unsupported_non_responders_for_reject_ownership(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Reject ownership should ignore unsupported members that cannot emit the response."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(display_name="AlphaAgent", rooms=["!room:localhost"]),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["alpha"].full_id,
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution(
                        intent=TeamIntent.EXPLICIT_MEMBERS,
                        requested_members=[
                            entity_ids(config, runtime_paths_for(config))["alpha"],
                            entity_ids(config, runtime_paths_for(config))["calculator"],
                        ],
                        member_statuses=[
                            TeamResolutionMember(
                                agent=entity_ids(config, runtime_paths_for(config))["alpha"],
                                name="alpha",
                                status=TeamMemberStatus.UNSUPPORTED_FOR_TEAM,
                            ),
                            TeamResolutionMember(
                                agent=entity_ids(config, runtime_paths_for(config))["calculator"],
                                name="calculator",
                                status=TeamMemberStatus.ELIGIBLE,
                            ),
                        ],
                        eligible_members=[entity_ids(config, runtime_paths_for(config))["calculator"]],
                        outcome=TeamOutcome.REJECT,
                        reason="Team request includes private agent 'alpha'; private agents are only supported in explicit Matrix ad hoc teams with requester identity",
                    ),
                ),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(True),
            ) as mock_decide_agent_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "alpha and calculator, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "reject"
        assert (
            "private agents are only supported in explicit Matrix ad hoc teams with requester identity"
            in action.rejection_message
        )
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_lets_shared_bot_own_private_ad_hoc_team(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Real team resolution should use a live shared owner for private ad hoc teams."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "alpha": AgentConfig(
                        display_name="AlphaAgent",
                        rooms=["!room:localhost"],
                        private=AgentPrivateConfig(per="user", root="alpha_data"),
                    ),
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.orchestrator = MagicMock()
        bot.orchestrator.agent_bots = {"calculator": MagicMock(running=True)}
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                entity_ids(config, runtime_paths_for(config))["alpha"].full_id,
                entity_ids(config, runtime_paths_for(config))["calculator"].full_id,
            ],
        )
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[
                entity_ids(config, runtime_paths_for(config))["alpha"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ],
            has_non_agent_mentions=False,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "alpha and calculator, help"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "team"
        assert action.form_team is not None
        assert action.form_team.outcome is TeamOutcome.TEAM
        assert [member.name for member in action.form_team.member_statuses] == ["alpha", "calculator"]
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_honors_single_agent_team_fallback(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Team formation may degrade to one responder without falling back through decide_agent_response."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        room = _matrix_room(own_user_id=bot.matrix_id.full_id, user_ids=[bot.matrix_id.full_id])
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.bot.TurnPolicy.decide_team_for_sender",
                new=AsyncMock(
                    return_value=TeamResolution.individual(
                        intent=TeamIntent.IMPLICIT_THREAD_TEAM,
                        requested_members=[bot.matrix_id],
                        member_statuses=[],
                        agent=bot.matrix_id,
                    ),
                ),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(False),
            ) as mock_decide_agent_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@user:localhost", "help me"),
                room,
                True,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_human_follow_up_in_active_thread(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Human follow-ups in an actively responding thread should bypass the normal multi-agent skip."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        ids = entity_ids(config, runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                bot.matrix_id.full_id,
                ids[ROUTER_AGENT_NAME].full_id,
                ids["general"].full_id,
            ],
        )
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=ThreadHistoryResult(
                [
                    ResolvedVisibleMessage(
                        sender=ids[ROUTER_AGENT_NAME].full_id,
                        body="routing",
                        timestamp=1,
                        event_id="$router",
                        content={"body": "routing"},
                        thread_id="$thread",
                        latest_event_id="$router",
                    ),
                    ResolvedVisibleMessage(
                        sender=bot.matrix_id.full_id,
                        body="working",
                        timestamp=2,
                        event_id="$agent",
                        content={"body": "working"},
                        thread_id="$thread",
                        latest_event_id="$agent",
                    ),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        envelope = MessageEnvelope(
            source_event_id="$followup",
            target=target,
            body="stop if you see this",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="live"),
        )

        with (
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(return_value=TeamResolution.none()),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(False),
            ) as mock_decide_agent_response,
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=True,
            ) as mock_has_active_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                PreparedDispatch(
                    requester_user_id="@user:localhost",
                    context=context,
                    target=target,
                    correlation_id="$followup",
                    envelope=envelope,
                ),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        mock_decide_agent_response.assert_called_once()
        mock_has_active_response.assert_called_once_with(target)

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_active_follow_up_inside_responder_boundary(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Active response follow-ups must not widen configured rooms to unconfigured bots."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["calculator"].full_id,
                ids["research"].full_id,
            ],
        )
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=ThreadHistoryResult(
                [
                    ResolvedVisibleMessage(
                        sender=ids["calculator"].full_id,
                        body="working",
                        timestamp=1,
                        event_id="$calculator",
                        content={"body": "working"},
                        thread_id="$thread",
                        latest_event_id="$calculator",
                    ),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        envelope = MessageEnvelope(
            source_event_id="$followup",
            target=target,
            body="continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="live"),
        )

        with (
            patch("mindroom.turn_policy.decide_team_formation", new=MagicMock(return_value=TeamResolution.none())),
            patch.object(bot._response_runner, "has_active_response_for_target", return_value=True),
        ):
            action = await bot._turn_policy.resolve_response_action(
                PreparedDispatch(
                    requester_user_id="@user:localhost",
                    context=context,
                    target=target,
                    correlation_id="$followup",
                    envelope=envelope,
                ),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "skip"

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_degraded_active_follow_up_inside_responder_boundary(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Degraded-history active follow-ups must still respect responder candidates."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["calculator"].full_id,
                ids["research"].full_id,
            ],
        )
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=ThreadHistoryResult([], is_full_history=False),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        envelope = MessageEnvelope(
            source_event_id="$followup",
            target=target,
            body="continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="live"),
        )

        with patch.object(bot._response_runner, "has_active_response_for_target", return_value=True):
            action = await bot._turn_policy.resolve_response_action(
                PreparedDispatch(
                    requester_user_id="@user:localhost",
                    context=context,
                    target=target,
                    correlation_id="$followup",
                    envelope=envelope,
                ),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "skip"

    @pytest.mark.asyncio
    async def test_resolve_response_action_keeps_gate_owned_active_thread_follow_up(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Gate-owned active follow-ups should keep active-response treatment after the active turn ends."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        ids = entity_ids(config, runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                bot.matrix_id.full_id,
                ids[ROUTER_AGENT_NAME].full_id,
                ids["general"].full_id,
            ],
        )
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        envelope = MessageEnvelope(
            source_event_id="$followup",
            target=target,
            body="stop if you see this",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

        with (
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(return_value=TeamResolution.none()),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(False),
            ) as mock_decide_agent_response,
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=False,
            ) as mock_has_active_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                PreparedDispatch(
                    requester_user_id="@user:localhost",
                    context=context,
                    target=target,
                    correlation_id="$followup",
                    envelope=envelope,
                ),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        mock_decide_agent_response.assert_called_once()
        mock_has_active_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_uses_active_follow_up_policy_without_erasing_voice(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Gate-owned voice follow-ups should keep voice source kind and active-response policy."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths)
        ids = entity_ids(config, runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                bot.matrix_id.full_id,
                ids[ROUTER_AGENT_NAME].full_id,
                ids["general"].full_id,
            ],
        )
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        envelope = MessageEnvelope(
            source_event_id="$followup",
            target=target,
            body="stop if you see this",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=bot.agent_name,
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
            origin=message_origin(
                sender_id="@user:localhost",
                requester_id="@user:localhost",
                source_kind=VOICE_SOURCE_KIND,
            ),
        )

        with (
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(return_value=TeamResolution.none()),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(False),
            ) as mock_decide_agent_response,
            patch.object(
                bot._response_runner,
                "has_active_response_for_target",
                return_value=False,
            ) as mock_has_active_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                PreparedDispatch(
                    requester_user_id="@user:localhost",
                    context=context,
                    target=target,
                    correlation_id="$followup",
                    envelope=envelope,
                ),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        assert envelope.source_kind == VOICE_SOURCE_KIND
        mock_decide_agent_response.assert_called_once()
        mock_has_active_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_plan_ignores_stale_thread_owner_outside_responder_boundary(self, tmp_path: Path) -> None:
        """Router gating must not treat unconfigured prior participants as configured-room owners."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                    "writer": AgentConfig(display_name="WriterAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=ids[ROUTER_AGENT_NAME].full_id,
            display_name="Router",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths)
        bot.client = AsyncMock()
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids[ROUTER_AGENT_NAME].full_id,
                ids["calculator"].full_id,
                ids["research"].full_id,
                ids["writer"].full_id,
                "@user:localhost",
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=ThreadHistoryResult(
                [
                    ResolvedVisibleMessage(
                        sender=ids["calculator"].full_id,
                        body="old answer",
                        timestamp=1,
                        event_id="$calculator",
                        content={"body": "old answer"},
                        thread_id="$thread",
                        latest_event_id="$calculator",
                    ),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        target = MessageTarget.resolve(room.room_id, "$thread", "$event")
        envelope = MessageEnvelope(
            source_event_id="$event",
            target=target,
            body="continue",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=ROUTER_AGENT_NAME,
            origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="live"),
        )
        event = self._make_handler_event("message", sender="@user:localhost", event_id="$event")
        event.body = "continue"
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=context,
            target=target,
            correlation_id="corr",
            envelope=envelope,
        )

        plan = await bot._turn_policy.plan_router_dispatch(room, event, dispatch)

        assert plan is not None
        assert plan.kind == "route"

    @pytest.mark.asyncio
    async def test_router_pre_ingress_skip_ignores_stale_thread_owner_outside_responder_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        """Router pre-ingress skip must use the same configured-room responder boundary."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent"),
                    "research": AgentConfig(display_name="ResearchAgent", rooms=["!room:localhost"]),
                    "writer": AgentConfig(display_name="WriterAgent", rooms=["!room:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=ids[ROUTER_AGENT_NAME].full_id,
            display_name="Router",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths)
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids[ROUTER_AGENT_NAME].full_id,
                ids["calculator"].full_id,
                ids["research"].full_id,
                ids["writer"].full_id,
                "@user:localhost",
            ],
        )
        event = self._make_handler_event("message", sender="@user:localhost", event_id="$event")
        event.body = "continue"
        event.source = {"content": {"body": "continue"}}
        bot._conversation_cache.get_dispatch_thread_snapshot = AsyncMock(
            return_value=ThreadHistoryResult(
                [
                    ResolvedVisibleMessage(
                        sender=ids["calculator"].full_id,
                        body="old answer",
                        timestamp=1,
                        event_id="$calculator",
                        content={"body": "old answer"},
                        thread_id="$thread",
                        latest_event_id="$calculator",
                    ),
                ],
                is_full_history=True,
            ),
        )

        should_skip = await bot._turn_controller._should_skip_router_before_shared_ingress_work(
            room,
            event,
            requester_user_id="@user:localhost",
            thread_id="$thread",
        )

        assert should_skip is False

    @pytest.mark.asyncio
    async def test_resolve_response_action_requires_explicit_mention_in_multi_human_thread_even_after_prior_team_mentions(
        self,
        tmp_path: Path,
    ) -> None:
        """Untargeted follow-ups in a multi-human thread must not reuse stale thread mentions to form a team."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "synth": AgentConfig(
                        display_name="Synthesis",
                        rooms=["!room:localhost"],
                    ),
                    "reasoner": AgentConfig(
                        display_name="Reasoner",
                        rooms=["!room:localhost"],
                    ),
                    "critic": AgentConfig(
                        display_name="Critic",
                        rooms=["!room:localhost"],
                    ),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot_user = AgentMatrixUser(
            agent_name="synth",
            user_id=ids["synth"].full_id,
            display_name="Synthesis",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(bot_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["synth"].full_id,
                ids["reasoner"].full_id,
                ids["critic"].full_id,
                "@bas:localhost",
                "@maciej:localhost",
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=ThreadHistoryResult(
                [
                    ResolvedVisibleMessage(
                        sender="@bas:localhost",
                        body="Team, please assess this",
                        timestamp=1,
                        event_id="$m1",
                        content={
                            "body": "Team, please assess this",
                            "m.mentions": {
                                "user_ids": [
                                    ids["synth"].full_id,
                                    ids["reasoner"].full_id,
                                    ids["critic"].full_id,
                                ],
                            },
                        },
                        thread_id="$thread",
                        latest_event_id="$m1",
                    ),
                    ResolvedVisibleMessage(
                        sender="@maciej:localhost",
                        body="I fixed two issues",
                        timestamp=2,
                        event_id="$m2",
                        content={"body": "I fixed two issues"},
                        thread_id="$thread",
                        latest_event_id="$m2",
                    ),
                ],
                is_full_history=True,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with (
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(side_effect=AssertionError("team formation should be skipped")),
            ) as mock_decide_team_formation,
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(False),
            ) as mock_decide_agent_response,
        ):
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@bas:localhost", "I fixed two issues"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "skip"
        mock_decide_team_formation.assert_not_called()
        mock_decide_agent_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_response_action_continues_single_agent_thread_when_policy_history_partial(
        self,
        tmp_path: Path,
    ) -> None:
        """Partial policy history should not drop ordinary single-agent thread follow-ups."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "synth": AgentConfig(display_name="Synthesis", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot_user = AgentMatrixUser(
            agent_name="synth",
            user_id=ids["synth"].full_id,
            display_name="Synthesis",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(bot_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["synth"].full_id,
                "@bas:localhost",
                "@maciej:localhost",
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=thread_history_result(
                [
                    ResolvedVisibleMessage(
                        sender="@bas:localhost",
                        body="Initial question",
                        timestamp=1,
                        event_id="$m1",
                        content={"body": "Initial question"},
                        thread_id="$thread",
                        latest_event_id="$m1",
                    ),
                ],
                is_full_history=False,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_model_history_refresh=True,
        )

        action = await bot._turn_policy.resolve_response_action(
            _policy_dispatch(bot, room, context, "@bas:localhost", "Follow-up"),
            room,
            False,
            has_active_response_for_target=bot._response_runner.has_active_response_for_target,
        )

        assert action.kind == "individual"

    @pytest.mark.asyncio
    async def test_resolve_response_action_skips_multi_agent_thread_when_policy_history_partial(
        self,
        tmp_path: Path,
    ) -> None:
        """Partial policy history should fail closed in multi-responder rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "synth": AgentConfig(display_name="Synthesis", rooms=["!room:localhost"]),
                    "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot_user = AgentMatrixUser(
            agent_name="synth",
            user_id=ids["synth"].full_id,
            display_name="Synthesis",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(bot_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["synth"].full_id,
                ids["research"].full_id,
                "@bas:localhost",
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=thread_history_result(
                [
                    ResolvedVisibleMessage(
                        sender="@bas:localhost",
                        body="Initial question",
                        timestamp=1,
                        event_id="$m1",
                        content={"body": "Initial question"},
                        thread_id="$thread",
                        latest_event_id="$m1",
                    ),
                ],
                is_full_history=False,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_model_history_refresh=True,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@bas:localhost", "Follow-up"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "skip"
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_response_action_allows_sole_responder_when_policy_history_degraded(
        self,
        tmp_path: Path,
    ) -> None:
        """Unavailable policy history should not silence the sole visible responder."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "synth": AgentConfig(display_name="Synthesis", rooms=["!room:localhost"]),
                },
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(config)
        ids = entity_ids(config, runtime_paths)
        bot_user = AgentMatrixUser(
            agent_name="synth",
            user_id=ids["synth"].full_id,
            display_name="Synthesis",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(bot_user, tmp_path, config=config, runtime_paths=runtime_paths)
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["synth"].full_id,
                "@bas:localhost",
            ],
        )
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=thread_history_result(
                [
                    ResolvedVisibleMessage(
                        sender="@bas:localhost",
                        body="Follow-up",
                        timestamp=1,
                        event_id="$m1",
                        content={"body": "Follow-up"},
                        thread_id="$thread",
                        latest_event_id="$m1",
                    ),
                ],
                is_full_history=False,
                diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True},
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )

        with patch(
            "mindroom.turn_policy.decide_agent_response",
            return_value=AgentResponseDecision(True),
        ) as mock_decide_agent_response:
            action = await bot._turn_policy.resolve_response_action(
                _policy_dispatch(bot, room, context, "@bas:localhost", "Follow-up"),
                room,
                False,
                has_active_response_for_target=bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        mock_decide_agent_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_skips_unmentioned_thread_when_policy_history_degraded(
        self,
        tmp_path: Path,
    ) -> None:
        """Unavailable policy history should not let the router claim a thread."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        ids = entity_ids(config, runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["calculator"].full_id,
                ids["general"].full_id,
                ids[ROUTER_AGENT_NAME].full_id,
                "@bas:localhost",
            ],
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event"
        event.body = "Follow-up"
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=thread_history_result(
                [
                    ResolvedVisibleMessage(
                        sender="@bas:localhost",
                        body="Follow-up",
                        timestamp=1,
                        event_id="$m1",
                        content={"body": "Follow-up"},
                        thread_id="$thread",
                        latest_event_id="$m1",
                    ),
                ],
                is_full_history=False,
                diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True},
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        dispatch = PreparedDispatch(
            requester_user_id="@bas:localhost",
            context=context,
            target=(dispatch_target := MessageTarget.resolve(room.room_id, "$thread", event.event_id)),
            correlation_id="corr-degraded-router-policy",
            envelope=_hook_envelope(body="Follow-up", source_event_id=event.event_id, target=dispatch_target),
        )

        plan = await bot._turn_policy.plan_router_dispatch(room, event, dispatch)

        assert plan == _DispatchPlan(kind="ignore", ignore_reason="router")

    @pytest.mark.asyncio
    async def test_router_skips_unmentioned_thread_when_policy_history_partial(
        self,
        tmp_path: Path,
    ) -> None:
        """Partial policy history should not let the router claim a thread."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!room:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!room:localhost"]),
                },
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(router_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        ids = entity_ids(config, runtime_paths_for(config))
        room = _matrix_room(
            own_user_id=bot.matrix_id.full_id,
            user_ids=[
                ids["calculator"].full_id,
                ids["general"].full_id,
                ids[ROUTER_AGENT_NAME].full_id,
                "@bas:localhost",
            ],
        )
        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event"
        event.body = "Follow-up"
        context = MessageContext(
            am_i_mentioned=False,
            is_thread=True,
            thread_id="$thread",
            thread_history=thread_history_result(
                [
                    ResolvedVisibleMessage(
                        sender="@bas:localhost",
                        body="Follow-up",
                        timestamp=1,
                        event_id="$m1",
                        content={"body": "Follow-up"},
                        thread_id="$thread",
                        latest_event_id="$m1",
                    ),
                ],
                is_full_history=False,
            ),
            mentioned_agents=[],
            has_non_agent_mentions=False,
            requires_model_history_refresh=True,
        )
        dispatch = PreparedDispatch(
            requester_user_id="@bas:localhost",
            context=context,
            target=(dispatch_target := MessageTarget.resolve(room.room_id, "$thread", event.event_id)),
            correlation_id="corr-partial-router-policy",
            envelope=_hook_envelope(body="Follow-up", source_event_id=event.event_id, target=dispatch_target),
        )

        plan = await bot._turn_policy.plan_router_dispatch(room, event, dispatch)

        assert plan == _DispatchPlan(kind="ignore", ignore_reason="router")
