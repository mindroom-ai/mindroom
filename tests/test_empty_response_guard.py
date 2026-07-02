"""Tests for the empty-completed-response guard (retry, fallback notice, history scrub)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.ai import _PreparedAgentRun, ai_response, stream_agent_response
from mindroom.ai_runtime import (
    EMPTY_RESPONSE_NOTICE,
    discard_empty_completed_run,
    is_empty_completed_run,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.history import PreparedHistoryState
from mindroom.history.runtime import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)


def _config() -> Config:
    return Config(
        agents={"general": AgentConfig(display_name="General")},
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )


def _mock_agent(run_output: RunOutput) -> MagicMock:
    agent = MagicMock()
    agent.model = MagicMock()
    agent.model.__class__.__name__ = "OpenAIChat"
    agent.model.id = "test-model"
    agent.name = "GeneralAgent"
    agent.add_history_to_context = False
    agent.arun = AsyncMock(return_value=run_output)
    return agent


def _mock_streaming_agent(stream: AsyncIterator[object]) -> MagicMock:
    agent = MagicMock()
    agent.model = MagicMock()
    agent.model.__class__.__name__ = "OpenAIChat"
    agent.model.id = "test-model"
    agent.name = "GeneralAgent"
    agent.add_history_to_context = False
    agent.arun = MagicMock(return_value=stream)
    return agent


def _prepared_prompt_result(agent: object) -> _PreparedAgentRun:
    return _PreparedAgentRun(
        agent=agent,
        messages=(Message(role="user", content="test prompt"),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(prepared_context_tokens=None),
        runtime_model_name="default",
    )


def _completed_run(run_id: str, content: str | None) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="general",
        session_id="session-1",
        content=content,
        messages=[Message(role="assistant", content=content)],
        status=RunStatus.completed,
    )


def test_is_empty_completed_run_detects_contentless_completed_runs() -> None:
    """Completed runs with no tool calls and no visible content are empty."""
    assert is_empty_completed_run(_completed_run("r1", None))
    assert is_empty_completed_run(_completed_run("r1", ""))
    assert is_empty_completed_run(_completed_run("r1", "  \n"))


def test_is_empty_completed_run_ignores_runs_with_content_tools_or_other_status() -> None:
    """Real responses, tool-only runs, and non-completed statuses are not empty."""
    assert not is_empty_completed_run(_completed_run("r1", "hello"))

    tool_run = _completed_run("r1", None)
    tool_run.tools = [ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "pwd"}, result="/app")]
    assert not is_empty_completed_run(tool_run)

    errored = _completed_run("r1", None)
    errored.status = RunStatus.error
    assert not is_empty_completed_run(errored)


def test_discard_empty_completed_run_removes_run_from_loaded_session_and_storage(tmp_path: Path) -> None:
    """The empty run must disappear from both the in-memory session and persisted history."""
    storage = create_state_storage(
        "general",
        tmp_path,
        subdir="sessions",
        session_table="general_sessions",
    )
    try:
        session = AgentSession(
            session_id="session-1",
            agent_id="general",
            runs=[
                _completed_run("run-good", "First response"),
                _completed_run("run-empty", None),
            ],
            metadata={},
            created_at=1,
            updated_at=1,
        )
        storage.upsert_session(session)
        scope_context = ScopeSessionContext(
            scope=HistoryScope(kind="agent", scope_id="general"),
            storage=storage,
            session=session,
        )

        discard_empty_completed_run(
            scope_context=scope_context,
            session_id="session-1",
            run_id="run-empty",
            session_type=SessionType.AGENT,
            entity_name="general",
            output_tokens=2,
        )

        assert session.runs is not None
        assert [run.run_id for run in session.runs] == ["run-good"]
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.runs is not None
        assert [run.run_id for run in persisted.runs] == ["run-good"]
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_ai_response_retries_once_after_empty_completed_run(tmp_path: Path) -> None:
    """One empty completed response should trigger exactly one fresh model attempt."""
    empty_agent = _mock_agent(_completed_run("run-empty", None))
    recovered_agent = _mock_agent(_completed_run("run-good", "Recovered"))

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.side_effect = [
            _prepared_prompt_result(empty_agent),
            _prepared_prompt_result(recovered_agent),
        ]

        result = await ai_response(
            agent_name="general",
            prompt="test",
            session_id="session-1",
            runtime_paths=_runtime_paths(tmp_path),
            config=_config(),
        )

    assert result == "Recovered"
    empty_agent.arun.assert_called_once()
    recovered_agent.arun.assert_called_once()


@pytest.mark.asyncio
async def test_ai_response_returns_fallback_notice_when_retry_is_also_empty(tmp_path: Path) -> None:
    """Two consecutive empty responses should surface a visible notice, never a blank reply."""
    first_agent = _mock_agent(_completed_run("run-empty-1", None))
    second_agent = _mock_agent(_completed_run("run-empty-2", ""))

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.side_effect = [
            _prepared_prompt_result(first_agent),
            _prepared_prompt_result(second_agent),
        ]

        result = await ai_response(
            agent_name="general",
            prompt="test",
            session_id="session-1",
            runtime_paths=_runtime_paths(tmp_path),
            config=_config(),
        )

    assert result == EMPTY_RESPONSE_NOTICE
    first_agent.arun.assert_called_once()
    second_agent.arun.assert_called_once()


@pytest.mark.asyncio
async def test_ai_response_fallback_notice_stays_out_of_the_turn_recorder(tmp_path: Path) -> None:
    """The delivery-only notice must never be recorded as model text it could replay from history."""
    recorder = TurnRecorder(user_message="test")
    first_agent = _mock_agent(_completed_run("run-empty-1", None))
    second_agent = _mock_agent(_completed_run("run-empty-2", None))

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.side_effect = [
            _prepared_prompt_result(first_agent),
            _prepared_prompt_result(second_agent),
        ]

        result = await ai_response(
            agent_name="general",
            prompt="test",
            session_id="session-1",
            runtime_paths=_runtime_paths(tmp_path),
            config=_config(),
            turn_recorder=recorder,
        )

    assert result == EMPTY_RESPONSE_NOTICE
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_stream_agent_response_retries_once_after_empty_completed_stream(tmp_path: Path) -> None:
    """One empty completed stream should trigger exactly one fresh streaming attempt."""

    async def empty_stream() -> AsyncIterator[object]:
        yield RunCompletedEvent(content=None)

    async def recovered_stream() -> AsyncIterator[object]:
        yield RunContentEvent(content="Recovered")
        yield RunCompletedEvent(content="Recovered")

    empty_agent = _mock_streaming_agent(empty_stream())
    recovered_agent = _mock_streaming_agent(recovered_stream())

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.side_effect = [
            _prepared_prompt_result(empty_agent),
            _prepared_prompt_result(recovered_agent),
        ]

        chunks = [
            chunk
            async for chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session-1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )
        ]

    contents = [cast("str", chunk.content) for chunk in chunks if isinstance(chunk, RunContentEvent)]
    assert contents == ["Recovered"]
    empty_agent.arun.assert_called_once()
    recovered_agent.arun.assert_called_once()


@pytest.mark.asyncio
async def test_stream_agent_response_yields_fallback_notice_when_retry_is_also_empty(tmp_path: Path) -> None:
    """Two consecutive empty streams should end with a visible notice, never a blank edit."""

    async def empty_stream() -> AsyncIterator[object]:
        yield RunCompletedEvent(content=None)

    first_agent = _mock_streaming_agent(empty_stream())
    second_agent = _mock_streaming_agent(empty_stream())

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.side_effect = [
            _prepared_prompt_result(first_agent),
            _prepared_prompt_result(second_agent),
        ]

        chunks = [
            chunk
            async for chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session-1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )
        ]

    contents = [cast("str", chunk.content) for chunk in chunks if isinstance(chunk, RunContentEvent)]
    assert contents == [EMPTY_RESPONSE_NOTICE]
    first_agent.arun.assert_called_once()
    second_agent.arun.assert_called_once()


@pytest.mark.asyncio
async def test_stream_agent_response_fallback_notice_stays_out_of_the_turn_recorder(tmp_path: Path) -> None:
    """The streamed delivery-only notice must never be recorded as model text either."""

    async def empty_stream() -> AsyncIterator[object]:
        yield RunCompletedEvent(content=None)

    recorder = TurnRecorder(user_message="test")
    first_agent = _mock_streaming_agent(empty_stream())
    second_agent = _mock_streaming_agent(empty_stream())

    with patch("mindroom.ai._prepare_agent_and_prompt", new_callable=AsyncMock) as mock_prepare:
        mock_prepare.side_effect = [
            _prepared_prompt_result(first_agent),
            _prepared_prompt_result(second_agent),
        ]

        chunks = [
            chunk
            async for chunk in stream_agent_response(
                agent_name="general",
                prompt="test",
                session_id="session-1",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
                turn_recorder=recorder,
            )
        ]

    contents = [cast("str", chunk.content) for chunk in chunks if isinstance(chunk, RunContentEvent)]
    assert contents == [EMPTY_RESPONSE_NOTICE]
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""
