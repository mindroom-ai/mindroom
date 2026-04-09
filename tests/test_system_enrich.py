"""Tests for the system:enrich hook framework."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.models.message import Message
from agno.models.ollama import Ollama
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team import Team

from mindroom.ai import _prepare_agent_and_prompt
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.delivery_gateway import DeliveryResult
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_SYSTEM_ENRICH,
    EnrichmentItem,
    HookRegistry,
    MessageEnvelope,
    SystemEnrichContext,
    emit_collect,
    hook,
    render_system_enrichment_block,
    strip_enrichment_from_session_storage,
    strip_system_enrichment_block,
)
from mindroom.hooks.types import RESERVED_EVENT_NAMESPACES, default_timeout_ms_for_event, validate_event_name
from mindroom.logging_config import get_logger
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_coordinator import ResponseRequest
from mindroom.team_runtime_resolution import ResolvedExactTeamMembers
from mindroom.teams import TeamMode, build_materialized_team_instance, prepare_materialized_team_execution
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    patch_response_coordinator_module,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _FakePreparedExecution:
    final_prompt: str
    unseen_event_ids: list[str]
    compaction_outcomes: list[object]
    replay_plan: object | None = None
    replays_persisted_history: bool = False


@dataclass
class _FakeStorage:
    session: AgentSession | TeamSession | None
    upserted_session: AgentSession | TeamSession | None = None

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession | TeamSession) -> None:
        self.upserted_session = session


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="CodeAgent", role="Write code", rooms=["!room:localhost"]),
                "research": AgentConfig(
                    display_name="ResearchAgent",
                    role="Do research",
                    rooms=["!room:localhost"],
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        runtime_paths,
    )


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _envelope(
    *,
    agent_name: str = "code",
    room_id: str = "!room:localhost",
    body: str = "hello",
) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$event",
        room_id=room_id,
        target=MessageTarget.resolve(room_id, "$thread", "$event"),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
        source_kind="message",
    )


def _system_context(tmp_path: Path, *, room_id: str = "!room:localhost") -> SystemEnrichContext:
    config = _config(tmp_path)
    return SystemEnrichContext(
        event_name=EVENT_SYSTEM_ENRICH,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.system_enrich").bind(event_name=EVENT_SYSTEM_ENRICH),
        correlation_id="corr-system-enrich",
        envelope=_envelope(room_id=room_id),
        target_entity_name="code",
        target_member_names=("research",),
    )


def _agent(agent_id: str, display_name: str) -> Agent:
    return Agent(
        id=agent_id,
        name=display_name,
        role=f"{display_name} role",
        model=Ollama(id="test-model"),
        instructions=["Stay concise."],
    )


def _agent_system_message(agent: Agent) -> str:
    message = agent.get_system_message(
        session=AgentSession(session_id="session-1", agent_id=agent.id),
        run_context=RunContext(run_id="run-1", session_id="session-1", session_state={}),
        tools=None,
        add_session_state_to_context=False,
    )
    assert message is not None
    assert message.content is not None
    return str(message.content)


def _team_system_message(team: Team) -> str:
    message = team.get_system_message(
        session=TeamSession(session_id="session-1", team_id=team.id or "team"),
        run_context=RunContext(run_id="run-1", session_id="session-1", session_state={}),
        tools=None,
        add_session_state_to_context=False,
    )
    assert message is not None
    assert message.content is not None
    return str(message.content)


def _make_bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="code",
        user_id="@mindroom_code:localhost",
        display_name="CodeAgent",
        password=TEST_PASSWORD,
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.client = MagicMock()
    bot.client.user_id = agent_user.user_id
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
    bot._handle_interactive_question = AsyncMock()
    return bot


@asynccontextmanager
async def _noop_typing_indicator(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def test_system_enrich_event_and_context_helpers(tmp_path: Path) -> None:
    """The built-in event should be registered and the context should collect instructions."""
    context = _system_context(tmp_path)

    context.add_instruction("team_state", "Delegate to research", cache_policy="stable")

    assert EVENT_SYSTEM_ENRICH in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_SYSTEM_ENRICH) == EVENT_SYSTEM_ENRICH
    assert "system" in RESERVED_EVENT_NAMESPACES
    assert default_timeout_ms_for_event(EVENT_SYSTEM_ENRICH) == 2000
    assert context._items == [EnrichmentItem(key="team_state", text="Delegate to research", cache_policy="stable")]


def test_render_system_enrichment_block_sorts_and_escapes() -> None:
    """Stable items should render first and keys should sort deterministically within each group."""
    rendered = render_system_enrichment_block(
        [
            EnrichmentItem(key="volatile-z", text="z < volatile", cache_policy="volatile"),
            EnrichmentItem(key='stable-b"<tag>', text="b & stable", cache_policy="stable"),
            EnrichmentItem(key="volatile-a", text="a volatile", cache_policy="volatile"),
            EnrichmentItem(key="stable-a", text="a stable", cache_policy="stable"),
        ],
    )

    assert rendered == (
        "<mindroom_system_context>\n"
        '<item key="stable-a" cache_policy="stable">\n'
        "a stable\n"
        "</item>\n"
        '<item key="stable-b&quot;&lt;tag&gt;" cache_policy="stable">\n'
        "b &amp; stable\n"
        "</item>\n"
        '<item key="volatile-a" cache_policy="volatile">\n'
        "a volatile\n"
        "</item>\n"
        '<item key="volatile-z" cache_policy="volatile">\n'
        "z &lt; volatile\n"
        "</item>\n"
        "</mindroom_system_context>"
    )


def test_render_system_enrichment_block_empty() -> None:
    """Empty system enrichment should render to an empty string."""
    assert render_system_enrichment_block([]) == ""


def test_strip_system_enrichment_block_removes_only_system_context() -> None:
    """System stripping should not touch message-enrichment blocks."""
    text = (
        "Prompt\n\n"
        "<mindroom_message_context>\n"
        '<item key="message" cache_policy="volatile">\n'
        "message context\n"
        "</item>\n"
        "</mindroom_message_context>\n\n"
        "<mindroom_system_context>\n"
        '<item key="system" cache_policy="stable">\n'
        "system context\n"
        "</item>\n"
        "</mindroom_system_context>\n\n"
        "Actual prompt"
    )

    assert strip_system_enrichment_block(text) == (
        "Prompt\n\n"
        "<mindroom_message_context>\n"
        '<item key="message" cache_policy="volatile">\n'
        "message context\n"
        "</item>\n"
        "</mindroom_message_context>\n"
        "Actual prompt"
    )


def test_strip_enrichment_from_session_storage_removes_message_and_system_blocks() -> None:
    """Persisted session cleanup should scrub both message and system blocks."""
    enriched = (
        "Question\n\n"
        "<mindroom_message_context>\n"
        '<item key="message" cache_policy="volatile">\n'
        "message context\n"
        "</item>\n"
        "</mindroom_message_context>\n\n"
        "<mindroom_system_context>\n"
        '<item key="system" cache_policy="stable">\n'
        "system context\n"
        "</item>\n"
        "</mindroom_system_context>"
    )
    session = AgentSession(
        session_id="session-1",
        agent_id="agent-1",
        runs=[
            RunOutput(
                session_id="session-1",
                messages=[Message(role="user", content=enriched, compressed_content=enriched)],
            ),
        ],
    )
    storage = _FakeStorage(session)

    changed = strip_enrichment_from_session_storage(storage, "session-1", session_type=SessionType.AGENT)

    assert changed is True
    assert storage.upserted_session is session
    assert session.runs[0].messages[0].content == "Question"
    assert session.runs[0].messages[0].compressed_content == "Question"


@pytest.mark.asyncio
async def test_emit_collect_system_enrich_merges_in_order_and_respects_scope(tmp_path: Path) -> None:
    """System enrich collectors should stay concurrent, deterministic, and scope-aware."""
    seen: list[str] = []

    @hook(EVENT_SYSTEM_ENRICH, priority=10, agents=("code",), rooms=("!room:localhost",))
    async def slow_valid(ctx: SystemEnrichContext) -> None:
        await asyncio.sleep(0.02)
        seen.append("slow_valid")
        ctx.add_instruction("first", "slow")

    @hook(EVENT_SYSTEM_ENRICH, priority=20, agents=("other",))
    async def wrong_agent(ctx: SystemEnrichContext) -> None:
        seen.append("wrong_agent")
        ctx.add_instruction("wrong_agent", "nope")

    @hook(EVENT_SYSTEM_ENRICH, priority=30, rooms=("!other:localhost",))
    async def wrong_room(ctx: SystemEnrichContext) -> None:
        seen.append("wrong_room")
        ctx.add_instruction("wrong_room", "nope")

    @hook(EVENT_SYSTEM_ENRICH, priority=40)
    async def fast_valid(ctx: SystemEnrichContext) -> None:
        seen.append("fast_valid")
        ctx.add_instruction("second", "fast")

    registry = HookRegistry.from_plugins(
        [_plugin("system-enrich", [slow_valid, wrong_agent, wrong_room, fast_valid])],
    )
    context = _system_context(tmp_path)

    items = await emit_collect(registry, EVENT_SYSTEM_ENRICH, context)

    assert [item.key for item in items] == ["first", "second"]
    assert seen == ["fast_valid", "slow_valid"]
    assert context._items == []


@pytest.mark.asyncio
async def test_emit_collect_system_enrich_rendered_output_is_deterministic(tmp_path: Path) -> None:
    """Rendered system enrichment should be stable-first and key-sorted across concurrent hooks."""

    @hook(EVENT_SYSTEM_ENRICH, priority=10)
    async def slow_hook(ctx: SystemEnrichContext) -> None:
        await asyncio.sleep(0.02)
        ctx.add_instruction("volatile-b", "late volatile", cache_policy="volatile")
        ctx.add_instruction("stable-c", "late stable", cache_policy="stable")

    @hook(EVENT_SYSTEM_ENRICH, priority=20)
    async def fast_hook(ctx: SystemEnrichContext) -> None:
        ctx.add_instruction("volatile-a", "early volatile", cache_policy="volatile")
        ctx.add_instruction("stable-a", "early stable", cache_policy="stable")

    registry = HookRegistry.from_plugins([_plugin("system-enrich", [slow_hook, fast_hook])])
    items = await emit_collect(registry, EVENT_SYSTEM_ENRICH, _system_context(tmp_path))

    assert render_system_enrichment_block(items) == (
        "<mindroom_system_context>\n"
        '<item key="stable-a" cache_policy="stable">\n'
        "early stable\n"
        "</item>\n"
        '<item key="stable-c" cache_policy="stable">\n'
        "late stable\n"
        "</item>\n"
        '<item key="volatile-a" cache_policy="volatile">\n'
        "early volatile\n"
        "</item>\n"
        '<item key="volatile-b" cache_policy="volatile">\n'
        "late volatile\n"
        "</item>\n"
        "</mindroom_system_context>"
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_applies_system_enrichment_to_agent_additional_context(
    tmp_path: Path,
) -> None:
    """Agent prep should set additional_context before prompt preparation and keep it in the system message."""
    config = _config(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )
    rendered = render_system_enrichment_block(system_items)
    prepared_agent = _agent("code", "CodeAgent")

    async def fake_prepare_agent_execution_context(**kwargs: object) -> _FakePreparedExecution:
        agent = kwargs["agent"]
        assert isinstance(agent, Agent)
        assert agent.additional_context == rendered
        return _FakePreparedExecution(
            final_prompt="prepared prompt",
            unseen_event_ids=[],
            compaction_outcomes=[],
        )

    with (
        patch("mindroom.ai.build_memory_enhanced_prompt", new=AsyncMock(return_value="prompt")),
        patch("mindroom.ai.create_agent", return_value=prepared_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(side_effect=fake_prepare_agent_execution_context),
        ),
    ):
        agent, full_prompt, unseen_event_ids, prepared_history = await _prepare_agent_and_prompt(
            agent_name="code",
            prompt="prompt",
            runtime_paths=runtime_paths_for(config),
            config=config,
            system_enrichment_items=system_items,
        )

    assert agent is prepared_agent
    assert full_prompt == "prepared prompt"
    assert unseen_event_ids == []
    assert prepared_history.compaction_outcomes == []
    assert agent.additional_context == rendered
    assert rendered in _agent_system_message(agent)


@pytest.mark.asyncio
async def test_prepare_materialized_team_execution_applies_system_enrichment_to_team_and_members(
    tmp_path: Path,
) -> None:
    """Team prep should set additional_context on the team coordinator and every member."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    member_agents = [_agent("code", "CodeAgent"), _agent("research", "ResearchAgent")]
    prepared_team = Team(
        id="team-code-research",
        name="Code + Research",
        members=member_agents,
        model=Ollama(id="test-model"),
    )
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["code", "research"],
        agents=member_agents,
        display_names=["CodeAgent", "ResearchAgent"],
        materialized_agent_names={"code", "research"},
        failed_agent_names=[],
    )
    system_items = (
        EnrichmentItem(key="a", text="stable", cache_policy="stable"),
        EnrichmentItem(key="b", text="volatile", cache_policy="volatile"),
    )
    rendered = render_system_enrichment_block(system_items)

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> _FakePreparedExecution:
        team = kwargs["team"]
        agents = kwargs["agents"]
        assert isinstance(team, Team)
        assert team.additional_context == rendered
        assert all(agent.additional_context == rendered for agent in agents)
        return _FakePreparedExecution(
            final_prompt="prepared team prompt",
            unseen_event_ids=[],
            compaction_outcomes=[],
        )

    with (
        patch("mindroom.teams._create_team_instance", return_value=prepared_team),
        patch(
            "mindroom.teams.prepare_bound_team_execution_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
    ):
        team = build_materialized_team_instance(
            requested_agent_names=team_members.requested_agent_names,
            agents=team_members.agents,
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            scope_context=None,
            model_name=None,
            configured_team_name=None,
        )
        await prepare_materialized_team_execution(
            scope_context=None,
            agents=team_members.agents,
            team=team,
            message="Coordinate",
            fallback_prompt="Coordinate",
            thread_history=[],
            config=config,
            runtime_paths=runtime_paths,
            active_model_name=None,
            reply_to_event_id="$event",
            active_event_ids=frozenset(),
            response_sender_id="@mindroom_code:localhost",
            compaction_outcomes_collector=[],
            configured_team_name=None,
            system_enrichment_items=system_items,
        )

    assert team is prepared_team
    assert team.additional_context == rendered
    assert rendered in _team_system_message(team)
    assert all(agent.additional_context == rendered for agent in member_agents)
    assert all(rendered in _agent_system_message(agent) for agent in member_agents)


@pytest.mark.asyncio
async def test_process_and_respond_threads_system_enrichment_items(tmp_path: Path) -> None:
    """Non-streaming agent delivery should forward system enrichment items into the AI layer."""
    bot = _make_bot(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )

    async def fake_ai_response(*_args: object, **kwargs: object) -> str:
        assert kwargs["system_enrichment_items"] == system_items
        return "handled"

    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.deliver_final",
            new=AsyncMock(
                return_value=DeliveryResult(
                    event_id="$response",
                    response_text="handled",
                    delivery_kind="sent",
                ),
            ),
        ),
        patch_response_coordinator_module(
            ensure_request_knowledge_managers=AsyncMock(return_value={}),
            typing_indicator=_noop_typing_indicator,
            ai_response=AsyncMock(side_effect=fake_ai_response),
        ),
    ):
        delivery = await bot._process_and_respond(
            ResponseRequest(
                room_id="!room:localhost",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                prompt="Please reply",
                user_id="@user:localhost",
                system_enrichment_items=system_items,
            ),
        )

    assert delivery.event_id == "$response"
    assert delivery.response_text == "handled"


@pytest.mark.asyncio
async def test_process_and_respond_streaming_threads_system_enrichment_items(tmp_path: Path) -> None:
    """Streaming agent delivery should forward system enrichment items into the AI layer."""
    bot = _make_bot(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )

    async def fake_stream_agent_response(*_args: object, **kwargs: object) -> AsyncIterator[str]:
        assert kwargs["system_enrichment_items"] == system_items
        yield "stream chunk"

    async def fake_send_streaming_response(*args: object, **_kwargs: object) -> tuple[str, str]:
        response_stream = args[7]
        chunks = [str(chunk) async for chunk in response_stream]
        return "$response", "".join(chunks)

    with (
        patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new=AsyncMock(side_effect=fake_send_streaming_response),
        ),
        patch_response_coordinator_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=fake_stream_agent_response,
        ),
    ):
        delivery = await bot._process_and_respond_streaming(
            ResponseRequest(
                room_id="!room:localhost",
                reply_to_event_id="$event",
                thread_id=None,
                thread_history=[],
                prompt="Please reply",
                user_id="@user:localhost",
                system_enrichment_items=system_items,
            ),
        )

    assert delivery.event_id == "$response"
    assert delivery.response_text == "stream chunk"
