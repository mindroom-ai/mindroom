"""Tests for the prepare-history lifecycle and forced/auto compaction."""
# ruff: noqa: D103, TC002, TC003

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.media import Image
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary

from mindroom.config.models import CompactionOverrideConfig
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.execution_preparation import (
    _prepare_bound_team_execution_context,
    prepare_agent_execution_context,
    prepare_bound_team_run_context,
)
from mindroom.history.replay import _summary_tokens, estimate_prompt_visible_history_tokens
from mindroom.history.runtime import (
    PreparedScopeHistory,
    prepare_bound_scope_history,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
)
from mindroom.history.session_context import (
    ScopeSessionContext,
    open_resolved_scope_session_context,
    resolve_history_scope,
)
from mindroom.history.storage import (
    read_chunk_basis,
    read_scope_state,
    set_force_compaction_state,
)
from mindroom.history.summary_input import build_summary_input
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
)
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import create_session_storage, get_agent_session
from tests.conftest import (
    FakeModel,
    prepare_history_for_run_for_test,
    seed_session,
)
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    RecordingCompactionLifecycle,
    _agent,
    _completed_run,
    _completed_team_run,
    _make_config,
    _session,
    _team_session,
    compact_runs,
    completed_outcome,
    folded_run_ids,
    latest_summary_model,
)


def test_prepare_scope_history_boundary_does_not_accept_execution_identity() -> None:
    assert "execution_identity" not in inspect.signature(prepare_agent_execution_context).parameters
    assert "execution_identity" not in inspect.signature(_prepare_bound_team_execution_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_bound_team_run_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_bound_scope_history).parameters
    assert "execution_identity" not in inspect.signature(prepare_scope_history).parameters


