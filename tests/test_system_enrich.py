"""Tests for the system:enrich hook framework."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
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
)
from mindroom.hooks.types import default_timeout_ms_for_event, validate_event_name
from mindroom.logging_config import get_logger
from mindroom.matrix.users import AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.memory import MemoryPromptParts
from mindroom.message_target import MessageTarget
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.teams.envelope import prepare_materialized_team_execution
from mindroom.teams.members import ResolvedExactTeamMembers, build_team_spec, materialize_exact_team_members
from mindroom.teams.spec import TeamMode
from tests.access_schema_support import with_current_room_member_access
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_model_providers,
    make_turn_context,
    message_origin,
    patch_response_runner_module,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.reply_span_helpers import response_span

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.teams.spec import TeamSpec


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        with_current_room_member_access(
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
            ),
        ),
        runtime_paths,
    )


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
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
        target=MessageTarget.resolve(room_id, "$thread", "$event"),
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
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


def _make_bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="code",
        user_id="@mindroom_code:localhost",
        display_name="CodeAgent",
        password=TEST_PASSWORD,
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.client = MagicMock()
    bot.client.user_id = agent_user.user_id
    bot._knowledge_access_support.for_agent = MagicMock(return_value=None)
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
    with pytest.raises(ValueError, match="reserved namespace"):
        validate_event_name("system:custom")
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
async def test_a_reply_renders_system_enrichment_into_its_system_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply's system enrichment lands in the system message the model receives, after the agent's prompt."""
    config = _config(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )
    rendered = render_system_enrichment_block(system_items)
    model = ScriptedModel([answer("Done.")])
    install_model_providers(monkeypatch, {"default": model})

    await ai_response(
        make_turn_context("code", system_enrichment_items=system_items),
        prompt="prompt",
        runtime_paths=runtime_paths_for(config),
        config=config,
    )

    [request] = model.requests
    system = request.messages[0]
    assert system.role == "system"
    assert isinstance(system.content, str)
    assert system.content.rstrip().endswith(rendered.strip())
    assert system.content.index("Write code") < system.content.index(rendered.strip())


@pytest.mark.asyncio
async def test_a_reply_keeps_transient_enrichment_out_of_its_system_message_and_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient context shares one user message after the prompt that the model sees but the run never stores."""
    config = _config(tmp_path)
    transient_items = (EnrichmentItem(key="location", text="Current location: Amsterdam", persist=False),)
    model = ScriptedModel([answer("Done.")])
    install_model_providers(monkeypatch, {"default": model})
    monkeypatch.setattr(
        "mindroom.ai.build_memory_prompt_parts",
        AsyncMock(return_value=MemoryPromptParts(transient_turn_context="Retrieved memory")),
    )

    await ai_response(
        make_turn_context("code", session_id="session-1", transient_enrichment_items=transient_items),
        prompt="prompt",
        runtime_paths=runtime_paths_for(config),
        config=config,
    )

    [request] = model.requests
    system, *turn = request.messages
    assert "Current location: Amsterdam" not in str(system.content)
    assert "Retrieved memory" not in str(system.content)
    *_, prompt, transient = turn
    assert prompt.role == transient.role == "user"
    assert "prompt" in str(prompt.content)
    assert transient.add_to_agent_memory is False
    assert "Retrieved memory" in str(transient.content)
    assert "Current location: Amsterdam" in str(transient.content)
    storage = create_session_storage("code", config, runtime_paths_for(config), execution_identity=None)
    [run] = storage.list_runs("session-1")
    assert "Current location: Amsterdam" not in " ".join(str(message.content) for message in run.messages)


def _team_members(config: Config, monkeypatch: pytest.MonkeyPatch) -> ResolvedExactTeamMembers:
    """Materialize the code and research members on scripted providers, as a team reply does."""
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    members = materialize_exact_team_members(
        ["code", "research"],
        config=config,
        runtime_paths=runtime_paths_for(config),
        execution_identity=None,
    )
    assert members.failed_agent_names == []
    return members


def _team_spec(config: Config, members: ResolvedExactTeamMembers, *, system_enrichment: str = "") -> TeamSpec:
    return build_team_spec(
        members=members,
        mode=TeamMode.COORDINATE,
        config=config,
        runtime_paths=runtime_paths_for(config),
        team_display_name="Code + Research",
        scope_context=None,
        execution_identity=None,
        model_name="default",
        configured_team_name=None,
        system_enrichment=system_enrichment,
    ).spec


@pytest.mark.asyncio
async def test_a_team_turn_renders_system_enrichment_for_the_leader_and_every_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader and every member get the turn's system enrichment; transient context stays a separate message."""
    config = _config(tmp_path)
    system_items = (
        EnrichmentItem(key="a", text="stable", cache_policy="stable"),
        EnrichmentItem(key="b", text="volatile", cache_policy="volatile"),
    )
    transient_items = (EnrichmentItem(key="location", text="Current location", persist=False),)
    rendered = render_system_enrichment_block(system_items)
    spec = _team_spec(config, _team_members(config, monkeypatch), system_enrichment=rendered)

    assert spec.system_message is not None
    assert rendered.strip() in str(spec.system_message.content)
    member_systems = [member.spec.system_message for member in spec.members]
    assert all(message is not None and rendered.strip() in str(message.content) for message in member_systems)
    prepared = await prepare_materialized_team_execution(
        make_turn_context(
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            correlation_id="$event",
            reply_to_event_id="$event",
            transient_enrichment_items=transient_items,
            system_enrichment_items=system_items,
        ),
        scope_context=None,
        spec=spec,
        message="Coordinate",
        thread_history=[],
        config=config,
        runtime_paths=runtime_paths_for(config),
        runtime_model=config.resolve_runtime_model(entity_name=None),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id=None,
        configured_team_name=None,
    )

    [transient] = prepared.turn.transient
    assert transient.add_to_agent_memory is False
    assert "Current location" in str(transient.content)
    assert "Current location" not in str(spec.system_message.content)


