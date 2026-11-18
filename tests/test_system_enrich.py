"""Tests for the system:enrich hook framework."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.final_delivery import Completed, FinalDeliveryOutcome, StreamTransportOutcome
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
from mindroom.llm.messages import system_text
from mindroom.logging_config import get_logger
from mindroom.matrix.users import AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.memory import MemoryPromptParts
from mindroom.message_target import MessageTarget
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import Owned, ResponseSources
from mindroom.response_turn import CompletedApprovalRun
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.teams.envelope import prepare_materialized_team_execution
from mindroom.teams.members import BuiltTeam, ResolvedExactTeamMembers, build_team_spec, resolve_exact_team_members
from mindroom.teams.spec import TeamMode
from mindroom.tool_jobs.runtime import BackgroundOutcome
from mindroom.turn_model import ModelScope, resolve_turn_model
from mindroom.turn_origin import TurnIntent
from tests.access_schema_support import with_current_room_member_access
from tests.approval_continuation_helpers import approval_continuation, resumed_approval
from tests.bot_helpers import _hook_plugin, make_test_agent_bot, unique_room_send_responses
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
    unstored_team_scope_context,
    unwrap_extracted_collaborator,
)
from tests.delegation_helpers import turn_tool_context
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.reply_span_helpers import paused_for_approval
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target, stream_transport
from tests.test_reply_records_turns import (
    _acknowledge_selection,
    _answer,
    _model_stream,
    _regeneration,
    _reply,
    _streaming_bot,
)
from tests.test_response_payload_preparation import _preparation, _request
from tests.test_response_runner_focused import _admit_approval_source
from tests.test_tool_job_follow_ups import _ended, _follow_up, _runner_key, _runner_owner, _runner_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.response_turn import ResponseTurnContext


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
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.client = MagicMock()
    bot.client.user_id = agent_user.user_id
    return bot


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
        [_hook_plugin("system-enrich", [slow_valid, wrong_agent, wrong_room, fast_valid])],
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

    registry = HookRegistry.from_plugins([_hook_plugin("system-enrich", [slow_hook, fast_hook])])
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
        replace(make_turn_context("code", system_enrichment_items=system_items), prompt="prompt"),
        runtime_paths_for(config),
        config,
    )

    [request] = model.requests
    stable, turn = request.messages[:2]
    assert (stable.role, turn.role) == ("system", "system")
    assert isinstance(turn.content, str)
    assert turn.content.rstrip().endswith(rendered.strip())
    assert "Write code" in str(stable.content)
    assert rendered.strip() not in str(stable.content)


@pytest.mark.asyncio
async def test_a_reply_keeps_transient_enrichment_out_of_its_system_message_and_stores_it_after_the_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient context shares one user message after the prompt, which the run stores there as turn context."""
    config = _config(tmp_path)
    transient_items = (EnrichmentItem(key="location", text="Current location: Amsterdam", persist=False),)
    model = ScriptedModel([answer("Done.")])
    install_model_providers(monkeypatch, {"default": model})
    monkeypatch.setattr(
        "mindroom.ai.build_memory_prompt_parts",
        AsyncMock(return_value=MemoryPromptParts(transient_turn_context="Retrieved memory")),
    )

    await ai_response(
        replace(
            make_turn_context("code", session_id="session-1", transient_enrichment_items=transient_items),
            prompt="prompt",
        ),
        runtime_paths_for(config),
        config,
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
    stored_prompt, stored_transient, _answer = run.messages
    assert "Current location: Amsterdam" not in str(stored_prompt.content)
    assert stored_transient.content == transient.content
    assert stored_transient.add_to_agent_memory is False


def _team_members(config: Config, monkeypatch: pytest.MonkeyPatch) -> ResolvedExactTeamMembers:
    """Materialize the code and research members on scripted providers, as a team reply does."""
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    return resolve_exact_team_members(
        ["code", "research"],
        config=config,
        runtime_paths=runtime_paths_for(config),
        execution_identity=None,
        refresh_scheduler=None,
        active_model_names=dict.fromkeys(["code", "research"], "default"),
    )


def _built_team(config: Config, members: ResolvedExactTeamMembers, *, system_enrichment: str = "") -> BuiltTeam:
    return build_team_spec(
        members=members,
        mode=TeamMode.COORDINATE,
        config=config,
        runtime_paths=runtime_paths_for(config),
        scope_context=unstored_team_scope_context(config, members.requested_agent_names, team_name=None),
        execution_identity=None,
        model_name="default",
        configured_team_name=None,
        timezone="UTC",
        system_enrichment=system_enrichment,
    )


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
    built = _built_team(config, _team_members(config, monkeypatch), system_enrichment=rendered)
    spec = built.spec

    assert rendered.strip() in system_text(spec.leader.system_messages)
    assert all(rendered.strip() in system_text(member.build().spec.system_messages) for member in spec.members)
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
        scope_context=unstored_team_scope_context(
            config,
            [member.id for member in built.spec.members],
            team_name=None,
        ),
        built=built,
        message="Coordinate",
        thread_history=[],
        config=config,
        runtime_paths=runtime_paths_for(config),
        runtime_model=resolve_turn_model(config, runtime_paths_for(config), ModelScope(entity=None), None),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id=None,
    )

    [transient] = prepared.turn.transient
    assert transient.add_to_agent_memory is False
    assert "Current location" in str(transient.content)
    assert "Current location" not in system_text(spec.leader.system_messages)