@pytest.mark.asyncio
async def test_prepare_history_for_run_detects_persisted_team_history(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    agent = _agent()
    agent.team_id = "team-123"
    with open_resolved_scope_session_context(
        scope=resolve_history_scope(agent),
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.scope == HistoryScope(kind="team", scope_id="team-123")
        session = _team_session(
            "session-1",
            team_id="team-123",
            runs=[_completed_team_run("team-1", team_id="team-123")],
            summary=SessionSummary(summary="team summary", updated_at=datetime.now(UTC)),
        )
        seed_session(scope_context.storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    )

    assert prepared.replays_persisted_history is True
    assert prepared.compaction_reply_outcome == "none"


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_rewrites_session(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)

    agent = _agent(db=storage)
    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(
                    summary="merged summary",
                    updated_at=datetime.now(UTC),
                ),
            ),
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert persisted.runs == []

    state = read_scope_state(persisted, scope)
    assert latest_summary_model(storage, scope) == "summary-model"
    assert len(folded_run_ids(storage)) == 4
    assert state.force_compact_before_next_run is False

    assert prepared.replays_persisted_history is True
    assert prepared.compaction_reply_outcome == "success"
    assert completed_outcome(lifecycle).summary == "merged summary"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_starts_lifecycle_before_summary_request(
    tmp_path: Path,
) -> None:
    """Foreground compaction should make the visible lifecycle notice before the summary call blocks."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    async def _summary_after_notice(*_args: object, **_kwargs: object) -> SessionSummary:
        assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=_summary_after_notice),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert completed_outcome(lifecycle).lifecycle_notice_event_id == "$compaction"
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_reply_outcome == "success"
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionOutcome)
    assert lifecycle.events[1].lifecycle_notice_event_id == "$compaction"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_edits_failure_when_model_load_fails(
    tmp_path: Path,
) -> None:
    """Required compaction should surface model-load failure in the lifecycle and continue."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    with patch("mindroom.providers.model_loading.get_model_instance", side_effect=ValueError("bad summary model")):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_reply_outcome == "failed"
    assert len(lifecycle.events) == 2
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].notice_event_id == "$compaction"
    assert lifecycle.events[1].failure_reason == "bad summary model"


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_failure_notice_redacts_credentials(tmp_path: Path) -> None:
    """Room-visible compaction failure text must not carry credentials from the raised error."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1"), _completed_run("run-2")])
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()
    api_key = "sk-" + "proj" + "A1b2C3d4E5f6G7h8J9k0"
    password = "hunter" + "2secret"
    error = RuntimeError(f"Incorrect API key provided: {api_key} at https://user:{password}@llm.internal/v1")

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.runtime.compact_scope_history", new=AsyncMock(side_effect=error)),
    ):
        await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    failure = lifecycle.events[1]
    assert isinstance(failure, CompactionLifecycleFailure)
    assert api_key not in failure.failure_reason
    assert password not in failure.failure_reason
    assert "Incorrect API key provided" in failure.failure_reason


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_edits_failure_when_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation should not leave the visible compaction notice stuck as running."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.runtime.compact_scope_history", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert len(lifecycle.events) == 2
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].notice_event_id == "$compaction"
    assert lifecycle.events[1].status == "failed"
    assert lifecycle.events[1].failure_reason == "CancelledError"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_classifies_provider_timeout(
    tmp_path: Path,
) -> None:
    """Provider TimeoutError should use the timeout lifecycle outcome even with an empty message."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock(side_effect=TimeoutError)),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert prepared.compaction_reply_outcome == "timeout"
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].status == "timeout"
    assert lifecycle.events[1].failure_reason == "TimeoutError"


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_provided_storage_without_reopening_scope_context(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    seed_session(storage, session)

    with patch("mindroom.history.session_context.create_scope_session_storage") as mock_create_scope_storage:
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    mock_create_scope_storage.assert_not_called()
    assert prepared.replay_plan is not None


@pytest.mark.asyncio
async def test_prepare_history_for_run_keeps_thread_session_compaction_isolated(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    room_session_id = create_session_id("!room:localhost", None)
    thread_session_id = create_session_id("!room:localhost", "$thread-1")
    room_session = _session(
        room_session_id,
        runs=[
            _completed_run("room-1"),
            _completed_run("room-2"),
            _completed_run("room-3"),
        ],
    )
    thread_session = _session(
        thread_session_id,
        runs=[
            _completed_run("thread-1"),
            _completed_run("thread-2"),
            _completed_run("thread-3"),
            _completed_run("thread-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(thread_session, scope, HistoryScopeState(), force=True)
    seed_session(storage, room_session)
    seed_session(storage, thread_session)

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(
                    summary="thread summary",
                    updated_at=datetime.now(UTC),
                ),
            ),
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id=thread_session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=thread_session,
        )

    persisted_room = get_agent_session(storage, room_session_id)
    persisted_thread = get_agent_session(storage, thread_session_id)
    assert persisted_room is not None
    assert persisted_thread is not None
    assert persisted_room.summary is None
    assert [run.run_id for run in persisted_room.runs] == ["room-1", "room-2", "room-3"]
    assert persisted_thread.summary is not None
    assert persisted_thread.summary.summary == "thread summary"
    assert persisted_thread.runs == []
    assert prepared.compaction_reply_outcome == "success"
    outcome = completed_outcome(lifecycle)
    assert outcome.session_id == thread_session_id
    assert outcome.scope == scope.key
    assert outcome.to_notice_metadata()["session_id"] == thread_session_id
    assert outcome.to_notice_metadata()["scope"] == scope.key


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_finishes_selected_runs_across_multiple_passes(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    seed_session(storage, session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    second_summary_text = "final summary"

    def _included_run_count(
        previous_summary: str | None,
        compacted_runs: list[RunOutput | TeamRunOutput],
        budget: int,
    ) -> int:
        return len(
            build_summary_input(
                previous_summary=previous_summary,
                compacted_runs=compacted_runs,
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
            )[1],
        )

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_run_count(None, visible_runs, budget) == 2
        and _included_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[2:],
        summary=SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        scope=scope,
        history_settings=history_settings,
    )
    assert (
        estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=history_settings,
        )
        > replay_budget
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        hard_replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
        compaction_timeout_seconds=600.0,
    )

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == second_summary_text
    assert persisted.runs == []
    assert len(folded_run_ids(storage)) == 3
    assert summary_mock.await_count == 2
    assert prepared.compaction_reply_outcome == "success"
    assert completed_outcome(lifecycle).compacted_run_count == 3
    assert completed_outcome(lifecycle).runs_after == 0


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_compaction_runs_to_completion_before_reply(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    seed_session(storage, session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    second_summary_text = "second pass summary"

    def _included_run_count(
        previous_summary: str | None,
        compacted_runs: list[RunOutput | TeamRunOutput],
        budget: int,
    ) -> int:
        return len(
            build_summary_input(
                previous_summary=previous_summary,
                compacted_runs=compacted_runs,
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
            )[1],
        )

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_run_count(None, visible_runs, budget) == 2
        and _included_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=1,
        hard_replay_budget_tokens=1,
        summary_input_budget_tokens=summary_input_budget,
        compaction_timeout_seconds=600.0,
    )

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == second_summary_text
    assert persisted.runs == []
    assert summary_mock.await_count == 2
    assert prepared.compaction_reply_outcome == "success"
    assert len(folded_run_ids(storage)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_native_compaction", [True, False])
async def test_prepare_scope_history_compacts_to_trigger_budget_when_native_compaction_is_disallowed(
    tmp_path: Path,
    *,
    allow_native_compaction: bool,
) -> None:
    """A scheduled run with a history limit cannot rely on native compaction above the trigger budget."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                f"run-{index}",
                messages=[
                    Message(role="user", content="u" * 2_000),
                    Message(role="assistant", content="a" * 2_000),
                ],
            )
            for index in range(3)
        ],
    )
    seed_session(storage, session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    history_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=_ALL_HISTORY_SETTINGS,
    )
    trigger_budget = history_tokens // 2
    hard_budget = history_tokens * 2
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=trigger_budget,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=trigger_budget,
        hard_replay_budget_tokens=hard_budget,
        summary_input_budget_tokens=50_000,
        compaction_timeout_seconds=600.0,
    )
    agent = _agent(db=storage)
    resolved_inputs = replace(
        resolve_agent_preparation_inputs(agent=agent, agent_name="test_agent", config=config, static_prompt_tokens=0),
        history_settings=_ALL_HISTORY_SETTINGS,
        execution_plan=execution_plan,
    )
    summary_mock = AsyncMock(return_value=SessionSummary(summary="short summary", updated_at=datetime.now(UTC)))
    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_scope_history(
            compaction_lifecycle=lifecycle,
            agent=agent,
            agent_name="test_agent",
            resolved_inputs=resolved_inputs,
            non_prompt_tokens=0,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=ScopeSessionContext(scope=scope, storage=storage, session=session, session_id="session-1"),
            allow_native_compaction=allow_native_compaction,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    if allow_native_compaction:
        assert prepared.compaction_decision.mode == "none"
        assert prepared.compaction_decision.reason == "within_hard_budget"
        assert prepared.compaction_reply_outcome == "none"
        summary_mock.assert_not_awaited()
        assert len(persisted.runs or []) == 3
        return

    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_decision.reason == "over_trigger_native_disabled"
    assert prepared.compaction_reply_outcome == "success"
    summary_mock.assert_awaited_once()
    outcome = completed_outcome(lifecycle)
    assert outcome.history_budget_tokens == trigger_budget
    assert outcome.before_tokens == history_tokens
    assert outcome.after_tokens <= trigger_budget
    assert (
        estimate_prompt_visible_history_tokens(
            session=persisted,
            scope=scope,
            history_settings=_ALL_HISTORY_SETTINGS,
        )
        <= trigger_budget
    )


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_required_compaction_finishes_original_previous_runs(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    previous_runs = [
        _completed_run(
            f"run-{index:02}",
            messages=[
                Message(role="user", content=f"RUN-{index:02} user " + ("u" * 200)),
                Message(role="assistant", content=f"RUN-{index:02} assistant " + ("a" * 200)),
            ],
        )
        for index in range(1, 24)
    ]
    session = _session(
        "session-1",
        runs=previous_runs,
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    seed_session(storage, session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    summary_inputs: list[str] = []

    summary_input_budget = next(
        budget
        for budget in range(1, 20_000)
        if len(
            build_summary_input(
                previous_summary=None,
                compacted_runs=visible_runs,
                history_settings=history_settings,
                max_input_tokens=budget,
            )[1],
        )
        == 9
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[9:],
        summary=SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        scope=scope,
        history_settings=history_settings,
    )
    before_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    assert before_tokens > replay_budget

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        hard_replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
        compaction_timeout_seconds=600.0,
    )

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        summary_text = first_summary_text if len(summary_inputs) == 1 else f"summary chunk {len(summary_inputs)}"
        return SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))

    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=fake_summary),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="CURRENT-RUN prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
            compaction_lifecycle=lifecycle,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.runs == []
    assert len(summary_inputs) > 1
    assert "RUN-09" in summary_inputs[0]
    assert "RUN-10" not in summary_inputs[0]
    assert all("CURRENT-RUN" not in summary_input for summary_input in summary_inputs)
    assert prepared.compaction_reply_outcome == "success"
    outcome = completed_outcome(lifecycle)
    assert outcome.compacted_run_count == 23
    assert outcome.runs_after == 0
    summary_only_tokens = _summary_tokens(persisted)
    assert outcome.after_tokens == summary_only_tokens
    assert outcome.after_tokens < replay_budget
    assert folded_run_ids(storage) == [f"run-{index:02}" for index in range(1, 24)]
    progress_events = [event for event in lifecycle.events if isinstance(event, CompactionLifecycleProgress)]
    assert progress_events
    assert progress_events[-1].runs_remaining > 0
    # Every progress edit targets the notice the compaction started.
    assert {event.notice_event_id for event in progress_events} == {lifecycle.start_event_id}
    assert isinstance(lifecycle.events[-1], CompactionOutcome)

    persisted.runs = [
        _completed_run(
            "run-24",
            messages=[
                Message(role="user", content="CURRENT-RUN user"),
                Message(role="assistant", content="CURRENT-RUN assistant"),
            ],
        ),
    ]
    seed_session(storage, persisted)
    current_run_session = get_agent_session(storage, "session-1")
    assert current_run_session is not None
    assert [run.run_id for run in current_run_session.runs or []] == ["run-24"]
    assert "run-24" not in folded_run_ids(storage)
    assert (
        estimate_prompt_visible_history_tokens(
            session=current_run_session,
            scope=scope,
            history_settings=history_settings,
        )
        > summary_only_tokens
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_chunk", ["fails", "outrun"])
async def test_prepare_history_for_run_persists_successful_compaction_chunks_before_later_failure(
    tmp_path: Path,
    second_chunk: str,
) -> None:
    """A later chunk's failure, or a writer outrunning it, keeps the chunks already stored."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    seed_session(storage, session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"

    def _included_run_count(
        previous_summary: str | None,
        compacted_runs: list[RunOutput | TeamRunOutput],
        budget: int,
    ) -> int:
        return len(
            build_summary_input(
                previous_summary=previous_summary,
                compacted_runs=compacted_runs,
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
            )[1],
        )

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_run_count(None, visible_runs, budget) == 2
        and _included_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[2:],
        summary=SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        scope=scope,
        history_settings=history_settings,
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        hard_replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
        compaction_timeout_seconds=600.0,
    )

    async def summarize(**_kwargs: object) -> SessionSummary:
        if summary_mock.await_count == 1:
            return SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC))
        if second_chunk == "fails":
            msg = "summary failed"
            raise RuntimeError(msg)
        storage.delete_runs(["run-3"])
        return SessionSummary(summary="stale second summary", updated_at=datetime.now(UTC))

    summary_mock = AsyncMock(side_effect=summarize)

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
            history_settings=history_settings,
            execution_plan=execution_plan,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == first_summary_text
    assert [run.run_id for run in persisted.runs or []] == (["run-3"] if second_chunk == "fails" else [])
    assert summary_mock.await_count == 2
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    if second_chunk == "fails":
        assert prepared.compaction_reply_outcome == "failed"
    else:
        assert prepared.compaction_reply_outcome == "success"
        outcome = completed_outcome(lifecycle)
        assert outcome.summary == first_summary_text
        assert outcome.compacted_run_count == 2


@pytest.mark.asyncio
async def test_prepare_history_for_run_failure_notice_reports_serving_fallback_model(
    tmp_path: Path,
) -> None:
    """A chunk-2 failure after a fallback-served chunk 1 reports the fallback, not the primary."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="v" * 200),
                    Message(role="assistant", content="b" * 200),
                ],
            ),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    visible_runs = list(session.runs or [])
    first_summary_text = "first chunk summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if len(
            build_summary_input(
                previous_summary=None,
                compacted_runs=visible_runs,
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
            )[1],
        )
        == 1
        and len(
            build_summary_input(
                previous_summary=first_summary_text,
                compacted_runs=visible_runs[1:],
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
            )[1],
        )
        == 1
    )
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=1,
        hard_replay_budget_tokens=1,
        summary_input_budget_tokens=summary_input_budget,
        compaction_timeout_seconds=600.0,
        compaction_fallback_model_name="fallback-model",
        compaction_fallback_summary_input_budget_tokens=summary_input_budget,
    )
    # Chunk 1: primary refuses, the fallback serves the retry; chunk 2 then
    # fails hard on the fallback.
    summary_mock = AsyncMock(
        side_effect=[
            ModelSafeguardRefusalError("provider-specific refusal wording"),
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            RuntimeError("summary failed"),
        ],
    )
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            side_effect=lambda _config, _paths, name, **_kwargs: FakeModel(id=f"{name}-id", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            execution_plan=execution_plan,
            compaction_lifecycle=lifecycle,
        )

    assert summary_mock.await_count == 3
    start_events = [event for event in lifecycle.events if isinstance(event, CompactionLifecycleStart)]
    assert [event.summary_model for event in start_events] == ["summary-model"]
    progress_events = [event for event in lifecycle.events if isinstance(event, CompactionLifecycleProgress)]
    assert [event.summary_model for event in progress_events] == ["fallback-model"]
    failure_events = [event for event in lifecycle.events if isinstance(event, CompactionLifecycleFailure)]
    assert [event.summary_model for event in failure_events] == ["fallback-model"]
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == first_summary_text
    assert [run.run_id for run in persisted.runs or []] == ["run-2"]


@pytest.mark.asyncio
async def test_prepare_history_for_run_compacts_on_primary_when_fallback_construction_fails(
    tmp_path: Path,
) -> None:
    """A fallback that fails to load is logged and unavailable; the healthy primary still compacts."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_enabled=True,
        text_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=1,
        hard_replay_budget_tokens=1,
        summary_input_budget_tokens=16_000,
        compaction_timeout_seconds=600.0,
        compaction_fallback_model_name="fallback-model",
        compaction_fallback_summary_input_budget_tokens=16_000,
    )

    def _load_model(_config: object, _paths: object, name: str = "default", **_kwargs: object) -> FakeModel:
        if name == "fallback-model":
            message = "fallback provider SDK unavailable"
            raise RuntimeError(message)
        return FakeModel(id=f"{name}-id", provider="fake")

    summary_mock = AsyncMock(
        return_value=SessionSummary(summary="primary summary", updated_at=datetime.now(UTC)),
    )
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch("mindroom.providers.model_loading.get_model_instance", side_effect=_load_model),
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.runtime.logger.warning") as warning_mock,
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            execution_plan=execution_plan,
            compaction_lifecycle=lifecycle,
        )

    assert prepared.compaction_reply_outcome == "success"
    assert completed_outcome(lifecycle).summary_model == "summary-model"
    assert summary_mock.await_count == 1
    assert summary_mock.await_args.kwargs["model"].id == "summary-model-id"
    fallback_warnings = [
        call
        for call in warning_mock.call_args_list
        if call.args[0] == "Compaction fallback model failed to load; continuing without a fallback"
    ]
    assert len(fallback_warnings) == 1
    assert fallback_warnings[0].kwargs["fallback_model"] == "fallback-model"
    assert not any(isinstance(event, CompactionLifecycleFailure) for event in lifecycle.events)
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "primary summary"
    assert latest_summary_model(storage, scope) == "summary-model-id"