@pytest.mark.asyncio
async def test_a_prepared_team_turn_sends_the_prompt_without_unseen_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team turn without thread history sends its prompt alone, with no unseen context ahead of it."""
    config = _config(tmp_path)
    spec = _team_spec(config, _team_members(config, monkeypatch))

    prepared = await prepare_materialized_team_execution(
        make_turn_context(room_id=None, thread_id=None, requester_id=None, reply_to_event_id="$event"),
        scope_context=None,
        spec=spec,
        message="Coordinate",
        thread_history=[],
        config=config,
        runtime_paths=runtime_paths_for(config),
        runtime_model=config.resolve_runtime_model(entity_name=None),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id=None,
        configured_team_name=None,
    )

    assert prepared.turn.context == ()
    assert [message.content for message in prepared.run_input(MediaInputs())] == ["Coordinate"]


@pytest.mark.asyncio
async def test_process_and_respond_threads_system_enrichment_items(tmp_path: Path) -> None:
    """Non-streaming agent delivery should forward system enrichment items into the AI layer."""
    bot = _make_bot(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )

    async def fake_ai_response(*args: object, **_kwargs: object) -> str:
        assert args[0].system_enrichment_items == system_items
        return "handled"

    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.deliver_final",
            new=AsyncMock(
                return_value=FinalDeliveryOutcome(
                    terminal_status="completed",
                    event_id="$response",
                    is_visible_response=True,
                    final_visible_body="handled",
                    delivery_kind="sent",
                ),
            ),
        ),
        patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            ai_response=AsyncMock(side_effect=fake_ai_response),
        ),
    ):
        generation = await bot._response_runner._process_and_respond(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$event",),
                    logical_source_event_ids=("$event",),
                ),
                thread_history=[],
                prompt="Please reply",
                user_id="@user:localhost",
                response_envelope=request_envelope(
                    room_id="!room:localhost",
                    reply_to_event_id="$event",
                    prompt="Please reply",
                    user_id="@user:localhost",
                ),
                system_enrichment_items=system_items,
            ),
        )

    assert generation.delivery.event_id == "$response"
    assert generation.delivery.response_text == "handled"


@pytest.mark.asyncio
async def test_process_and_respond_streaming_threads_system_enrichment_items(tmp_path: Path) -> None:
    """Streaming agent delivery should forward system enrichment items into the AI layer."""
    bot = _make_bot(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )

    async def fake_stream_agent_response(*args: object, **_kwargs: object) -> AsyncIterator[str]:
        assert args[0].system_enrichment_items == system_items
        yield "stream chunk"

    async def fake_send_streaming_response(*args: object, **_kwargs: object) -> StreamTransportOutcome:
        response_stream = args[4]
        chunks = [str(chunk) async for chunk in response_stream]
        return StreamTransportOutcome(
            last_physical_stream_event_id="$response",
            terminal_status="completed",
            rendered_body="".join(chunks),
            visible_body_state="visible_body",
        )

    request = ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=("$event",),
            logical_source_event_ids=("$event",),
        ),
        thread_history=[],
        prompt="Please reply",
        user_id="@user:localhost",
        response_envelope=request_envelope(
            room_id="!room:localhost",
            reply_to_event_id="$event",
            prompt="Please reply",
            user_id="@user:localhost",
        ),
        system_enrichment_items=system_items,
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new=AsyncMock(side_effect=fake_send_streaming_response),
        ),
        patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            stream_agent_response=fake_stream_agent_response,
        ),
    ):
        async with response_span(runner, request):
            generation = await runner._process_and_respond_streaming(request)

    assert generation.delivery.event_id == "$response"
    assert generation.delivery.response_text == "stream chunk"