@pytest.mark.asyncio
async def test_a_prepared_team_turn_sends_the_prompt_without_unseen_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team turn without thread history sends its prompt alone, with no unseen context ahead of it."""
    config = _config(tmp_path)
    built = _built_team(config, _team_members(config, monkeypatch))

    prepared = await prepare_materialized_team_execution(
        make_turn_context(room_id=None, thread_id=None, requester_id=None, reply_to_event_id="$event"),
        scope_context=unstored_team_scope_context(
            config,
            [member.id for member in built.spec.members],
            team_name=None,
        ),
        built=built,
        message="Coordinate",
        thread_history=[],
        config=config,
        runtime_paths=runtime_paths_for(config),
        runtime_model=resolve_turn_model(config, runtime_paths_for(config), ModelScope(entity=None), None),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id=None,
    )

    assert prepared.turn.context == ()
    assert [message.content for message in prepared.run_input(MediaInputs())] == ["Coordinate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_agent_delivery_threads_system_enrichment_items(tmp_path: Path, streaming: bool) -> None:
    """Agent delivery, streamed or delivered whole, forwards system enrichment items into the AI layer."""
    bot = _make_bot(tmp_path)
    system_items = (
        EnrichmentItem(key="alpha", text="stable", cache_policy="stable"),
        EnrichmentItem(key="omega", text="volatile", cache_policy="volatile"),
    )
    seen_items: list[object] = []

    async def fake_stream_agent_response(
        ctx: ResponseTurnContext,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        seen_items.append(ctx.system_enrichment_items)
        yield "handled"

    async def fake_send_streaming_response(*args: object, **_kwargs: object) -> StreamTransportOutcome:
        response_stream = args[4]
        chunks = [str(chunk) async for chunk in response_stream]
        return stream_transport("$response", "".join(chunks))

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
    effects = AsyncMock()
    delivered = FinalDeliveryOutcome(
        ending=Completed(),
        event_id="$response",
        is_visible_response=True,
        final_visible_body="handled",
        delivery_kind="sent",
    )
    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=delivered)),
        patch(
            "mindroom.delivery_gateway.send_streaming_response",
            new=AsyncMock(side_effect=fake_send_streaming_response),
        ),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock(return_value=delivered)),
        patch_response_runner_module(
            typing_indicator=_noop_typing,
            stream_agent_response=fake_stream_agent_response,
            should_use_streaming=AsyncMock(return_value=streaming),
            apply_post_response_effects=effects,
        ),
    ):
        assert await runner.generate_response(request) == Owned()

    # The finalized answer reached the reply's post-response effects.
    assert effects.await_args.args[0].final_visible_event_id == "$response"

    assert seen_items == [system_items]


_MARKER = EnrichmentItem(key="marker", text="Marker from a system:enrich hook.", cache_policy="stable")


def _install_marker_hook(bot: AgentBot) -> list[SystemEnrichContext]:
    """Give ``bot`` a plugin whose system:enrich hook adds ``_MARKER``; return the contexts the hook runs with."""
    seen: list[SystemEnrichContext] = []

    @hook(EVENT_SYSTEM_ENRICH)
    async def add_marker(ctx: SystemEnrichContext) -> None:
        seen.append(ctx)
        ctx.add_instruction(_MARKER.key, _MARKER.text, cache_policy=_MARKER.cache_policy)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("marker", [add_marker])])
    return seen


def _message_turn(*, source_kind: str = MESSAGE_SOURCE_KIND) -> ResponseRequest:
    """Return a message turn as dispatch hands it over, its payload left for the runner to build under the lock."""
    target = _target()
    preparation = _preparation(target)
    envelope = replace(
        preparation.dispatch.envelope,
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind=source_kind),
    )
    preparation = replace(preparation, dispatch=replace(preparation.dispatch, envelope=envelope, correlation_id="corr"))
    return replace(
        _request(target, preparation, thread_history=[]),
        response_envelope=envelope,
        correlation_id="corr",
    )


@pytest.mark.asyncio
async def test_a_message_turn_runs_system_enrich_once(tmp_path: Path) -> None:
    """A message turn, whose payload the runner builds under the lock, gets the hook's instructions from one run."""
    bot = await _streaming_bot(tmp_path)
    seen = _install_marker_hook(bot)
    model = AsyncMock(return_value="An answer.")

    assert await _answer(bot, _message_turn(), model) == Owned()

    assert model.await_args.args[0].system_enrichment_items == (_MARKER,)
    [ctx] = seen
    assert (ctx.target_entity_name, ctx.target_member_names) == ("general", None)
    assert (ctx.envelope.source_event_id, ctx.correlation_id) == ("$event", "corr")