@pytest.mark.asyncio
async def test_prepare_history_for_run_reuses_completed_auto_compaction(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-4",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    seed_session(storage, session)

    summary_mock = AsyncMock(
        return_value=SessionSummary(summary="all runs summary", updated_at=datetime.now(UTC)),
    )
    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        first_prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            available_history_budget=100,
        )
        persisted_before_second = get_agent_session(storage, "session-1")
        assert persisted_before_second is not None
        second_prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=persisted_before_second,
            available_history_budget=100,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "all runs summary"
    assert persisted.runs == []
    assert summary_mock.await_count == 1
    assert first_prepared.compaction_reply_outcome == "success"
    assert second_prepared.compaction_reply_outcome == "none"


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_context_window_guard_without_authored_compaction(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=600)
    config.defaults.compaction = None
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )
    seed_session(storage, session)
    agent = _agent(db=storage)
    prepared = await prepare_history_for_run_for_test(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 2
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_context_window_guard_preserves_custom_system_message_role(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=40)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="developer", content="d" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="developer", content="d" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
        ],
    )
    seed_session(storage, session)
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    agent = _agent(db=storage)

    prepared = await prepare_history_for_run_for_test(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=persisted,
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
            system_message_role="developer",
        ),
        # The 40-token window minus its clamped 20-token reserve leaves 10 tokens of history beside this prompt.
        static_prompt_tokens=10,
        available_history_budget=10,
    )

    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_failure_clears_force_flag(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    flag_at_failure_notice: list[bool] = []

    @dataclass
    class _FlagAtFailure(RecordingCompactionLifecycle):
        async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
            stored = get_agent_session(storage, "session-1")
            assert stored is not None
            flag_at_failure_notice.append(read_scope_state(stored, scope).force_compact_before_next_run)
            await super().complete_failure(event)

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=RuntimeError("summary failed")),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=_FlagAtFailure(),
        )

    # The failed manual request is cleared before its failure notice, so a reply cannot retry it.
    assert flag_at_failure_notice == [False]

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3", "run-4"]

    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert latest_summary_model(storage, scope) is None

    assert prepared.compaction_reply_outcome == "failed"
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["before_summary", "during_summary"])
async def test_a_manual_compaction_the_conversation_outran_says_so_and_stays_requested(
    tmp_path: Path,
    race: str,
) -> None:
    """A redaction landing before or during the summary call voids the chunk; the notice names it and the request stays."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1"), _completed_run("run-2")])
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    def basis_after_a_redaction_cuts_a_run(*args: object, **kwargs: object) -> object:
        if race == "before_summary":
            storage.delete_runs(["run-1"])
        return read_chunk_basis(*args, **kwargs)

    async def summary_while_a_redaction_cuts_a_run(**_kwargs: object) -> SessionSummary:
        if race == "during_summary":
            storage.delete_runs(["run-1"])
        return SessionSummary(summary="stale summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.compaction.read_chunk_basis", new=basis_after_a_redaction_cuts_a_run),
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_while_a_redaction_cuts_a_run),
    ):
        prepared = await prepare_history_for_run_for_test(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert read_scope_state(persisted, scope).force_compact_before_next_run is True
    # The reply plans with what the conversation holds now, not the run the redaction removed.
    assert [run.run_id for run in session.runs or []] == ["run-2"]
    assert prepared.compaction_reply_outcome == "failed"
    assert isinstance(lifecycle.events[-1], CompactionLifecycleFailure)
    assert lifecycle.events[-1].failure_reason == "The conversation changed during compaction."


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_context_window_skips_auto_compaction(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, threshold_tokens=10),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
        ],
    )
    seed_session(storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent=_agent(db=storage),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.replays_persisted_history is True


# Three runs of 2,000 + 2,000 characters estimate to 3,000 history tokens at chars / 4, under the trigger.
_REPORTED_USAGE_PLAN = ResolvedHistoryExecutionPlan(
    authored_compaction_enabled=True,
    text_compaction_available=True,
    explicit_compaction_model=True,
    compaction_model_name="summary-model",
    replay_window_tokens=64_000,
    trigger_threshold_tokens=5_000,
    reserve_tokens=0,
    static_prompt_tokens=1_000,
    replay_budget_tokens=4_000,
    hard_replay_budget_tokens=6_000,
    summary_input_budget_tokens=50_000,
    compaction_timeout_seconds=600.0,
)


_SUMMARY_WRITTEN_AT = 2_000_000_000


def _reported_run(
    run_id: str,
    *,
    input_tokens: int = 0,
    model: str = "fake-model",
    created_at: int = _SUMMARY_WRITTEN_AT + 1,
    images: list[Image] | None = None,
) -> RunOutput:
    """Return a completed run whose answer carries the request size its provider reported.

    By default the answer's request starts the second after any summary a test writes.
    """
    run = _completed_run(
        run_id,
        messages=[
            Message(role="user", content="u" * 2_000, images=images),
            Message(
                role="assistant",
                content="a" * 2_000,
                metrics=MessageMetrics(input_tokens=input_tokens),
                created_at=created_at,
            ),
        ],
    )
    run.model = model
    run.model_provider = "fake"
    return run


async def _prepare_with_reported_usage(
    tmp_path: Path,
    latest: dict[str, object],
    *,
    history_settings: ResolvedHistorySettings = _ALL_HISTORY_SETTINGS,
    folded_run_ids: tuple[str, ...] = (),
) -> tuple[PreparedScopeHistory, int, RecordingCompactionLifecycle]:
    """Prepare a reply after three runs; return it, the chars / 4 history estimate, and the compaction notices.

    ``latest`` configures the newest run, and a summary folding ``folded_run_ids`` is stored as written at
    ``_SUMMARY_WRITTEN_AT``. Of the plan's 1,000 static prompt tokens, 400 are the system prompt and tools.
    """
    runs = [_reported_run("run-0"), _reported_run("run-1"), _reported_run("run-2", **latest)]
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = seed_session(storage, _session("session-1", runs=runs))
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    if folded_run_ids:
        with patch("mindroom.history.summary_rows.time.time", return_value=_SUMMARY_WRITTEN_AT):
            compact_runs(storage, session, scope, list(folded_run_ids), "Earlier summary")
    estimate = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    agent = _agent(db=storage)
    resolved_inputs = replace(
        resolve_agent_preparation_inputs(agent=agent, agent_name="test_agent", config=config, static_prompt_tokens=0),
        history_settings=history_settings,
        execution_plan=_REPORTED_USAGE_PLAN,
    )
    lifecycle = RecordingCompactionLifecycle()
    with (
        patch(
            "mindroom.providers.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="short summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_scope_history(
            compaction_lifecycle=lifecycle,
            agent=agent,
            agent_name="test_agent",
            resolved_inputs=resolved_inputs,
            non_prompt_tokens=400,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=ScopeSessionContext(scope=scope, storage=storage, session=session, session_id="session-1"),
        )
    return prepared, estimate, lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "folded_run_ids",
    [pytest.param((), id="no-summary"), pytest.param(("run-0",), id="summary-before-the-request")],
)
async def test_the_latest_reported_request_size_decides_compaction_before_a_reply(
    tmp_path: Path,
    folded_run_ids: tuple[str, ...],
) -> None:
    """Chars / 4 undercounts real tokenizers (Qwen by about 1.3x), so the provider's count of the last request wins."""
    prepared, estimate, lifecycle = await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 8_000},
        folded_run_ids=folded_run_ids,
    )

    assert estimate < 4_000  # The trigger budget.
    # That request held the system prompt, tools, and all history but its own 500-token answer.
    expected = 8_000 + 500 - 400
    assert prepared.compaction_decision.current_history_tokens == expected
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_decision.reason == "history_exceeds_hard_budget"
    assert prepared.compaction_reply_outcome == "success"
    assert completed_outcome(lifecycle).before_tokens == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest", "setup"),
    [
        pytest.param({}, {}, id="no-reported-usage"),
        pytest.param({"input_tokens": 2_000}, {}, id="below-the-estimate"),
        pytest.param({"input_tokens": 8_000, "model": "other-model"}, {}, id="another-model-answered"),
        pytest.param(
            {"input_tokens": 8_000, "images": [Image(url="https://example.com/chart.png")]},
            {},
            id="media-replay-drops",
        ),
        # Each new run pushes the oldest replayed content out, so the last request held more than will replay.
        pytest.param(
            {"input_tokens": 8_000},
            {"history_settings": replace(_ALL_HISTORY_SETTINGS, policy=HistoryPolicy(mode="runs", limit=2))},
            id="run-limit",
        ),
        pytest.param(
            {"input_tokens": 8_000},
            {"history_settings": replace(_ALL_HISTORY_SETTINGS, max_tool_calls_from_history=1)},
            id="tool-call-limit",
        ),
        # A compaction after that request folded runs it held; whole-second stamps cannot order a same-second one.
        pytest.param(
            {"input_tokens": 8_000, "created_at": _SUMMARY_WRITTEN_AT - 1},
            {"folded_run_ids": ("run-0",)},
            id="summary-after-the-request",
        ),
        pytest.param(
            {"input_tokens": 8_000, "created_at": _SUMMARY_WRITTEN_AT},
            {"folded_run_ids": ("run-0",)},
            id="summary-in-the-same-second",
        ),
    ],
)
async def test_history_keeps_its_estimate_without_a_usable_reported_size(
    tmp_path: Path,
    latest: dict[str, object],
    setup: dict[str, object],
) -> None:
    prepared, estimate, lifecycle = await _prepare_with_reported_usage(tmp_path, latest, **setup)

    assert prepared.compaction_decision.current_history_tokens == estimate
    assert prepared.compaction_decision.reason == "under_trigger"
    assert lifecycle.events == []
