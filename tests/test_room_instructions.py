"""Room-level `agent_instructions` setting: shared text reader, prompt rendering, and agent/team turn injection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom import room_settings
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import EnrichmentItem
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE
from mindroom.prompts import ROOM_INSTRUCTIONS_TEMPLATE
from mindroom.reply_turn.enrichment import room_instructions_item
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.room_settings import ROOM_TEXT_SETTING_MAX_LENGTH, read_room_text_setting
from mindroom.thread_summary.generation import room_title_settings
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
    patch_response_runner_module,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
)
from tests.response_attempt_helpers import install_direct_response_admission
from tests.response_runner_helpers import streamed_answer
from tests.team_turn_helpers import scripted_team_stream

if TYPE_CHECKING:
    from pathlib import Path

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


@pytest.mark.asyncio
async def test_read_room_text_setting_strips_and_caps_at_2000_chars() -> None:
    """Valid text is stripped and capped at ``ROOM_TEXT_SETTING_MAX_LENGTH`` characters."""
    assert ROOM_TEXT_SETTING_MAX_LENGTH == 2000
    client = _client_with_state(
        {
            (ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "  Keep answers short.  "},
            (ROOM_SETTINGS_EVENT_TYPE, "long"): {"value": "  " + "x" * 2500 + "  "},
        },
    )

    assert await read_room_text_setting(client, _ROOM, "agent_instructions") == "Keep answers short."
    assert await read_room_text_setting(client, _ROOM, "long") == "x" * 2000


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
async def test_room_instructions_item_renders_default_and_overridden_prompt() -> None:
    """The enrichment item uses `ROOM_INSTRUCTIONS_TEMPLATE` and honors config prompt overrides."""
    client = _client_with_state(
        {(ROOM_SETTINGS_EVENT_TYPE, "agent_instructions"): {"value": "  This room is about the Q3 launch.  "}},
    )
    default_config = Config()

    item = await room_instructions_item(client, _ROOM, default_config)

    assert item == EnrichmentItem(
        key="room_instructions",
        text=ROOM_INSTRUCTIONS_TEMPLATE.format(instructions="This room is about the Q3 launch."),
        cache_policy="stable",
        persist=False,
        minimal_required=True,
    )

    custom_config = Config(prompts={"ROOM_INSTRUCTIONS_TEMPLATE": "<room>{instructions}</room>"})
    custom_item = await room_instructions_item(client, _ROOM, custom_config)
    assert custom_item is not None
    assert custom_item.text == "<room>This room is about the Q3 launch.</room>"


def test_room_instructions_template_rejects_unsupported_placeholder() -> None:
    """Config validation rejects placeholders absent from `ROOM_INSTRUCTIONS_TEMPLATE`."""
    with pytest.raises(ValidationError, match="Unsupported prompt placeholder"):
        Config.model_validate({"prompts": {"ROOM_INSTRUCTIONS_TEMPLATE": "{instructions} {room_id}"}})


@pytest.mark.asyncio
async def test_room_title_settings_still_work_with_shared_helper(tmp_path: Path) -> None:
    """Title model and capped title rules continue to read through the shared helper."""
    client = _client_with_state(
        {
            (ROOM_SETTINGS_EVENT_TYPE, "thread_summary_model"): {"value": " room_titles "},
            (ROOM_SETTINGS_EVENT_TYPE, "thread_summary_instructions"): {"value": " " + "y" * 2200},
        },
    )
    config = Config(
        models={
            "default": ModelConfig(provider="openai", id="gpt"),
            "room_titles": ModelConfig(provider="openai", id="titles"),
        },
    )

    model_name, instructions = await room_title_settings(client, config, test_runtime_paths(tmp_path), _ROOM)

    assert model_name == "room_titles"
    assert instructions == "y" * 2000


@pytest.mark.asyncio
async def test_agent_reply_turn_injects_room_instructions_without_matrix_message_tool(tmp_path: Path) -> None:
    """An agent turn receives `room_instructions` even when `matrix_message` is not offered."""
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

    [item] = [item for item in mock_ai.call_args.args[0].transient_enrichment_items if item.key == "room_instructions"]
    assert item.cache_policy == "stable"
    assert item.persist is False
    assert item.minimal_required is True
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

    assert not [
        item for item in mock_ai.call_args.args[0].transient_enrichment_items if item.key == "room_instructions"
    ]
    assert any(log.get("event") == "room_setting_read_failed" for log in logs)


@pytest.mark.asyncio
async def test_team_reply_turn_injects_room_instructions(tmp_path: Path) -> None:
    """A team turn takes its own enrichment path in ResponseRunner and also receives `room_instructions`."""
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
    [item] = [item for item in ctx.transient_enrichment_items if item.key == "room_instructions"]
    assert item.cache_policy == "stable"
    assert item.persist is False
    assert item.minimal_required is True
    assert "Team room rule: cite ticket IDs." in item.text
