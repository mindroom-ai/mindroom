"""Tests for the empty-completed-response guard (retry, fallback notice, history scrub)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.ai import ai_response, stream_agent_response
from mindroom.ai_runtime import (
    EMPTY_RESPONSE_NOTICE,
    discard_empty_completed_run,
    is_empty_completed_run,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.timing import DispatchPipelineTiming
from tests.conftest import bind_runtime_paths, make_turn_context, runtime_paths_for, seed_session
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.scripted_agent_helpers import install_agent_model, stored_runs, streamed_text

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.ai import AIStreamChunk


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path),
    )


def _completed_run(run_id: str, content: str | None) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id="general",
        session_id="session-1",
        content=content,
        messages=[Message(role="assistant", content=content)],
        status=RunStatus.completed,
    )


def _completed_team_run(run_id: str, content: str | None) -> TeamRunRecord:
    return TeamRunRecord(
        run_id=run_id,
        team_id="team_general",
        session_id="session-1",
        content=content,
        status=RunStatus.completed,
    )


async def _respond(config: Config, **kwargs: object) -> str:
    return await ai_response(
        make_turn_context("general", session_id="session-1"),
        prompt="test",
        runtime_paths=runtime_paths_for(config),
        config=config,
        **kwargs,
    )


async def _stream(config: Config, **kwargs: object) -> list[AIStreamChunk]:
    return [
        chunk
        async for chunk in stream_agent_response(
            make_turn_context("general", session_id="session-1"),
            prompt="test",
            runtime_paths=runtime_paths_for(config),
            config=config,
            **kwargs,
        )
    ]


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
    storage = create_state_storage("general", tmp_path)
    session = ConversationSession(
        session_id="session-1",
        kind="agent",
        owner_id="general",
        runs=[_completed_run("run-good", "First response"), _completed_run("run-empty", None)],
        metadata={},
    )
    seed_session(storage, session)
    scope_context = ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id="general"),
        storage=storage,
        session=session,
    )

    discard_empty_completed_run(
        scope_context=scope_context,
        session_id="session-1",
        run_id="run-empty",
        entity_name="general",
        output_tokens=2,
    )

    assert [run.run_id for run in session.runs] == ["run-good"]
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs] == ["run-good"]


def test_discard_empty_completed_run_deletes_a_run_the_live_session_never_saw(tmp_path: Path) -> None:
    """The loop stores the run without the scope session it loaded first, which therefore lacks the run."""
    storage = create_state_storage("general", tmp_path)
    live_session = seed_session(
        storage,
        ConversationSession(
            session_id="session-1",
            kind="agent",
            owner_id="general",
            runs=[_completed_run("run-good", "First response")],
            metadata={},
        ),
    )
    storage.save_run(live_session, _completed_run("run-empty", None), record_usage=False)

    discard_empty_completed_run(
        scope_context=ScopeSessionContext(
            scope=HistoryScope(kind="agent", scope_id="general"),
            storage=storage,
            session=live_session,
        ),
        session_id="session-1",
        run_id="run-empty",
        entity_name="general",
        output_tokens=2,
    )

    assert [run.run_id for run in live_session.runs] == ["run-good"]
    persisted = storage.load_session("session-1", "agent")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs] == ["run-good"]


def test_discard_empty_completed_team_run_removes_run_from_session_and_storage(tmp_path: Path) -> None:
    """The team discard must purge the run and its member runs from memory and persisted history."""
    storage = create_state_storage("team_general", tmp_path)
    session = ConversationSession(
        session_id="session-1",
        kind="team",
        owner_id="team_general",
        runs=[
            _completed_team_run("run-good", "First response"),
            _completed_team_run("run-empty", None),
            RunRecord(run_id="member-of-empty", agent_id="general", parent_run_id="run-empty"),
        ],
        metadata={},
    )
    seed_session(storage, session)
    scope_context = ScopeSessionContext(
        scope=HistoryScope(kind="team", scope_id="team_general"),
        storage=storage,
        session=session,
    )

    discard_empty_completed_run(
        scope_context=scope_context,
        session_id="session-1",
        run_id="run-empty",
        entity_name="team_general",
        output_tokens=2,
    )

    assert [run.run_id for run in session.runs] == ["run-good"]
    persisted = storage.load_session("session-1", "team")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs] == ["run-good"]


@pytest.mark.asyncio
async def test_ai_response_retries_once_after_empty_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One empty completed response triggers exactly one fresh model attempt, and only the answer is kept."""
    config = _config(tmp_path)
    model = ScriptedModel([answer(""), answer("Recovered")])
    install_agent_model(monkeypatch, model)
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")

    result = await _respond(config, pipeline_timing=timing)

    assert result == "Recovered"
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
    install_agent_model(monkeypatch, model)

    result = await _respond(config)

    assert result == EMPTY_RESPONSE_NOTICE
    assert len(model.requests) == 2
    # The retry's empty run is discarded too; the notice is delivery-only.
    assert stored_runs(config, runtime_paths_for(config), "session-1") == []


@pytest.mark.asyncio
async def test_ai_response_fallback_notice_stays_out_of_the_turn_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery-only notice must never be recorded as model text it could replay from history."""
    config = _config(tmp_path)
    install_agent_model(monkeypatch, ScriptedModel([answer(""), answer("")]))
    recorder = TurnRecorder(user_message="test")

    result = await _respond(config, turn_recorder=recorder)

    assert result == EMPTY_RESPONSE_NOTICE
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_stream_agent_response_retries_once_after_empty_completed_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One empty completed stream triggers exactly one fresh streaming attempt."""
    config = _config(tmp_path)
    model = ScriptedModel([answer(""), answer("Recovered", chunk=len("Recovered"))])
    install_agent_model(monkeypatch, model)
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
    install_agent_model(monkeypatch, model)

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
    install_agent_model(monkeypatch, ScriptedModel([answer(""), answer("")]))
    recorder = TurnRecorder(user_message="test")

    chunks = await _stream(config, turn_recorder=recorder)

    assert streamed_text(chunks) == [EMPTY_RESPONSE_NOTICE]
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""
