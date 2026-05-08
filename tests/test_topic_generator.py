"""Tests for AI-generated Matrix room topics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.state import MatrixState
from mindroom.topic_generator import generate_room_topic_ai
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


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
