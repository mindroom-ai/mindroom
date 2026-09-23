"""Tests for AI-generated Matrix room topics."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.matrix.state import MatrixState
from mindroom.topic_generator import ensure_room_has_topic, generate_room_topic_ai
from tests.access_schema_support import membership_config
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("use_snapshot", [False, True])
@pytest.mark.parametrize("final_state", ["human_topic", "empty", "missing", "forbidden"])
async def test_topic_generation_rechecks_remote_state_before_writing(
    tmp_path: Path,
    use_snapshot: bool,
    final_state: str,
) -> None:
    """A human topic added during generation wins; an unreadable final state forbids writes."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = AsyncMock(spec=nio.AsyncClient)
    room_id = "!lobby:example.com"
    current: nio.RoomGetStateEventResponse | nio.RoomGetStateEventError = nio.RoomGetStateEventResponse(
        {},
        "m.room.topic",
        "",
        room_id,
    )

    async def read(_room: str, _event_type: str, _state_key: str = "") -> object:
        return current

    async def generate(*_args: object) -> str:
        nonlocal current
        if final_state in {"human_topic", "empty"}:
            current = nio.RoomGetStateEventResponse(
                {"topic": "Human topic" if final_state == "human_topic" else ""},
                "m.room.topic",
                "",
                room_id,
            )
        else:
            current = nio.RoomGetStateEventError(
                "unavailable",
                "M_NOT_FOUND" if final_state == "missing" else "M_FORBIDDEN",
                room_id,
            )
        return "AI topic"

    client.room_get_state_event.side_effect = read
    client.room_put_state.return_value = nio.RoomPutStateResponse("$topic", room_id)
    with patch("mindroom.topic_generator.generate_room_topic_ai", side_effect=generate):
        result = await ensure_room_has_topic(
            client,
            room_id,
            "lobby",
            "Lobby",
            config,
            runtime_paths_for(config),
            snapshot=RoomStateSnapshot(room_id, {}) if use_snapshot else None,
        )
    assert result is (final_state != "forbidden")
    if final_state in {"human_topic", "forbidden"}:
        client.room_put_state.assert_not_awaited()
    else:
        client.room_put_state.assert_awaited_once_with(
            room_id=room_id,
            event_type="m.room.topic",
            content={"topic": "AI topic"},
        )


@pytest.mark.asyncio
async def test_generate_room_topic_includes_team_only_room_entities(tmp_path) -> None:  # noqa: ANN001
    """Team-configured rooms should describe the team in the topic prompt."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"research": AgentConfig(display_name="Research Agent")},
            teams={
                "ops": TeamConfig(
                    display_name="Ops Team",
                    role="Operations team",
                    agents=["research"],
                    rooms=["ops"],
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    captured_prompt: str | None = None

    async def capture_run(**kwargs: object) -> SimpleNamespace:
        nonlocal captured_prompt
        captured_prompt = str(kwargs["run_input"])
        return SimpleNamespace(content="Ops topic")

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=None),
        patch("mindroom.topic_generator.cached_agent_run", new=AsyncMock(side_effect=capture_run)),
    ):
        topic = await generate_room_topic_ai("ops", "Ops", config, runtime_paths_for(config))

    assert topic == "Ops topic"
    assert captured_prompt is not None
    assert "- Configured agents and teams: Ops Team" in captured_prompt
    assert "No specific agents or teams configured yet" not in captured_prompt


@pytest.mark.asyncio
async def test_generate_room_topic_resolves_configured_entities_for_persisted_room_key(tmp_path) -> None:  # noqa: ANN001
    """Persisted room IDs should not hide room-key configured entities from topic prompts."""
    runtime_paths = test_runtime_paths(tmp_path)
    state = MatrixState()
    state.add_room("ops", room_id="!ops:localhost", alias="#ops:localhost", name="Ops")
    state.save(runtime_paths)
    config = bind_runtime_paths(
        Config(
            agents={"research": AgentConfig(display_name="Research Agent", rooms=["ops"])},
            teams={
                "ops_team": TeamConfig(
                    display_name="Ops Team",
                    role="Operations team",
                    agents=["research"],
                    rooms=["ops"],
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    captured_prompt: str | None = None

    async def capture_run(**kwargs: object) -> SimpleNamespace:
        nonlocal captured_prompt
        captured_prompt = str(kwargs["run_input"])
        return SimpleNamespace(content="Ops topic")

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=None),
        patch("mindroom.topic_generator.cached_agent_run", new=AsyncMock(side_effect=capture_run)),
    ):
        topic = await generate_room_topic_ai("ops", "Ops", config, runtime_paths_for(config))

    assert topic == "Ops topic"
    assert captured_prompt is not None
    assert "- Configured agents and teams: Research Agent, Ops Team" in captured_prompt