@pytest.mark.asyncio
async def test_hook_instructions_come_ahead_of_the_runtimes_own(tmp_path: Path) -> None:
    """A silent schedule's quiet-delivery guidance, which the runtime adds, follows the hook's instructions."""
    bot = await _streaming_bot(tmp_path)
    _install_marker_hook(bot)
    model = AsyncMock(return_value="A finding.")

    assert await _answer(bot, _message_turn(source_kind=SILENT_SCHEDULE_SOURCE_KIND), model) == Owned()

    keys = [item.key for item in model.await_args.args[0].system_enrichment_items]
    assert keys == [_MARKER.key, "silent_schedule_delivery"]


@pytest.mark.asyncio
async def test_an_edit_regeneration_runs_system_enrich(tmp_path: Path) -> None:
    """The answer an edit regenerates gets the hook's instructions, with the edit as the hook's message."""
    bot = await _streaming_bot(tmp_path)
    seen = _install_marker_hook(bot)
    await _answer(bot, _plain_request(_target()), AsyncMock(return_value="First answer."))
    regenerated = AsyncMock(return_value="Second answer.")

    assert await _answer(bot, _regeneration(answer_event_id=(await _reply(bot)).event_id), regenerated) == Owned()

    assert regenerated.await_args.args[0].system_enrichment_items == (_MARKER,)
    assert seen[-1].envelope.source_event_id == "$edit"
    assert (seen[-1].target_entity_name, seen[-1].target_member_names) == ("general", None)


@pytest.mark.asyncio
async def test_an_interactive_selection_answer_runs_system_enrich(tmp_path: Path) -> None:
    """The answer to a selection, which fills its acknowledgement, gets the hook's instructions."""
    bot = await _streaming_bot(tmp_path)
    seen = _install_marker_hook(bot)
    ack_event_id, span_id = await _acknowledge_selection(bot)
    request = replace(_plain_request(_target()), existing_event_id=ack_event_id, interactive_span_id=span_id)
    model = AsyncMock(return_value="Selected answer.")

    assert await _answer(bot, request, model) == Owned()

    assert model.await_args.args[0].system_enrichment_items == (_MARKER,)
    [ctx] = seen
    assert ctx.envelope.source_event_id == "$event"


@pytest.mark.asyncio
async def test_a_job_follow_up_runs_system_enrich(tmp_path: Path) -> None:
    """The turn that reports ended background jobs gets the hook's instructions, with the follow-up as its message."""
    bot = _bot(tmp_path)
    await bot._take_over_replies()
    unique_room_send_responses(bot.client)
    seen = _install_marker_hook(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    turns: list[ResponseTurnContext] = []

    async def stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        turns.append(ctx)
        yield "The job finished."

    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=_runner_owner(key))
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        with patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner._run_job_follow_up(event)
    finally:
        await runtime.shutdown()

    [turn] = turns
    assert turn.system_enrichment_items == (_MARKER,)
    [ctx] = seen
    assert (ctx.envelope.source_event_id, ctx.correlation_id) == ("job-follow-up:1", "job-follow-up:1")
    assert ctx.envelope.origin.intent is TurnIntent.JOB_FOLLOW_UP


