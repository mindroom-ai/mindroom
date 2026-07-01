"""Tests for the prepare-history lifecycle and forced/auto compaction."""
# ruff: noqa: D103, TC002, TC003

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.models import CompactionOverrideConfig
from mindroom.execution_preparation import (
    _prepare_bound_team_execution_context,
    prepare_agent_execution_context,
    prepare_bound_team_run_context,
)
from mindroom.history.compaction import (
    _build_summary_input,
    estimate_prompt_visible_history_tokens,
    estimate_session_summary_tokens,
)
from mindroom.history.runtime import (
    open_scope_session_context,
    prepare_bound_scope_history,
    prepare_scope_history,
)
from mindroom.history.storage import (
    read_scope_state,
    write_scope_state,
)
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
from mindroom.thread_utils import create_session_id
from tests.conftest import (
    FakeModel,
    prepare_history_for_run_for_test,
)
from tests.history_helpers import (  # noqa: F401
    RecordingCompactionLifecycle,
    _agent,
    _close_test_storages,
    _completed_run,
    _completed_team_run,
    _make_config,
    _session,
    _team_session,
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
    with open_scope_session_context(
        agent=agent,
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
        scope_context.storage.upsert_session(session)

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
    assert prepared.compaction_outcomes == []


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
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    agent = _agent(db=storage)
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert persisted.runs == []

    state = read_scope_state(persisted, scope)
    assert state.last_summary_model == "summary-model"
    assert state.last_compacted_run_count == 4
    assert state.force_compact_before_next_run is False
    assert state.last_compacted_at is not None

    assert prepared.replays_persisted_history is True
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].summary == "merged summary"


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
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    async def _summary_after_notice(*_args: object, **_kwargs: object) -> SessionSummary:
        assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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

    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].lifecycle_notice_event_id == "$compaction"
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
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    with patch("mindroom.model_loading.get_model_instance", side_effect=ValueError("bad summary model")):
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
    assert prepared.compaction_outcomes == []
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_reply_outcome == "failed"
    assert len(lifecycle.events) == 2
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].notice_event_id == "$compaction"
    assert lifecycle.events[1].failure_reason == "bad summary model"


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
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.runtime._run_scope_compaction", new=AsyncMock(side_effect=asyncio.CancelledError)),
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
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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

    assert prepared.compaction_outcomes == []
    assert prepared.compaction_reply_outcome == "timeout"
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].status == "timeout"
    assert lifecycle.events[1].failure_reason == "TimeoutError"


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_provided_storage_without_reopening_scope_context(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.open_scope_session_context") as mock_open_scope_context:
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

    mock_open_scope_context.assert_not_called()
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
    write_scope_state(thread_session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(room_session)
    storage.upsert_session(thread_session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
        prepared = await prepare_history_for_run_for_test(
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
    assert len(prepared.compaction_outcomes) == 1
    outcome = prepared.compaction_outcomes[0]
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
    storage.upsert_session(session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
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
            _build_summary_input(
                previous_summary=previous_summary,
                compacted_runs=compacted_runs,
                max_input_tokens=budget,
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
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        hard_replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
    )

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 3
    assert summary_mock.await_count == 2
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].compacted_run_count == 3
    assert prepared.compaction_outcomes[0].runs_after == 0


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
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    second_summary_text = "second pass summary"

    def _included_run_count(
        previous_summary: str | None,
        compacted_runs: list[RunOutput | TeamRunOutput],
        budget: int,
    ) -> int:
        return len(
            _build_summary_input(
                previous_summary=previous_summary,
                compacted_runs=compacted_runs,
                max_input_tokens=budget,
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
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=1,
        hard_replay_budget_tokens=1,
        summary_input_budget_tokens=summary_input_budget,
    )

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
    assert len(prepared.compaction_outcomes) == 1
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 3


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_required_compaction_finishes_original_previous_runs(  # noqa: PLR0915
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
    write_scope_state(session, scope, HistoryScopeState(compacted_run_ids=("prior-tombstone",)))
    storage.upsert_session(session)
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
            _build_summary_input(
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
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        hard_replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
    )

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        summary_text = first_summary_text if len(summary_inputs) == 1 else f"summary chunk {len(summary_inputs)}"
        return SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))

    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
    assert len(prepared.compaction_outcomes) == 1
    outcome = prepared.compaction_outcomes[0]
    assert outcome.compacted_run_count == 23
    assert outcome.runs_after == 0
    summary_only_tokens = estimate_session_summary_tokens(persisted.summary.summary)
    assert outcome.after_tokens == summary_only_tokens
    assert outcome.after_tokens < replay_budget
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 23
    assert state.compacted_run_ids == (
        "prior-tombstone",
        *(f"run-{index:02}" for index in range(1, 24)),
    )
    progress_events = [event for event in lifecycle.events if isinstance(event, CompactionLifecycleProgress)]
    assert progress_events
    assert progress_events[-1].runs_remaining > 0
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
    storage.upsert_session(persisted)
    current_run_session = get_agent_session(storage, "session-1")
    assert current_run_session is not None
    assert [run.run_id for run in current_run_session.runs or []] == ["run-24"]
    assert "run-24" not in read_scope_state(current_run_session, scope).compacted_run_ids
    assert (
        estimate_prompt_visible_history_tokens(
            session=current_run_session,
            scope=scope,
            history_settings=history_settings,
        )
        > summary_only_tokens
    )


@pytest.mark.asyncio
async def test_prepare_history_for_run_persists_successful_compaction_chunks_before_later_failure(
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
    storage.upsert_session(session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
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
            _build_summary_input(
                previous_summary=previous_summary,
                compacted_runs=compacted_runs,
                max_input_tokens=budget,
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
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        hard_replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
    )
    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            RuntimeError("summary failed"),
        ],
    )

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
    assert persisted.summary.summary == first_summary_text
    assert [run.run_id for run in persisted.runs or []] == ["run-3"]
    assert summary_mock.await_count == 2
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []


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
    storage.upsert_session(session)

    summary_mock = AsyncMock(
        return_value=SessionSummary(summary="all runs summary", updated_at=datetime.now(UTC)),
    )
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
            available_history_budget=1,
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
            available_history_budget=1,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "all runs summary"
    assert persisted.runs == []
    assert summary_mock.await_count == 1
    assert len(first_prepared.compaction_outcomes) == 1
    assert second_prepared.compaction_outcomes == []


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
    storage.upsert_session(session)
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
    assert prepared.compaction_outcomes == []
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
    storage.upsert_session(session)
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
        static_prompt_tokens=0,
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
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
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
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3", "run-4"]

    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert state.last_summary_model is None
    assert state.last_compacted_run_count is None

    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True


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
    storage.upsert_session(session)

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
    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True
