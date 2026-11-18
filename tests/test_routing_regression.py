"""Regression tests for routing behavior.

These tests ensure that fixed bugs don't resurface, particularly:
1. Router should NOT respond when any agent is mentioned
2. Only mentioned agents should respond
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.hooks import MessageEnvelope
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.routing import ResponderSelection, suggest_responder_for_message
from mindroom.teams.formation import TeamOutcome, TeamResolution
from mindroom.thread_utils import AgentResponseDecision
from mindroom.turn_policy import PreparedDispatch, TurnPolicy, _ResponderAvailability
from tests.access_schema_support import with_current_room_member_access
from tests.authorization_helpers import (
    make_test_turn_policy_deps,
)
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    drain_coalescing,
    install_model_providers,
    install_runtime_journal_support,
    make_matrix_client_mock,
    make_visible_message,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.identity_helpers import actual_entity_usernames, entity_ids, entity_name_for_id, persist_entity_accounts
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import streamed_answer
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.orchestrator import _MultiAgentOrchestrator
    from mindroom.turn_origin import TurnOrigin


def _runtime_bound_config(config: Config, runtime_root: Path) -> Config:
    """Return a runtime-bound config for routing regression tests."""
    return bind_runtime_paths(config, test_runtime_paths(runtime_root))


def _policy_dispatch(
    *,
    agent_name: str,
    room_id: str,
    context: MessageContext,
    requester_user_id: str,
    body: str,
    source_event_id: str = "$event",
) -> PreparedDispatch:
    """Build a complete prepared dispatch for direct turn-policy tests."""
    target = MessageTarget.resolve(room_id, context.thread_id, source_event_id)
    envelope = MessageEnvelope(
        source_event_id=source_event_id,
        target=target,
        body=body,
        attachment_ids=(),
        mentioned_agents=tuple(context.mentioned_agents),
        agent_name=agent_name,
        origin=message_origin(
            sender_id=requester_user_id,
            requester_id=requester_user_id,
            source_kind="message",
        ),
    )
    return PreparedDispatch(
        requester_user_id=requester_user_id,
        context=context,
        target=target,
        correlation_id=source_event_id,
        envelope=envelope,
    )


def setup_test_bot(
    agent: AgentMatrixUser,
    storage_path: Path,
    room_id: str,
    config: Config | None = None,
    orchestrator: _MultiAgentOrchestrator | None = None,
) -> AgentBot:
    """Set up a non-streaming test bot with all required mocks, in ``orchestrator`` when one is given."""
    if config is None:
        agents: dict[str, AgentConfig] = {}
        if agent.agent_name != "router":
            agents[agent.agent_name] = AgentConfig(
                display_name=agent.display_name,
                rooms=[room_id],
            )
        config = _runtime_bound_config(
            with_current_room_member_access(
                Config(
                    agents=agents,
                    models={"default": ModelConfig(provider="openai", id="test-model")},
                    router=RouterConfig(model="default"),
                    authorization={},
                ),
            ),
            storage_path,
        )
    else:
        try:
            runtime_paths_for(config)
        except KeyError:
            config = _runtime_bound_config(config, storage_path)

    runtime_paths = runtime_paths_for(config)
    usernames = actual_entity_usernames(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    for alias in [*config.agents, *config.teams, "router"]:
        if managed_account_key(alias) in state.accounts:
            usernames.pop(alias, None)
    if agent.user_id is not None:
        account_key = managed_account_key(agent.agent_name)
        if account_key not in state.accounts:
            usernames[agent.agent_name] = MatrixID.parse(agent.user_id).username
    persist_entity_accounts(config, runtime_paths, usernames=usernames)
    config.defaults.enable_streaming = False

    bot = make_test_agent_bot(
        agent,
        config=config,
        runtime_paths=runtime_paths,
        rooms=[room_id],
        orchestrator=orchestrator,
    )
    install_direct_response_admission(bot)
    bot.client = make_matrix_client_mock(user_id=agent.user_id)
    return install_runtime_journal_support(bot)


@pytest.mark.asyncio
@patch("mindroom.routing.suggest_responder", new_callable=AsyncMock)
async def test_suggest_responder_for_message_returns_aliases_for_actual_ids(
    mock_suggest_responder: AsyncMock,
    tmp_path: Path,
) -> None:
    """Routing should expose configured aliases even when Matrix IDs have drifted."""
    config = _runtime_bound_config(
        Config(
            agents={
                "news": AgentConfig(display_name="News"),
                "facts": AgentConfig(display_name="Facts"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "news": "actual_news", "facts": "actual_facts"},
    )
    mock_suggest_responder.return_value = ResponderSelection("news")

    result = await suggest_responder_for_message(
        "what happened?",
        [ids["news"], ids["facts"]],
        config,
        runtime_paths,
        [make_visible_message(sender="@actual_facts:localhost", body="Prior context")],
        room_id="!room:localhost",
        thread_id="$thread",
    )

    assert result == ResponderSelection("news")
    mock_suggest_responder.assert_awaited_once()
    assert mock_suggest_responder.await_args.args[1] == ["news", "facts"]
    assert mock_suggest_responder.await_args.args[4][0].sender == "facts"


@pytest.mark.asyncio
@patch("mindroom.routing.suggest_responder", new_callable=AsyncMock)
async def test_suggest_responder_for_message_keeps_each_human_senders_matrix_id(
    mock_suggest_responder: AsyncMock,
    tmp_path: Path,
) -> None:
    """Two people on one homeserver reach the router as two speakers, not as their shared server."""
    config = _runtime_bound_config(
        Config(
            agents={"news": AgentConfig(display_name="News")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    mock_suggest_responder.return_value = ResponderSelection("news")

    await suggest_responder_for_message(
        "what happened?",
        [ids["news"]],
        config,
        runtime_paths,
        [
            make_visible_message(sender="@alice:example.org", body="Did the deploy finish?"),
            make_visible_message(sender="@bob:example.org", body="Not yet."),
        ],
        room_id="!room:localhost",
        thread_id="$thread",
    )

    context = mock_suggest_responder.await_args.args[4]
    assert [message.sender for message in context] == ["@alice:example.org", "@bob:example.org"]


def test_active_response_follow_up_uses_actual_managed_sender_ids(tmp_path: Path) -> None:
    """Active-response follow-up classification should not trust stale generated-looking IDs."""
    config = _runtime_bound_config(
        Config(
            agents={
                "research": AgentConfig(display_name="Research"),
                "news": AgentConfig(display_name="News"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "research": "actual_research", "news": "actual_news"},
    )
    policy = TurnPolicy(
        make_test_turn_policy_deps(
            runtime=SimpleNamespace(config=config, orchestrator=None, client=None),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="research",
            matrix_id=ids["research"],
        ),
    )
    context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$thread",
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    target = MessageTarget.resolve("!room:localhost", "$thread", "$msg")

    def envelope(
        sender_id: str,
        *,
        requester_id: str | None = None,
        origin: TurnOrigin | None = None,
    ) -> MessageEnvelope:
        resolved_requester_id = requester_id or sender_id
        return MessageEnvelope(
            source_event_id="$msg",
            target=target,
            body="follow up",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="research",
            origin=origin
            or message_origin(
                sender_id=sender_id,
                requester_id=resolved_requester_id,
                sender_entity_name=entity_name_for_id(MatrixID.parse(sender_id), config, runtime_paths),
                requester_entity_name=entity_name_for_id(MatrixID.parse(resolved_requester_id), config, runtime_paths),
            ),
        )

    assert policy._should_queue_follow_up_in_active_response_thread(
        context=context,
        target=target,
        source_envelope=envelope("@mindroom_news:localhost"),
        has_active_response_for_target=lambda _target: True,
    )
    assert not policy._should_queue_follow_up_in_active_response_thread(
        context=context,
        target=target,
        source_envelope=envelope("@actual_news:localhost"),
        has_active_response_for_target=lambda _target: True,
    )
    assert policy._should_queue_follow_up_in_active_response_thread(
        context=context,
        target=target,
        source_envelope=envelope(
            ids["router"].full_id,
            requester_id="@human:localhost",
            origin=message_origin(
                sender_id=ids["router"].full_id,
                requester_id="@human:localhost",
                sender_entity_name="router",
                source_kind="trusted_internal_relay",
                original_sender="@human:localhost",
                trusted_user_relay=True,
            ),
        ),
        has_active_response_for_target=lambda _target: True,
    )


def test_team_request_responder_filtering_uses_actual_member_ids(tmp_path: Path) -> None:
    """Team responder filtering should map actual IDs to aliases and drop unknown IDs."""
    config = _runtime_bound_config(
        Config(
            agents={
                "worker": AgentConfig(display_name="Worker"),
                "idle": AgentConfig(display_name="Idle"),
            },
            teams={"squad": TeamConfig(agents=["worker"], display_name="Squad", role="Coordinate worker responses")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(
        config,
        runtime_paths,
        usernames={
            "router": "actual_router",
            "worker": "actual_worker",
            "idle": "actual_idle",
            "squad": "actual_squad",
        },
    )
    policy = TurnPolicy(
        make_test_turn_policy_deps(
            runtime=SimpleNamespace(config=config, orchestrator=None, client=None),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="worker",
            matrix_id=ids["worker"],
        ),
    )

    filtered = policy._filter_materializable_responders(
        [
            ids["worker"],
            ids["idle"],
            ids["squad"],
            MatrixID.from_username("mindroom_missing", "localhost"),
        ],
        _ResponderAvailability(materializable_agent_names={"worker"}, live_entity_names=frozenset({"squad"})),
    )

    assert filtered == [ids["worker"], ids["squad"]]
    # A configured team whose own bot is not running cannot answer, however materializable its members are.
    not_running = _ResponderAvailability(materializable_agent_names={"worker"}, live_entity_names=frozenset())
    assert policy._filter_materializable_responders([ids["squad"]], not_running) == []


@pytest.fixture
def mock_research_agent() -> AgentMatrixUser:
    """Create a mock research agent user."""
    return AgentMatrixUser(
        agent_name="research",
        password=TEST_PASSWORD,
        display_name="MindRoomResearch",
        user_id="@mindroom_research:localhost",
    )


@pytest.fixture
def mock_news_agent() -> AgentMatrixUser:
    """Create a mock news agent user."""
    return AgentMatrixUser(
        agent_name="news",
        password=TEST_PASSWORD,
        display_name="MindRoomNews",
        user_id="@mindroom_news:localhost",
    )


class TestRoutingRegression:
    """Regression tests for routing behavior."""

    @pytest.mark.asyncio
    @patch("mindroom.response_attempt.is_user_online")
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.turn_policy.suggest_responder_for_message")
    async def test_router_does_not_respond_when_agent_mentioned(
        self,
        mock_suggest_responder: AsyncMock,
        mock_stream_agent_response: MagicMock,
        mock_is_user_online: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that router doesn't activate when an agent is directly mentioned.

        Regression test for issue where both mentioned agent AND router-selected
        agent would respond to the same message.
        """
        test_room_id = "!research:localhost"

        # Mock user as online for stop button to show
        mock_is_user_online.return_value = True

        # Set up research bot (the one being mentioned)
        research_bot = setup_test_bot(mock_research_agent, tmp_path, test_room_id)

        # Set up news bot
        news_bot = setup_test_bot(mock_news_agent, tmp_path, test_room_id)

        # Mock AI responses
        mock_ai_response = AsyncMock()
        mock_stream_agent_response.side_effect = streamed_answer(mock_ai_response)
        mock_ai_response.return_value = "I can help with that research!"
        mock_suggest_responder.return_value = ResponderSelection("news")  # A selection would pick news

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        research_bot.client.room_send.return_value = mock_send_response
        news_bot.client.room_send.return_value = mock_send_response

        # Create room with both agents
        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {
            mock_research_agent.user_id: MagicMock(),
            mock_news_agent.user_id: MagicMock(),
        }

        # User mentions research agent specifically
        message_event = MagicMock(spec=nio.RoomMessageText)
        message_event.sender = "@user:localhost"
        message_event.body = "@mindroom_research:localhost what can you do?"
        message_event.event_id = "$user_msg_123"
        message_event.server_timestamp = 1000
        message_event.source = {
            "content": {
                "body": "@mindroom_research:localhost what can you do?",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        }

        # Process with research bot - SHOULD respond
        await research_bot._on_message(mock_room, message_event)
        await drain_coalescing(research_bot)
        assert research_bot.client.room_send.call_count == 3  # thinking + 🛑 + final
        assert mock_ai_response.call_count == 1

        # Process with news bot - should NOT respond and NOT use router
        await news_bot._on_message(mock_room, message_event)
        await drain_coalescing(news_bot)
        assert news_bot.client.room_send.call_count == 0
        # Router should NOT have been called
        assert mock_suggest_responder.call_count == 0

    def test_router_original_sender_metadata_requires_canonical_relay_source_kind(
        self,
        tmp_path: Path,
    ) -> None:
        """Router relays only honor provenance when canonical relay metadata is present."""
        test_room_id = "!router-mentions:localhost"
        test_config = _runtime_bound_config(
            with_current_room_member_access(
                Config(
                    agents={
                        "alpha": AgentConfig(display_name="AlphaAgent", rooms=[test_room_id]),
                        "beta": AgentConfig(display_name="BetaAgent", rooms=[test_room_id]),
                    },
                    room_models={},
                    models={"default": ModelConfig(provider="openai", id="test-model")},
                    router=RouterConfig(model="default"),
                    authorization={},
                ),
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(test_config)
        ids = entity_ids(test_config, runtime_paths)
        router_bot = setup_test_bot(
            AgentMatrixUser(
                agent_name="router",
                password=TEST_PASSWORD,
                display_name="RouterAgent",
                user_id=ids["router"].full_id,
            ),
            tmp_path,
            test_room_id,
            config=test_config,
        )

        router_sender = ids["router"].full_id

        def requester_for(
            content: dict[str, object],
            *,
            original_sender: str = "@stale:localhost",
            source_kind: str | None = None,
        ) -> str:
            content_with_metadata: dict[str, object] = {ORIGINAL_SENDER_KEY: original_sender, **content}
            if source_kind is not None:
                content_with_metadata[SOURCE_KIND_KEY] = source_kind
            return router_bot._ingress_validator.classify_source(
                router_sender,
                {"content": content_with_metadata},
                room_id="!room:localhost",
            ).lane_requester_id

        untrusted_contents: list[dict[str, object]] = [
            {"body": "@alice Sorry, I could not route this request."},
            {"body": "No configured target", "m.mentions": {"user_ids": ["@alice:localhost"]}},
        ]
        for content in untrusted_contents:
            assert requester_for(content) == router_sender

        trusted_contents: list[dict[str, object]] = [
            {"body": "@alpha could you help with this?"},
            {"body": f"{ids['beta'].full_id} could you help with this?"},
            {"body": "Beta could help", "m.mentions": {"user_ids": [ids["beta"].full_id]}},
        ]
        for content in trusted_contents:
            assert requester_for(content) == router_sender
            assert requester_for(content, source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND) == "@stale:localhost"
            assert (
                requester_for(
                    content,
                    original_sender=ids["router"].full_id,
                    source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                )
                == router_sender
            )

    @pytest.mark.asyncio
    async def test_direct_response_candidates_filter_configured_room_by_live_state(
        self,
        tmp_path: Path,
    ) -> None:
        """Direct response planning should use the same filtered configured-room candidates."""
        test_room_id = "!live-direct:localhost"
        test_config = _runtime_bound_config(
            with_current_room_member_access(
                Config(
                    agents={
                        "alpha": AgentConfig(display_name="AlphaAgent", rooms=[test_room_id]),
                        "beta": AgentConfig(display_name="BetaAgent", rooms=[test_room_id]),
                        "writer": AgentConfig(display_name="WriterAgent"),
                    },
                    teams={
                        "ops": TeamConfig(
                            display_name="Ops Team",
                            role="Operations",
                            agents=["beta"],
                            rooms=[test_room_id],
                        ),
                    },
                    room_models={},
                    models={"default": ModelConfig(provider="openai", id="test-model")},
                    authorization={},
                ),
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(test_config)
        ids = entity_ids(test_config, runtime_paths)
        alpha_user = AgentMatrixUser(
            agent_name="alpha",
            password=TEST_PASSWORD,
            display_name="AlphaAgent",
            user_id=ids["alpha"].full_id,
        )
        runtime = ready_runtime(test_config, runtime_paths)
        runtime.unavailable = {"beta"}
        alpha_bot = setup_test_bot(alpha_user, tmp_path, test_room_id, config=test_config, orchestrator=runtime)
        room = nio.MatrixRoom(test_room_id, ids["alpha"].full_id)
        room.add_member(ids["alpha"].full_id, "AlphaAgent", None)
        room.add_member(ids["beta"].full_id, "BetaAgent", None)
        room.add_member(ids["writer"].full_id, "WriterAgent", None)
        room.add_member(ids["ops"].full_id, "Ops Team", None)
        room.add_member("@user:localhost", "User", None)
        room.members_synced = True
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
                "mindroom.turn_policy.decide_team_formation",
                new=MagicMock(return_value=TeamResolution.none()),
            ),
            patch(
                "mindroom.turn_policy.decide_agent_response",
                return_value=AgentResponseDecision(True),
            ) as mock_decide_agent_response,
        ):
            action = await alpha_bot._turn_policy._resolve_response_action(
                _policy_dispatch(
                    agent_name=alpha_bot.agent_name,
                    room_id=room.room_id,
                    context=context,
                    requester_user_id="@user:localhost",
                    body="can anyone help?",
                ),
                room,
                has_active_response_for_target=alpha_bot._response_runner.has_active_response_for_target,
            )

        assert action.kind == "individual"
        candidate_names = [
            entity_name_for_id(candidate, test_config, runtime_paths)
            for candidate in mock_decide_agent_response.call_args.kwargs["available_responders_in_room"]
        ]
        assert candidate_names == ["alpha"]

    @pytest.mark.asyncio
    async def test_explicit_configured_team_mention_rejects_when_member_bot_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """A live team bot must surface configured-team rejection even if members are unavailable."""
        test_room_id = "!team-reject:localhost"
        test_config = _runtime_bound_config(
            with_current_room_member_access(
                Config(
                    agents={
                        "alpha": AgentConfig(display_name="AlphaAgent"),
                    },
                    teams={
                        "ops": TeamConfig(
                            display_name="Ops Team",
                            role="Operations",
                            agents=["alpha"],
                            rooms=[test_room_id],
                        ),
                    },
                    room_models={},
                    models={"default": ModelConfig(provider="openai", id="test-model")},
                    authorization={},
                ),
            ),
            tmp_path,
        )
        runtime_paths = runtime_paths_for(test_config)
        ids = entity_ids(test_config, runtime_paths)
        team_user = AgentMatrixUser(
            agent_name="ops",
            password=TEST_PASSWORD,
            display_name="Ops Team",
            user_id=ids["ops"].full_id,
        )
        runtime = ready_runtime(test_config, runtime_paths)
        runtime.unavailable = {"alpha"}
        bot = make_test_agent_bot(team_user, rooms=[test_room_id], orchestrator=runtime)
        room = nio.MatrixRoom(test_room_id, ids["ops"].full_id)
        room.add_member(ids["ops"].full_id, "Ops Team", None)
        room.add_member("@user:localhost", "User", None)
        room.members_synced = True
        context = MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[ids["ops"]],
            has_non_agent_mentions=False,
        )

        action = await bot._turn_policy._resolve_response_action(
            _policy_dispatch(
                agent_name=bot.agent_name,
                room_id=room.room_id,
                context=context,
                requester_user_id="@user:localhost",
                body="ops, help",
            ),
            room,
            has_active_response_for_target=bot._response_runner.has_active_response_for_target,
        )

        assert action.kind == "reject"
        assert action.form_team is not None
        assert action.form_team.outcome is TeamOutcome.REJECT
        assert action.rejection_message == (
            "Team 'ops' includes agent 'alpha' that could not be materialized for this request."
        )

    @pytest.mark.asyncio
    @patch("mindroom.teams.members.resolve_agent_knowledge_access")
    @patch("mindroom.response_runner.stream_agent_response")
    @patch("mindroom.config.main.load_config")
    async def test_multiple_mentions_each_responds_once(
        self,
        mock_from_yaml: MagicMock,
        mock_stream_agent_response: MagicMock,
        mock_resolve_agent_knowledge_access: MagicMock,
        mock_research_agent: AgentMatrixUser,
        mock_news_agent: AgentMatrixUser,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that when multiple agents are mentioned, each responds exactly once."""
        # Create a mock config with proper models
        mock_config = _runtime_bound_config(
            with_current_room_member_access(
                Config(
                    agents={
                        "research": AgentConfig(display_name="ResearchAgent", rooms=["!research:localhost"]),
                        "news": AgentConfig(display_name="NewsAgent", rooms=["!research:localhost"]),
                    },
                    teams={},
                    room_models={},
                    models={"default": ModelConfig(provider="anthropic", id="claude-haiku-4-5")},
                    authorization={},
                ),
            ),
            tmp_path,
        )
        mock_from_yaml.return_value = mock_config

        # Get the actual domain from config
        domain = mock_config.get_domain(runtime_paths_for(mock_config))

        # Update mock agents to use the correct domain
        mock_research_agent.user_id = f"@mindroom_research:{domain}"
        mock_news_agent.user_id = f"@mindroom_news:{domain}"

        mock_resolve_agent_knowledge_access.return_value = _KnowledgeResolution(knowledge=None)
        # Every model builds as one scripted model: it chooses the team mode, then answers as the team's leader on
        # its own; the members are never asked.
        team_model = ScriptedModel(
            [answer(json.dumps({"mode": "collaborate", "reasoning": "Both weigh in"})), answer("Team response")],
        )
        install_model_providers(monkeypatch, {"default": team_model})

        test_room_id = "!research:localhost"

        # Set up both bots in one runtime, which team formation reads the live config from
        runtime = ready_runtime(mock_config, runtime_paths_for(mock_config))
        research_bot = setup_test_bot(
            mock_research_agent,
            tmp_path,
            test_room_id,
            config=mock_config,
            orchestrator=runtime,
        )
        news_bot = setup_test_bot(mock_news_agent, tmp_path, test_room_id, config=mock_config, orchestrator=runtime)
        research_bot.running = True
        news_bot.running = True

        # Mock AI responses; the team's leader is scripted above.
        mock_ai_response = AsyncMock()
        mock_stream_agent_response.side_effect = streamed_answer(mock_ai_response)
        mock_ai_response.side_effect = ["Research response!", "News response!"]

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$response_123"
        research_bot.client.room_send.return_value = mock_send_response
        news_bot.client.room_send.return_value = mock_send_response

        # Create room
        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.users = {
            mock_research_agent.user_id: MagicMock(),
            mock_news_agent.user_id: MagicMock(),
        }

        # User mentions BOTH agents
        message_event = MagicMock(spec=nio.RoomMessageText)
        message_event.sender = f"@user:{domain}"
        message_event.body = f"@mindroom_research:{domain} and @mindroom_news:{domain}, what do you think?"
        message_event.event_id = "$user_msg_789"
        message_event.server_timestamp = 1000
        message_event.source = {
            "content": {
                "body": f"@mindroom_research:{domain} and @mindroom_news:{domain}, what do you think?",
                "m.mentions": {"user_ids": [f"@mindroom_research:{domain}", f"@mindroom_news:{domain}"]},
            },
        }

        # Process with both bots
        await research_bot._on_message(mock_room, message_event)
        await drain_coalescing(research_bot)
        await news_bot._on_message(mock_room, message_event)
        await drain_coalescing(news_bot)

        # With simplified team behavior: multiple mentions should form a team
        # The alphabetically first agent (news) handles team formation
        # The other agent (research) does not respond individually
        assert research_bot.client.room_send.call_count == 0  # No individual response
        assert news_bot.client.room_send.call_count == 2  # Team response (thinking + final)
        # The team mode was chosen once, then the team formed once.
        assert [request.output_schema is not None for request in team_model.requests] == [True, False]

    @pytest.mark.asyncio
    @patch("mindroom.response_attempt.is_user_online")
    @patch("mindroom.response_runner.stream_agent_response")
    async def test_router_message_has_completion_marker(
        self,
        mock_stream_agent_response: MagicMock,
        mock_is_user_online: AsyncMock,
        mock_research_agent: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that router messages trigger responses from mentioned agents.

        Regression test for potential issue where router mentions an agent but
        that agent ignores it.
        """
        test_room_id = "!research:localhost"

        # Mock user as online for stop button to show
        mock_is_user_online.return_value = True

        # Create router agent
        router_agent = AgentMatrixUser(
            agent_name="router",
            password=TEST_PASSWORD,
            display_name="RouterAgent",
            user_id="@mindroom_router:localhost",
        )

        # Set up router bot
        router_bot = setup_test_bot(router_agent, tmp_path, test_room_id)

        # Set up research bot (will be mentioned by router)
        research_bot = setup_test_bot(mock_research_agent, tmp_path, test_room_id)

        # Mock AI response
        mock_ai_response = AsyncMock()
        mock_stream_agent_response.side_effect = streamed_answer(mock_ai_response)
        mock_ai_response.return_value = "I can help with that research question!"

        # Mock successful room_send
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        mock_send_response.event_id = "$router_msg"
        router_bot.client.room_send.return_value = mock_send_response
        research_bot.client.room_send.return_value = mock_send_response

        # Create room
        mock_room = MagicMock()
        mock_room.room_id = test_room_id

        # Simulate router message from router agent mentioning research
        # The router sends its messages
        router_message = MagicMock(spec=nio.RoomMessageText)
        router_message.sender = "@mindroom_router:localhost"
        router_message.body = "@research could you help with this?"
        router_message.event_id = "$router_msg"
        router_message.server_timestamp = 1000
        router_message.source = {
            "content": {
                "body": "@research could you help with this?",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        }

        # Process router message with research bot
        await research_bot._on_message(mock_room, router_message)
        await drain_coalescing(research_bot)

        # Research bot SHOULD respond
        assert research_bot.client.room_send.call_count == 3  # thinking + 🛑 + final
        assert mock_ai_response.call_count == 1
