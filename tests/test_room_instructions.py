"""Room-level `agent_instructions` setting: reader, system enrichment, reset/edit, teams, minimal mode, and voice calls."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import nio
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom import room_settings
from mindroom.agent_cli.minimal_agent import minimal_turn
from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.prompt import PromptSpec
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.ai import TurnResult
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.calls import CallsConfig, LiveCallProfile, RealtimeCallProfile
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import EnrichmentItem
from mindroom.llm.provider import ModelRequest
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE
from mindroom.matrix_rtc.call_manager import CallManager
from mindroom.matrix_rtc.call_origin import CallOrigin, CallOriginContext
from mindroom.matrix_rtc.call_tools import build_call_tools
from mindroom.prompts import ROOM_INSTRUCTIONS_TEMPLATE
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.response_sources import ResponseSources
from mindroom.room_settings import (
    ROOM_TEXT_SETTING_MAX_LENGTH,
    read_room_text_setting,
    room_instructions_item,
)
from mindroom.teams.spec import TeamMode
from mindroom.thread_summary.generation import room_title_settings
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.bot_helpers import (
    _hook_envelope,
    _make_matrix_client_mock,
    _noop_typing_indicator,
    _response_request,
    _runtime_bound_config,
    _set_knowledge_for_agent,
    make_mock_agent_user,
    make_test_agent_bot,
    unique_room_send_responses,
)
from tests.conftest import (
    delivered_matrix_side_effect,
    install_model_providers,
    make_conversation_reader_mock,
    make_relation_lookup,
    patch_response_runner_module,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.history_helpers import replay_settings
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import streamed_answer
from tests.team_turn_helpers import authored_team_config, build_team_turn, scripted_team_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.messages import Message
    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import ResponseTurnContext

_ROOM = "!test:localhost"


def _client_with_state(
    state: dict[tuple[str, str], dict[str, object] | Exception | nio.RoomGetStateEventError],
) -> AsyncMock:
    """Return a mock Matrix client serving ``state`` keyed by ``(event_type, state_key)``."""
    client = _make_matrix_client_mock()

    async def get_state(room_id: str, event_type: str, state_key: str = "") -> object:
        entry = state.get((event_type, state_key))
        if entry is None:
            return nio.RoomGetStateEventError("not found", "M_NOT_FOUND", room_id=room_id)
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, nio.RoomGetStateEventError):
            return entry
        return nio.RoomGetStateEventResponse(dict(entry), event_type, state_key, room_id)

    client.room_get_state_event = AsyncMock(side_effect=get_state)
    return client


def _all_request_text(messages: tuple[Message, ...] | list[Message]) -> str:
    return "\n".join(msg.get_content_string() for msg in messages)


def _system_request_text(messages: tuple[Message, ...] | list[Message]) -> str:
    return "\n".join(msg.get_content_string() for msg in messages if msg.role == "system")


@pytest.mark.asyncio
async def test_read_room_text_setting_strips_and_only_caps_when_max_length_is_given() -> None:
    """Valid text is stripped; only callers passing ``max_length`` cap the length."""
    assert ROOM_TEXT_SETTING_MAX_LENGTH == 2000
    long_value = "x" * 2500
    client = _client_with_state(
        {
            (ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "  Keep answers short.  "},
            (ROOM_SETTINGS_EVENT_TYPE, "long"): {"value": f"  {long_value}  "},
        },
    )

    assert await read_room_text_setting(client, _ROOM, "agent_instructions") == "Keep answers short."
    assert await read_room_text_setting(client, _ROOM, "long") == long_value
    assert await read_room_text_setting(client, _ROOM, "long", max_length=ROOM_TEXT_SETTING_MAX_LENGTH) == "x" * 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"value": "   \n\t  "},
        {"value": 42},
        {"value": ["not", "a", "string"]},
        {"value": None},
    ],
    ids=["not-found", "cleared", "blank", "int", "list", "none"],
)
async def test_read_room_text_setting_returns_none_quietly_when_unset(
    entry: dict[str, object] | None,
) -> None:
    """Missing, cleared, blank, or non-string room settings return None without logging a warning."""
    state: dict[tuple[str, str], dict[str, object] | Exception | nio.RoomGetStateEventError] = {}
    if entry is not None:
        state[(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions")] = entry
    client = _client_with_state(state)

    with capture_logs() as logs:
        result = await read_room_text_setting(client, _ROOM, "agent_instructions")

    assert result is None
    assert not [log for log in logs if log.get("log_level") == "warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        nio.RoomGetStateEventError("forbidden", status_code="M_FORBIDDEN"),
        RuntimeError("homeserver down"),
        TimeoutError("state timeout"),
    ],
    ids=["error-response", "exception", "timeout"],
)
async def test_read_room_text_setting_logs_warning_and_returns_none_on_error(
    failure: Exception | nio.RoomGetStateEventError,
) -> None:
    """Failed reads log a warning and return None instead of raising."""
    client = _client_with_state({(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): failure})

    with capture_logs() as logs:
        result = await read_room_text_setting(client, _ROOM, "agent_instructions")

    assert result is None
    warnings = [log for log in logs if log.get("event") == "room_setting_read_failed"]
    assert len(warnings) == 1
    assert warnings[0]["room_id"] == _ROOM
    assert warnings[0]["setting"] == "agent_instructions"


@pytest.mark.asyncio
async def test_read_room_text_setting_times_out_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled homeserver hits the request timeout, logs a warning, and returns None."""
    monkeypatch.setattr(room_settings, "_REQUEST_TIMEOUT_SECONDS", 0.01)
    client = _make_matrix_client_mock()

    async def stall(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(1.0)
        return None

    client.room_get_state_event = AsyncMock(side_effect=stall)

    with capture_logs() as logs:
        assert await read_room_text_setting(client, _ROOM, "agent_instructions") is None

    assert any(log.get("event") == "room_setting_read_failed" for log in logs)


@pytest.mark.asyncio
async def test_read_room_text_setting_catches_transport_body_read_error() -> None:
    """An exception raised by ``transport.text()`` inside ``state_event_content`` logs and returns None."""
    transport = MagicMock(spec=aiohttp.ClientResponse)
    transport.status = 200
    transport.text = AsyncMock(side_effect=aiohttp.ClientError("body stream failed"))
    response = nio.RoomGetStateEventResponse({}, ROOM_SETTINGS_EVENT_TYPE, "agent_instructions", _ROOM)
    response.transport_response = transport
    client = _make_matrix_client_mock()
    client.room_get_state_event = AsyncMock(return_value=response)

    with capture_logs() as logs:
        assert await read_room_text_setting(client, _ROOM, "agent_instructions") is None

    assert any(log.get("event") == "room_setting_read_failed" for log in logs)


@pytest.mark.asyncio
async def test_room_instructions_item_renders_default_overridden_and_capped_prompt() -> None:
    """The enrichment item uses `ROOM_INSTRUCTIONS_TEMPLATE`, caps at 2000 chars, and honors overrides."""
    client = _client_with_state(
        {(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "  " + "a" * 2100 + "  "}},
    )
    default_config = Config()

    item = await room_instructions_item(client, _ROOM, default_config)

    assert item == EnrichmentItem(
        key="room_instructions",
        text=ROOM_INSTRUCTIONS_TEMPLATE.format(instructions="a" * 2000),
        cache_policy="stable",
        persist=False,
        minimal_required=True,
    )

    custom_config = Config(prompts={"ROOM_INSTRUCTIONS_TEMPLATE": "<room>{instructions}</room>"})
    custom_item = await room_instructions_item(client, _ROOM, custom_config)
    assert custom_item is not None
    assert custom_item.text == f"<room>{'a' * 2000}</room>"


def test_room_instructions_template_rejects_unsupported_placeholder() -> None:
    """Config validation rejects placeholders absent from `ROOM_INSTRUCTIONS_TEMPLATE`."""
    with pytest.raises(ValidationError, match="Unsupported prompt placeholder"):
        Config.model_validate({"prompts": {"ROOM_INSTRUCTIONS_TEMPLATE": "{instructions} {room_id}"}})


def test_minimal_mode_includes_room_instructions_in_system_messages() -> None:
    """Minimal mode includes ``minimal_required=True`` system enrichment directly in system messages."""
    build = _fake_voice_build(turn_context="Date: today")
    item = EnrichmentItem(
        key="room_instructions",
        text=ROOM_INSTRUCTIONS_TEMPLATE.format(instructions="Keep replies concise."),
        cache_policy="stable",
        persist=False,
        minimal_required=True,
    )

    minimal = minimal_turn(
        build,
        session_preamble="",
        transient_memory="",
        system_enrichment=(item,),
        transient_enrichment=(),
    )

    assert "Keep replies concise." in _system_request_text(minimal.system_messages)


@pytest.mark.asyncio
async def test_room_title_settings_caps_only_instructions_not_model_name(tmp_path: Path) -> None:
    """Title rules are capped at 2000 chars while ``thread_summary_model`` is not truncated."""
    long_model_name = "m" * 2100
    client = _client_with_state(
        {
            (ROOM_SETTINGS_EVENT_TYPE, "thread_summary_model"): {"value": f" {long_model_name} "},
            (ROOM_SETTINGS_EVENT_TYPE, "thread_summary_instructions"): {"value": " " + "y" * 2200},
        },
    )
    config = Config(
        models={
            "default": ModelConfig(provider="openai", id="gpt"),
            long_model_name: ModelConfig(provider="openai", id="titles"),
        },
    )

    model_name, instructions = await room_title_settings(client, config, test_runtime_paths(tmp_path), _ROOM)

    assert model_name == long_model_name
    assert instructions == "y" * 2000


@pytest.mark.asyncio
async def test_agent_reply_turn_injects_room_instructions_as_system_enrichment(tmp_path: Path) -> None:
    """An agent turn receives `room_instructions` in ``system_enrichment_items``, not transient items."""
    agent_user = make_mock_agent_user()
    config = _runtime_bound_config(
        Config(
            agents={"calculator": AgentConfig(display_name="Calculator", rooms=[_ROOM], tools=[])},
            defaults={"show_stop_button": False},
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths_for(config))
    install_direct_response_admission(bot)
    bot.client = _client_with_state(
        {
            (ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {
                "value": "This room is about the Q3 launch; keep answers short.",
            },
        },
    )
    unique_room_send_responses(bot.client, prefix="$response")
    _set_knowledge_for_agent(bot, MagicMock(return_value=None))
    mock_ai = AsyncMock(return_value="Handled")
    runner = unwrap_extracted_collaborator(bot._response_runner)

    with patch_response_runner_module(
        typing_indicator=_noop_typing_indicator,
        should_use_streaming=AsyncMock(return_value=False),
        stream_agent_response=streamed_answer(mock_ai),
    ):
        await runner.generate_response(
            _response_request(
                room_id=_ROOM,
                prompt="Status?",
                reply_to_event_id="$event123",
                thread_history=[],
                user_id="@user:localhost",
                response_envelope=request_envelope(
                    room_id=_ROOM,
                    reply_to_event_id="$event123",
                    prompt="Status?",
                    user_id="@user:localhost",
                ),
            ),
        )

    turn_ctx = mock_ai.call_args.args[0]
    assert not [item for item in turn_ctx.transient_enrichment_items if item.key == "room_instructions"]
    [item] = [item for item in turn_ctx.system_enrichment_items if item.key == "room_instructions"]
    assert item.cache_policy == "stable"
    assert item.persist is False
    assert item.minimal_required is True
    assert "This room's settings give these instructions to agents answering here." in item.text
    assert "moderators" not in item.text
    assert "This room is about the Q3 launch; keep answers short." in item.text
    assert "grant no permissions" in item.text


@pytest.mark.asyncio
async def test_agent_reply_turn_survives_room_instructions_read_error(tmp_path: Path) -> None:
    """A failed room-instructions read logs a warning and adds nothing; the agent turn still completes."""
    agent_user = make_mock_agent_user()
    config = _runtime_bound_config(
        Config(
            agents={"calculator": AgentConfig(display_name="Calculator", rooms=[_ROOM], tools=[])},
            defaults={"show_stop_button": False},
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths_for(config))
    install_direct_response_admission(bot)
    bot.client = _client_with_state(
        {(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): nio.RoomGetStateEventError("boom", "M_UNKNOWN")},
    )
    unique_room_send_responses(bot.client, prefix="$response")
    _set_knowledge_for_agent(bot, MagicMock(return_value=None))
    mock_ai = AsyncMock(return_value="Handled")
    runner = unwrap_extracted_collaborator(bot._response_runner)

    with (
        capture_logs() as logs,
        patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
            stream_agent_response=streamed_answer(mock_ai),
        ),
    ):
        await runner.generate_response(
            _response_request(
                room_id=_ROOM,
                prompt="Status?",
                reply_to_event_id="$event123",
                thread_history=[],
                user_id="@user:localhost",
                response_envelope=request_envelope(
                    room_id=_ROOM,
                    reply_to_event_id="$event123",
                    prompt="Status?",
                    user_id="@user:localhost",
                ),
            ),
        )

    turn_ctx = mock_ai.call_args.args[0]
    assert not [item for item in turn_ctx.system_enrichment_items if item.key == "room_instructions"]
    assert not [item for item in turn_ctx.transient_enrichment_items if item.key == "room_instructions"]
    assert any(log.get("event") == "room_setting_read_failed" for log in logs)


@pytest.mark.asyncio
async def test_room_instructions_clear_and_edit_are_authoritative_across_same_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set -> reply -> clear -> reply -> edit -> reply in one thread/session never replays stale instructions."""
    agent_user = make_mock_agent_user()
    config = _runtime_bound_config(
        Config(
            agents={"calculator": AgentConfig(display_name="Calculator", rooms=[_ROOM], tools=[])},
            defaults={"show_stop_button": False},
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths_for(config))
    install_direct_response_admission(bot)
    state: dict[tuple[str, str], dict[str, object] | Exception | nio.RoomGetStateEventError] = {
        (ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "FIRST_ROOM_RULE_XYZ"},
    }
    bot.client = _client_with_state(state)
    unique_room_send_responses(bot.client, prefix="$response")
    _set_knowledge_for_agent(bot, MagicMock(return_value=None))
    model = ScriptedModel([answer("First reply"), answer("Second reply"), answer("Third reply")])
    install_model_providers(monkeypatch, {"default": model})
    runner = unwrap_extracted_collaborator(bot._response_runner)

    async def send_turn(event_id: str, prompt: str) -> None:
        with patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
        ):
            await runner.generate_response(
                _response_request(
                    room_id=_ROOM,
                    thread_id="$thread1",
                    prompt=prompt,
                    reply_to_event_id=event_id,
                    thread_history=[],
                    user_id="@user:localhost",
                    response_envelope=request_envelope(
                        room_id=_ROOM,
                        thread_id="$thread1",
                        reply_to_event_id=event_id,
                        prompt=prompt,
                        user_id="@user:localhost",
                    ),
                ),
            )

    await send_turn("$ev1", "First question")

    state[(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions")] = {}
    await send_turn("$ev2", "Second question")

    state[(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions")] = {"value": "SECOND_ROOM_RULE_ABC"}
    await send_turn("$ev3", "Third question")

    assert len(model.requests) == 3
    # Turn 1: instructions appear only in system prompt, never in user/history messages.
    assert "FIRST_ROOM_RULE_XYZ" in _system_request_text(model.requests[0].messages)
    assert "FIRST_ROOM_RULE_XYZ" not in _all_request_text(
        [m for m in model.requests[0].messages if m.role != "system"],
    )
    # Turn 2 after clearing: old instructions appear nowhere in the request (system OR replayed history).
    assert "FIRST_ROOM_RULE_XYZ" not in _all_request_text(model.requests[1].messages)
    # Turn 3 after editing: only the new instructions appear in system prompt, old rule nowhere.
    assert "SECOND_ROOM_RULE_ABC" in _system_request_text(model.requests[2].messages)
    assert "FIRST_ROOM_RULE_XYZ" not in _all_request_text(model.requests[2].messages)


@pytest.mark.asyncio
async def test_team_reply_turn_injects_room_instructions_into_system_enrichment(tmp_path: Path) -> None:
    """A team turn receives `room_instructions` in ``system_enrichment_items`` via the shared preparer."""
    agent_user = make_mock_agent_user()
    config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(display_name="General", rooms=[_ROOM], tools=[]),
                "calculator": AgentConfig(display_name="Calculator", rooms=[_ROOM], tools=[]),
            },
            defaults={"show_stop_button": False},
        ),
        tmp_path,
    )
    runtime_paths = runtime_paths_for(config)
    bot = make_test_agent_bot(agent_user, config=config, runtime_paths=runtime_paths)
    bot.client = _client_with_state(
        {(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "Team room rule: cite ticket IDs."}},
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    team_stream = scripted_team_stream("Team reply")

    with (
        patch(
            "mindroom.delivery_gateway.send_message_outcome",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$team")),
        ),
        patch_response_runner_module(
            typing_indicator=_noop_typing_indicator,
            should_use_streaming=AsyncMock(return_value=False),
            team_response_stream=team_stream,
        ),
    ):
        await runner.generate_team_response_helper(
            ResponseRequest(
                sources=ResponseSources(
                    pending_event_ids=("$team-root",),
                    logical_source_event_ids=("$team-root",),
                ),
                thread_history=[],
                user_id="@user:localhost",
                prompt="team prompt",
                response_envelope=_hook_envelope(body="team prompt", source_event_id="$team-root"),
                correlation_id="corr-team",
            ),
            member_names=["calculator", "general"],
            team_mode="collaborate",
        )

    _spec, ctx = team_stream.call_args.args
    assert not [item for item in ctx.transient_enrichment_items if item.key == "room_instructions"]
    [item] = [item for item in ctx.system_enrichment_items if item.key == "room_instructions"]
    assert item.cache_policy == "stable"
    assert item.persist is False
    assert item.minimal_required is True
    assert "Team room rule: cite ticket IDs." in item.text


@pytest.mark.asyncio
async def test_team_leader_and_members_all_receive_room_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team system enrichment reaches both the leader and every delegated member without extra reads."""
    authored = authored_team_config(
        {
            "writer": {"display_name": "Writer", "role": "Writes.", "model": "writer-model", "tools": []},
            "checker": {"display_name": "Checker", "role": "Checks.", "model": "checker-model", "tools": []},
        },
        teams={
            "squad": {
                "display_name": "Squad",
                "role": "Answers.",
                "agents": ["writer", "checker"],
                "mode": "collaborate",
            },
        },
        models=("default", "writer-model", "checker-model"),
    )
    team = build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=("writer", "checker"),
        team_name="squad",
        mode=TeamMode.COLLABORATE,
        scripts={
            "default": [
                calls(tool_call("delegate_task_to_members", "call-all", task="Answer concisely"), metrics=usage(40, 4)),
                answer("Consensus.", usage(50, 5)),
            ],
            "writer-model": [answer("Writer reply.")],
            "checker-model": [answer("Checker reply.")],
        },
    )
    client = _client_with_state(
        {(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "TEAM_MEMBER_RULE_999"}},
    )
    item = await room_instructions_item(client, _ROOM, team.config)
    assert item is not None

    await team.stream(
        mode=TeamMode.COLLABORATE,
        ctx=team.ctx(system_enrichment_items=(item,)),
        show_tool_calls=False,
    )

    assert client.room_get_state_event.await_count == 1
    assert "TEAM_MEMBER_RULE_999" in _system_request_text(team.models["default"].requests[0].messages)
    assert "TEAM_MEMBER_RULE_999" in _system_request_text(team.models["writer-model"].requests[0].messages)
    assert "TEAM_MEMBER_RULE_999" in _system_request_text(team.models["checker-model"].requests[0].messages)


@asynccontextmanager
async def _authorized_call_operation() -> AsyncIterator[bool]:
    yield True


def _fake_voice_build(role: str = "Voice helper.", *, turn_context: str | None = None) -> AgentBuild:
    return AgentBuild(
        spec=AgentSpec(id="general", name="General", model=ScriptedModel([]), system_messages=(), toolkits=()),
        prompt=PromptSpec(role=role, instructions=(), turn_context=turn_context),
        request_template=ModelRequest(messages=()),
        history_settings=replay_settings(),
        surface=AgentSurface(
            mode="standard",
            cli_shell=False,
            deferred=(),
            context_documents=(),
            context_files=(),
            knowledge_sources=(),
            output_file_policy=None,
            identity="General",
        ),
        tool_policy=(),
        tool_jobs=None,
    )


@pytest.mark.asyncio
async def test_voice_calls_inject_room_instructions_across_cascaded_live_and_realtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cascaded/live responders, live session prompts, and realtime prompts all receive room instructions."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        Config(agents={"general": AgentConfig(display_name="General", rooms=[_ROOM], tools=[])}),
        tmp_path,
    )
    state: dict[tuple[str, str], dict[str, object] | Exception | nio.RoomGetStateEventError] = {
        (ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "VOICE_ROOM_RULE_777"},
    }
    client = _client_with_state(state)
    orchestrator = SimpleNamespace(knowledge_refresh_scheduler=None, budgets=None)

    class ToolSupport:
        def build_context(self, target: MessageTarget, **_kwargs: object) -> ToolRuntimeContext:
            return ToolRuntimeContext(
                agent_name="general",
                target=target,
                requester_id="@caller:localhost",
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                plugins=config_plugins(config, runtime_paths),
                conversation_reader=make_conversation_reader_mock(),
                relations=make_relation_lookup(),
                orchestrator=orchestrator,  # type: ignore[arg-type]
            )

    ai_calls: list[ResponseTurnContext] = []

    async def fake_ai_response(turn: ResponseTurnContext, *_args: object, **_kwargs: object) -> TurnResult:
        ai_calls.append(turn)
        return TurnResult("Spoken reply")

    async def fake_resolve_knowledge(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(knowledge=None, unavailable={})

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        fake_resolve_knowledge,
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=_fake_voice_build()))
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)

    # 1 & 2. Cascaded / Live responder + Live session prompt
    live_tooling = await build_call_tools(
        agent_name="general",
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id=_ROOM,
        requester_id="@caller:localhost",
        authorize_operation=_authorized_call_operation,
        enable_responder=True,
        voice_instructions="Speak briefly.",
    )
    assert live_tooling.get_system_prompt is not None
    assert live_tooling.responder is not None
    assert live_tooling.finalize_spoken_response is not None

    live_prompt = await live_tooling.get_system_prompt()
    assert "VOICE_ROOM_RULE_777" in live_prompt

    first = await live_tooling.responder("Hello on call", None)
    live_tooling.finalize_spoken_response(first.turn_id, first.text, False)
    assert [item.key for item in ai_calls[0].system_enrichment_items] == ["voice_call", "room_instructions"]
    assert "VOICE_ROOM_RULE_777" in ai_calls[0].system_enrichment_items[1].text

    # Clearing mid-call drops room_instructions on the next spoken turn.
    state[(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions")] = {}
    second = await live_tooling.responder("Second turn", None)
    live_tooling.finalize_spoken_response(second.turn_id, second.text, False)
    assert [item.key for item in ai_calls[1].system_enrichment_items] == ["voice_call"]
    assert live_tooling.close is not None
    await live_tooling.close()

    # 3. Realtime voice session prompt
    state[(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions")] = {"value": "REALTIME_ROOM_RULE_888"}
    realtime_tooling = await build_call_tools(
        agent_name="general",
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id=_ROOM,
        requester_id="@caller:localhost",
        authorize_operation=_authorized_call_operation,
        enable_responder=False,
    )
    assert "REALTIME_ROOM_RULE_888" in realtime_tooling.instructions
    assert realtime_tooling.close is not None
    await realtime_tooling.close()


def _make_call_manager(
    *,
    client: AsyncMock,
    config: Config,
    runtime_paths: object,
    profile: LiveCallProfile | RealtimeCallProfile,
    tool_support: object,
) -> CallManager:
    call_config = config.model_copy(
        update={"calls": CallsConfig(enabled=True, profiles={"voice": profile}, agents={"general": "voice"})},
    )
    manager = CallManager(
        agent_name="general",
        client=client,
        config=call_config,
        runtime_paths=runtime_paths,  # type: ignore[arg-type]
        ssl_verify=False,
        tool_support=tool_support,  # type: ignore[arg-type]
        get_invited_rooms_by_agent=dict,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        response_admission_gate=ResponseAdmissionGate(),
        admission_fence=nullcontext,
    )
    manager._is_authorized_call_member = lambda _user_id, _room_id: True  # type: ignore[method-assign]
    return manager


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_voice_call_reads_instructions_from_validated_origin_room_while_keeping_call_room_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calls started from a chat room read `agent_instructions` from the origin room, keeping tools anchored to the call room."""
    call_room, origin_room = "!call:localhost", "!origin:localhost"
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    rooms=[call_room, origin_room],
                    tools=[],
                    access=ResponderAccessConfig(users=["@caller:localhost"]),
                ),
            },
        ),
        tmp_path,
    )
    by_room = {
        (origin_room, ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "ORIGIN_ROOM_RULE_111"},
        (call_room, ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "CALL_ROOM_RULE_222"},
    }
    client = _make_matrix_client_mock()
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[nio.RoomMember(user_id="@caller:localhost", display_name="Caller", avatar_url=None)],
            room_id=origin_room,
        ),
    )
    memberships = AgentReplyMembershipIndex()

    async def get_state(room_id: str, event_type: str, state_key: str = "") -> object:
        entry = by_room.get((room_id, event_type, state_key))
        if entry is None:
            return nio.RoomGetStateEventError("not found", "M_NOT_FOUND", room_id=room_id)
        return nio.RoomGetStateEventResponse(dict(entry), event_type, state_key, room_id)

    client.room_get_state_event = AsyncMock(side_effect=get_state)
    orchestrator = SimpleNamespace(knowledge_refresh_scheduler=None, budgets=None)
    anchored_rooms: list[str] = []

    class ToolSupport:
        def build_context(self, target: MessageTarget, **_kwargs: object) -> ToolRuntimeContext:
            anchored_rooms.append(target.room_id)
            return ToolRuntimeContext(
                agent_name="general",
                target=target,
                requester_id="@caller:localhost",
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                plugins=config_plugins(config, runtime_paths),
                conversation_reader=make_conversation_reader_mock(),
                relations=make_relation_lookup(),
                agent_reply_memberships=memberships,
                orchestrator=orchestrator,  # type: ignore[arg-type]
            )

    ai_calls: list[ResponseTurnContext] = []

    async def fake_ai_response(turn: ResponseTurnContext, *_args: object, **_kwargs: object) -> TurnResult:
        ai_calls.append(turn)
        return TurnResult("Spoken reply")

    async def fake_resolve_knowledge(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(knowledge=None, unavailable={})

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async", fake_resolve_knowledge)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=_fake_voice_build()))
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)

    origin_context = CallOriginContext(
        origin=CallOrigin(room_id=origin_room, thread_id="$thread1"),
        caller_id="@caller:localhost",
        room_name="Origin",
        thread_title=None,
        messages=(),
    )
    tool_support = ToolSupport()

    # 1. Live / cascaded via CallManager._build_tooling with validated origin_context -> reads origin_room
    live_manager = _make_call_manager(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        profile=LiveCallProfile(backend="live", model="gpt-realtime", credentials_service="openai", voice="alloy"),
        tool_support=tool_support,
    )
    live_tooling = await live_manager._build_tooling(
        call_room,
        requester_id="@caller:localhost",
        session_id=f"{call_room}:call:1",
        origin_context=origin_context,
    )
    assert live_tooling.get_system_prompt is not None
    assert live_tooling.responder is not None
    live_prompt = await live_tooling.get_system_prompt()
    assert "ORIGIN_ROOM_RULE_111" in live_prompt
    assert "CALL_ROOM_RULE_222" not in live_prompt

    await live_tooling.responder("Hello", None)
    assert ai_calls[0].room_id == call_room
    [item] = [item for item in ai_calls[0].system_enrichment_items if item.key == "room_instructions"]
    assert "ORIGIN_ROOM_RULE_111" in item.text
    assert "CALL_ROOM_RULE_222" not in item.text
    assert live_tooling.close is not None
    await live_tooling.close()

    # 2. Realtime via CallManager._build_tooling with validated origin_context -> reads origin_room
    realtime_manager = _make_call_manager(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        profile=RealtimeCallProfile(
            backend="realtime",
            model="gpt-realtime",
            credentials_service="openai",
            voice="alloy",
        ),
        tool_support=tool_support,
    )
    realtime_tooling = await realtime_manager._build_tooling(
        call_room,
        requester_id="@caller:localhost",
        session_id=f"{call_room}:call:2",
        origin_context=origin_context,
    )
    assert "ORIGIN_ROOM_RULE_111" in realtime_tooling.instructions
    assert "CALL_ROOM_RULE_222" not in realtime_tooling.instructions
    assert realtime_tooling.close is not None
    await realtime_tooling.close()

    # 3. Without origin_context -> falls back to the call room
    standalone_tooling = await realtime_manager._build_tooling(
        call_room,
        requester_id="@caller:localhost",
        session_id=f"{call_room}:call:3",
        origin_context=None,
    )
    assert "CALL_ROOM_RULE_222" in standalone_tooling.instructions
    assert "ORIGIN_ROOM_RULE_111" not in standalone_tooling.instructions
    assert standalone_tooling.close is not None
    await standalone_tooling.close()

    # Tool context remained anchored to the call room on every build.
    assert anchored_rooms == [call_room, call_room, call_room]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_voice_call_drops_origin_room_instructions_when_access_is_revoked_or_unverifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-room origin reads re-check ``room_access_allowed`` per spoken turn and omit instructions on revoke/error."""
    call_room, origin_room = "!call:localhost", "!origin:localhost"
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    rooms=[call_room, origin_room],
                    tools=[],
                    access=ResponderAccessConfig(users=["@caller:localhost"]),
                ),
            },
        ),
        tmp_path,
    )
    client = _make_matrix_client_mock()
    caller_in_origin = True

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        members = (
            [nio.RoomMember(user_id="@caller:localhost", display_name="Caller", avatar_url=None)]
            if room_id == origin_room and caller_in_origin
            else []
        )
        return nio.JoinedMembersResponse(members=members, room_id=room_id)

    client.joined_members = AsyncMock(side_effect=joined_members)
    origin_reads: list[str] = []

    async def get_state(room_id: str, event_type: str, state_key: str = "") -> object:
        if room_id == origin_room and event_type == ROOM_SETTINGS_EVENT_TYPE and state_key == "agent_instructions":
            origin_reads.append(room_id)
            return nio.RoomGetStateEventResponse({"value": "ORIGIN_SECRET_RULE"}, event_type, state_key, room_id)
        return nio.RoomGetStateEventError("not found", "M_NOT_FOUND", room_id=room_id)

    client.room_get_state_event = AsyncMock(side_effect=get_state)
    orchestrator = SimpleNamespace(knowledge_refresh_scheduler=None, budgets=None)
    memberships = AgentReplyMembershipIndex()

    class ToolSupport:
        def build_context(self, target: MessageTarget, **_kwargs: object) -> ToolRuntimeContext:
            return ToolRuntimeContext(
                agent_name="general",
                target=target,
                requester_id="@caller:localhost",
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                plugins=config_plugins(config, runtime_paths),
                conversation_reader=make_conversation_reader_mock(),
                relations=make_relation_lookup(),
                agent_reply_memberships=memberships,
                orchestrator=orchestrator,  # type: ignore[arg-type]
            )

    ai_calls: list[ResponseTurnContext] = []

    async def fake_ai_response(turn: ResponseTurnContext, *_args: object, **_kwargs: object) -> TurnResult:
        ai_calls.append(turn)
        return TurnResult("Spoken reply")

    async def fake_resolve_knowledge(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(knowledge=None, unavailable={})

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async", fake_resolve_knowledge)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=_fake_voice_build()))
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)

    tooling = await build_call_tools(
        agent_name="general",
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id=call_room,
        instructions_room_id=origin_room,
        requester_id="@caller:localhost",
        authorize_operation=_authorized_call_operation,
        enable_responder=True,
        voice_instructions="Speak briefly.",
    )
    assert tooling.responder is not None
    assert tooling.finalize_spoken_response is not None

    # Turn 1: caller still has access to origin room -> receives origin instructions.
    first = await tooling.responder("First spoken turn", None)
    tooling.finalize_spoken_response(first.turn_id, first.text, False)
    assert [item.key for item in ai_calls[0].system_enrichment_items] == ["voice_call", "room_instructions"]
    assert "ORIGIN_SECRET_RULE" in ai_calls[0].system_enrichment_items[1].text
    assert len(origin_reads) == 1

    # Turn 2: caller access to origin room is revoked -> item omitted, origin room not read, call continues.
    caller_in_origin = False
    second = await tooling.responder("Second spoken turn", None)
    tooling.finalize_spoken_response(second.turn_id, second.text, False)
    assert [item.key for item in ai_calls[1].system_enrichment_items] == ["voice_call"]
    assert len(origin_reads) == 1

    # Turn 3: access check raises -> warning logged, item omitted, call continues.
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.room_access_allowed",
        AsyncMock(side_effect=RuntimeError("membership check failed")),
    )
    with capture_logs() as logs:
        third = await tooling.responder("Third spoken turn", None)
        tooling.finalize_spoken_response(third.turn_id, third.text, False)
    assert [item.key for item in ai_calls[2].system_enrichment_items] == ["voice_call"]
    assert any(log.get("event") == "call_room_instructions_access_check_failed" for log in logs)
    assert len(origin_reads) == 1
    assert tooling.close is not None
    await tooling.close()
