"""Tests for the empty-completed-response guard: one retry, the fallback notice, and no empty run in history."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.ai import TurnResult, ai_response, stream_agent_response
from mindroom.ai_runtime import EMPTY_RESPONSE_NOTICE
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.response_turn import TurnSinks
from mindroom.timing import DispatchPipelineTiming
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_turn_context,
    runtime_paths_for,
)
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.scripted_agent_helpers import stored_runs, streamed_text

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.ai import AIStreamChunk
    from mindroom.response_turn import ResponseTurnContext


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
    )


def _ctx(**turn_inputs: object) -> ResponseTurnContext:
    return replace(make_turn_context("general", session_id="session-1"), prompt="test", **turn_inputs)


async def _respond(config: Config, *, sinks: TurnSinks = TurnSinks(), **turn_inputs: object) -> TurnResult:  # noqa: B008
    return await ai_response(_ctx(**turn_inputs), runtime_paths_for(config), config, sinks=sinks)


async def _stream(config: Config, *, sinks: TurnSinks = TurnSinks(), **turn_inputs: object) -> list[AIStreamChunk]:  # noqa: B008
    return [
        chunk
        async for chunk in stream_agent_response(_ctx(**turn_inputs), runtime_paths_for(config), config, sinks=sinks)
    ]


@pytest.mark.asyncio
async def test_ai_response_retries_once_after_empty_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One empty completed response triggers exactly one fresh model attempt, and only the answer is kept."""
    config = _config(tmp_path)
    model = ScriptedModel([answer(""), answer("Recovered")])
    install_model_providers(monkeypatch, {"default": model})
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")

    result = await _respond(config, pipeline_timing=timing)

    assert result.text == "Recovered"
    assert len(model.requests) == 2
    assert timing.marks["first_model_request_sent"] < timing.marks["model_request_sent"]
    assert len(stored_runs(config, runtime_paths_for(config), "session-1")) == 1


@pytest.mark.asyncio
async def test_ai_response_returns_fallback_notice_when_retry_is_also_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive empty responses surface a visible notice, and neither empty run is kept."""
    config = _config(tmp_path)
    model = ScriptedModel([answer(""), answer("  ")])
    install_model_providers(monkeypatch, {"default": model})

    result = await _respond(config)

    # The retry's whitespace streamed before the notice, as a streamed reply shows it.
    assert result.text == f"  {EMPTY_RESPONSE_NOTICE}"
    assert len(model.requests) == 2
    # The retry's empty run is not stored either; the notice is delivery-only.
    assert stored_runs(config, runtime_paths_for(config), "session-1") == []


@pytest.mark.asyncio
async def test_a_blocking_reply_that_ends_empty_publishes_the_error_status_a_streamed_one_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking reply whose retry is empty too publishes its run as failed, as the streamed reply does."""
    config = _config(tmp_path)
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer(""), answer(""), answer(""), answer("")])})
    streamed: dict[str, Any] = {}

    result = await _respond(config)
    chunks = await _stream(config, sinks=TurnSinks(run_metadata_collector=streamed))

    assert result.text == EMPTY_RESPONSE_NOTICE
    assert streamed_text(chunks) == [EMPTY_RESPONSE_NOTICE]
    assert result.run_metadata[AI_RUN_METADATA_KEY]["status"] == "error"
    assert streamed[AI_RUN_METADATA_KEY]["status"] == "error"


@pytest.mark.asyncio
async def test_ai_response_fallback_notice_stays_out_of_the_turn_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery-only notice must never be recorded as model text it could replay from history."""
    config = _config(tmp_path)
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer(""), answer("")])})
    recorder = TurnRecorder()

    result = await _respond(config, sinks=TurnSinks(turn_recorder=recorder))

    assert result.text == EMPTY_RESPONSE_NOTICE
    assert recorder.outcome == "completed"


@pytest.mark.asyncio
async def test_the_turn_recorder_holds_the_run_id_of_the_attempt_that_answered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an empty attempt's retry, the recorder names the retry's stored run, which an interruption would record."""
    config = _config(tmp_path)
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer(""), answer("Recovered")])})
    recorder = TurnRecorder()

    await _stream(config, sinks=TurnSinks(turn_recorder=recorder))

    [run] = stored_runs(config, runtime_paths_for(config), "session-1")
    assert recorder.run_id == run.run_id


@pytest.mark.asyncio
async def test_stream_agent_response_retries_once_after_empty_completed_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One empty completed stream triggers exactly one fresh streaming attempt."""
    config = _config(tmp_path)
    model = ScriptedModel([answer(""), answer("Recovered", chunk=len("Recovered"))])
    install_model_providers(monkeypatch, {"default": model})
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")

    chunks = await _stream(config, pipeline_timing=timing)

    assert streamed_text(chunks) == ["Recovered"]
    assert len(model.requests) == 2
    assert timing.marks["first_model_request_sent"] < timing.marks["model_request_sent"]
    assert len(stored_runs(config, runtime_paths_for(config), "session-1")) == 1


@pytest.mark.asyncio
async def test_stream_agent_response_yields_fallback_notice_when_retry_is_also_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive empty streams end with a visible notice, never a blank edit, and keep neither run."""
    config = _config(tmp_path)
    model = ScriptedModel([answer(""), answer("")])
    install_model_providers(monkeypatch, {"default": model})

    chunks = await _stream(config)

    assert streamed_text(chunks) == [EMPTY_RESPONSE_NOTICE]
    assert len(model.requests) == 2
    assert stored_runs(config, runtime_paths_for(config), "session-1") == []


@pytest.mark.asyncio
async def test_stream_agent_response_fallback_notice_stays_out_of_the_turn_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streamed delivery-only notice must never be recorded as model text either."""
    config = _config(tmp_path)
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer(""), answer("")])})
    recorder = TurnRecorder()

    chunks = await _stream(config, sinks=TurnSinks(turn_recorder=recorder))

    assert streamed_text(chunks) == [EMPTY_RESPONSE_NOTICE]
    assert recorder.outcome == "completed"
