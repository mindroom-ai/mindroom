"""Tests for session compaction and history scrubbing."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.agents import _get_agent_session, create_agent, create_session_storage, get_seen_event_ids
from mindroom.compaction import (
    _PENDING_COMPACTION,
    _WRAPPER_OVERHEAD_TOKENS,
    _estimate_serialized_run_tokens,
    apply_pending_compaction,
    compact_session_now,
    estimate_runs_tokens,
    queue_pending_compaction,
    scrub_history_messages_from_sessions,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, RuntimePaths, resolve_runtime_paths
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from agno.run.agent import RunOutput


class FakeModel(Model):
    """Minimal Agno model for deterministic history-storage tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return a fixed sync response."""
        return ModelResponse(content="hello")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return a fixed async response."""
        return ModelResponse(content="hello")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        """Yield a fixed sync streaming response."""
        yield ModelResponse(content="hello")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield a fixed async streaming response."""
        yield ModelResponse(content="hello")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


def _runtime_paths(tmp_path: object) -> RuntimePaths:
    base_path = Path(str(tmp_path))
    return resolve_runtime_paths(
        config_path=base_path / "config.yaml",
        storage_path=base_path,
    )


def _runtime_bound_config(config: Config, tmp_path: object | None = None) -> Config:
    return bind_runtime_paths(config, _runtime_paths(tmp_path or tempfile.mkdtemp()))


def _make_config(tmp_path: object) -> Config:
    return _runtime_bound_config(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        tmp_path,
    )


def _coerce_agent_session(raw_session: object) -> AgentSession:
    if isinstance(raw_session, AgentSession):
        return raw_session
    session = AgentSession.from_dict(raw_session)
    if session is not None:
        return session
    msg = f"Unsupported session payload: {type(raw_session).__name__}"
    raise TypeError(msg)


def _make_storage(tmp_path: Path) -> SqliteDb:
    return SqliteDb(
        session_table="test_agent_sessions",
        db_file=str(tmp_path / "test-agent.db"),
    )


def _make_plain_agent(storage: SqliteDb, *, store_history_messages: bool) -> Agent:
    return Agent(
        id="test_agent",
        model=FakeModel(id="fake-model", provider="fake"),
        db=storage,
        add_history_to_context=True,
        store_history_messages=store_history_messages,
    )


def _single_run_compaction_window(run: RunOutput, *, reserve_tokens: int = 1024) -> int:
    desired_budget = _estimate_serialized_run_tokens(run) + _WRAPPER_OVERHEAD_TOKENS + 32
    return int((desired_budget + reserve_tokens + 2000) / 0.9) + 1


async def _seed_session(
    storage: SqliteDb,
    *,
    session_id: str,
    turn_count: int,
) -> Agent:
    agent = _make_plain_agent(storage, store_history_messages=False)
    for turn_number in range(1, turn_count + 1):
        prompt = f"turn-{turn_number} " * 20
        await agent.arun(
            prompt,
            session_id=session_id,
            metadata={"matrix_seen_event_ids": [f"$e{turn_number}"]},
        )
    return agent


@pytest.mark.asyncio
async def test_create_agent_does_not_store_history_messages(tmp_path: Path) -> None:
    """Newly created agents should avoid persisting replayed history messages."""
    config = _make_config(tmp_path)
    runtime_paths = runtime_paths_for(config)

    with patch("mindroom.ai.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        agent = create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
        )

    await agent.arun("first turn", session_id="sid")
    await agent.arun("second turn", session_id="sid")

    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _get_agent_session(storage, "sid")
    assert session is not None
    assert len(session.runs or []) == 2

    stored_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    for run in stored_session.runs or []:
        assert run.messages is not None
        assert not any(message.from_history for message in run.messages)


@pytest.mark.asyncio
async def test_scrub_history_messages_from_sessions_removes_history_and_preserves_get_messages(tmp_path: Path) -> None:
    """Scrubbing should remove duplicated history rows without changing message replay."""
    storage = _make_storage(tmp_path)
    agent = _make_plain_agent(storage, store_history_messages=True)
    await agent.arun("first turn", session_id="sid")
    await agent.arun("second turn", session_id="sid")

    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    expected_messages = [message.content for message in session.get_messages()]

    stats = scrub_history_messages_from_sessions(storage)

    assert stats.sessions_scanned == 1
    assert stats.sessions_changed == 1
    assert stats.messages_removed == 2
    assert stats.size_before_bytes > stats.size_after_bytes

    scrubbed_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert [message.content for message in scrubbed_session.get_messages()] == expected_messages
    assert not any(message.from_history for run in scrubbed_session.runs or [] for message in run.messages or [])


@pytest.mark.asyncio
async def test_compact_session_now_multi_pass_compacts_multiple_runs(tmp_path: Path) -> None:
    """Immediate compaction should keep compacting until the session fits the target window."""
    storage = _make_storage(tmp_path)
    agent = await _seed_session(storage, session_id="sid", turn_count=5)
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    recent_run = session.runs[-1]
    compaction_window = _single_run_compaction_window(session.runs[0])
    summary_responses = [SessionSummary(summary=f"## Goal\npass {index}", topics=[]) for index in range(1, 5)]

    with patch(
        "mindroom.compaction._generate_compaction_summary",
        new_callable=AsyncMock,
        side_effect=summary_responses,
    ) as mock_summary:
        result = await compact_session_now(
            storage=storage,
            session_id="sid",
            agent=agent,
            model=MagicMock(id="compact-model"),
            mode="auto",
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            keep_recent_tokens=estimate_runs_tokens([recent_run]),
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
        )

    assert result is not None
    updated_session, outcome = result
    assert mock_summary.await_count == 4
    assert outcome.compacted_run_count == 4
    assert outcome.runs_before == 5
    assert outcome.runs_after == 2
    assert len(updated_session.runs or []) == 1
    assert updated_session.summary is not None
    assert updated_session.summary.summary == "## Goal\npass 4"
    metadata = updated_session.metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert metadata["seen_event_ids"] == ["$e1", "$e2", "$e3", "$e4"]
    assert get_seen_event_ids(updated_session) == {"$e1", "$e2", "$e3", "$e4", "$e5"}


@pytest.mark.asyncio
async def test_queue_pending_compaction_multi_pass_tracks_total_compacted_count(tmp_path: Path) -> None:
    """Deferred compaction should accumulate the total runs compacted across passes."""
    config = _make_config(tmp_path)
    storage = _make_storage(tmp_path)
    agent = await _seed_session(storage, session_id="sid", turn_count=5)
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    compaction_window = _single_run_compaction_window(session.runs[0])
    summary_responses = [SessionSummary(summary=f"## Goal\ndeferred {index}", topics=[]) for index in range(1, 5)]

    with (
        patch(
            "mindroom.compaction._generate_compaction_summary",
            new_callable=AsyncMock,
            side_effect=summary_responses,
        ) as mock_summary,
        patch("mindroom.compaction.create_session_storage", return_value=storage),
    ):
        queued = await queue_pending_compaction(
            storage=storage,
            session_id="sid",
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=None,
            model=MagicMock(id="compact-model"),
            keep_recent_runs=1,
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
        )
        assert queued is not None
        pending = _PENDING_COMPACTION.get(None)
        assert pending is not None
        assert pending.compacted_count == 4
        assert mock_summary.await_count == 4

        await agent.arun(
            "turn-6 " * 20,
            session_id="sid",
            metadata={"matrix_seen_event_ids": ["$e6"]},
        )
        applied = await apply_pending_compaction()

    assert applied is not None
    assert applied.compacted_run_count == 4
    assert applied.runs_before == 6
    assert applied.runs_after == 2
    saved_session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert len(saved_session.runs or []) == 2
    assert saved_session.summary is not None
    assert saved_session.summary.summary == "## Goal\ndeferred 4"
    assert get_seen_event_ids(saved_session) == {"$e1", "$e2", "$e3", "$e4", "$e5", "$e6"}
    assert _PENDING_COMPACTION.get(None) is None


@pytest.mark.asyncio
async def test_compact_session_now_partial_failure_preserves_last_successful_pass(tmp_path: Path) -> None:
    """A later pass failure should preserve and persist the most recent successful pass."""
    storage = _make_storage(tmp_path)
    agent = await _seed_session(storage, session_id="sid", turn_count=4)
    session = _coerce_agent_session(storage.get_session("sid", SessionType.AGENT))
    assert session.runs is not None
    recent_run = session.runs[-1]
    compaction_window = _single_run_compaction_window(session.runs[0])

    with patch(
        "mindroom.compaction._generate_compaction_summary",
        new_callable=AsyncMock,
        side_effect=[
            SessionSummary(summary="## Goal\nfirst pass", topics=[]),
            RuntimeError("boom"),
        ],
    ) as mock_summary:
        result = await compact_session_now(
            storage=storage,
            session_id="sid",
            agent=agent,
            model=MagicMock(id="compact-model"),
            mode="auto",
            window_tokens=16000,
            threshold_tokens=8000,
            reserve_tokens=1024,
            keep_recent_tokens=estimate_runs_tokens([recent_run]),
            notify=True,
            compaction_model_context_window=compaction_window,
            max_passes=10,
        )

    assert result is not None
    updated_session, outcome = result
    assert mock_summary.await_count == 2
    assert outcome.compacted_run_count == 1
    assert len(updated_session.runs or []) == 3
    assert updated_session.summary is not None
    assert updated_session.summary.summary == "## Goal\nfirst pass"
    metadata = updated_session.metadata[MINDROOM_COMPACTION_METADATA_KEY]
    assert metadata["seen_event_ids"] == ["$e1"]
    assert get_seen_event_ids(updated_session) == {"$e1", "$e2", "$e3", "$e4"}