@pytest.mark.asyncio
async def test_a_team_turn_tells_system_enrich_its_members(tmp_path: Path) -> None:
    """A team turn handed over with its prompt built names its members to the hook and gets the hook's instructions."""
    bot = await _streaming_bot(tmp_path)
    seen = _install_marker_hook(bot)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    model = AsyncMock(return_value="Team answer.")

    with patch_response_runner_module(
        team_response_stream=_model_stream(model),
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
    ):
        assert (
            await runner.generate_team_response_helper(
                _plain_request(_target()),
                member_names=[bot.agent_name],
                team_mode="coordinate",
            )
            == Owned()
        )

    assert model.await_args.args[1].system_enrichment_items == (_MARKER,)
    [ctx] = seen
    assert (ctx.target_entity_name, ctx.target_member_names) == ("general", ("general",))


@pytest.mark.asyncio
@pytest.mark.parametrize(("entity_kind", "member_names"), [("agent", None), ("team", ("alpha", "beta"))])
@pytest.mark.parametrize(
    ("source_kind", "keys"),
    [
        pytest.param(MESSAGE_SOURCE_KIND, [_MARKER.key], id="message"),
        pytest.param(SILENT_SCHEDULE_SOURCE_KIND, [_MARKER.key, "silent_schedule_delivery"], id="silent-schedule"),
    ],
)
async def test_an_approval_resume_runs_system_enrich_for_the_run_it_resumes(
    tmp_path: Path,
    entity_kind: str,
    member_names: tuple[str, ...] | None,
    source_kind: str,
    keys: list[str],
) -> None:
    """A reply resumed after a tool approval runs the hook once, on its paused message, and resumes with its instructions.

    A team's resume names the team's members to the hook and runs on the model its run paused on. A silent schedule's
    resume also carries its quiet-delivery guidance after the hook's instructions, as its turn did.
    """
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.models["large"] = runner.deps.runtime.config.models["default"]
    await _admit_approval_source(runner.deps.replies.store)
    seen = _install_marker_hook(bot)
    tool_context = turn_tool_context(
        runner.deps.runtime.config,
        runner.deps.runtime_paths,
        approval_continuation().execution_identity,
    )
    continuation = approval_continuation(
        entity_kind=entity_kind,
        origin=message_origin(sender_id="@user:localhost", source_kind=source_kind),
        correlation_id="corr-resume",
        execution_identity=tool_context.execution_identity,
        runtime_model_name="large",
        team_member_names=member_names or (),
        team_mode="coordinate" if member_names else None,
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) is not None
    target = _target(thread_id="$thread")
    agent_resume = AsyncMock(return_value=CompletedApprovalRun(response_text="Done.", metadata_content={}))
    team_resume = AsyncMock(return_value=CompletedApprovalRun(response_text="Done.", metadata_content={}))

    with (
        patch.object(runner.deps.tool_runtime, "build_turn_context", return_value=tool_context),
        patch("mindroom.approval.execution.AgentApprovalExecution.continue_run", new=agent_resume),
        patch("mindroom.approval.continuation_runner.continue_paused_team_run", new=team_resume),
    ):
        async with resumed_approval(bot, continuation) as claimed:
            await runner.approvals._continue_entity_call(
                claimed,
                request=runner.approvals.approval_response_request(claimed, target=target),
                target=target,
                tool_trace_collector=[],
                progress=None,
                cli_approval_handler=None,
            )

    [ctx] = seen
    assert (ctx.target_entity_name, ctx.target_member_names) == ("general", member_names)
    assert (ctx.envelope.source_event_id, ctx.correlation_id) == ("$source", "corr-resume")
    resumed, skipped = (team_resume, agent_resume) if member_names else (agent_resume, team_resume)
    skipped.assert_not_awaited()
    items = resumed.await_args.kwargs["system_enrichment_items"]
    assert items[0] == _MARKER
    assert [item.key for item in items] == keys
    if member_names:
        assert team_resume.await_args.kwargs["model_name"] == "large"
