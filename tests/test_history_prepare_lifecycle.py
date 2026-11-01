"""Tests for the prepare-history lifecycle and forced/auto compaction."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.ai_run_metadata import build_prepared_history_metadata_content
from mindroom.config.models import CompactionOverrideConfig
from mindroom.config.resolution import entity_model_name
from mindroom.execution_preparation import prepare_agent_execution_context, prepare_bound_team_run_context
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.replay import estimate_prompt_visible_history_tokens, estimate_summary_tokens
from mindroom.history.runtime import (
    PreparedScopeHistory,
    finalize_history_preparation,
    prepare_scope_history,
    resolve_entity_preparation_inputs,
)
from mindroom.history.session_context import (
    ScopeSessionContext,
    load_scope_session,
)
from mindroom.history.storage import (
    read_chunk_basis,
    read_scope_state,
    with_force_compaction,
)
from mindroom.history.summary_input import build_summary_input
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    NativeHistory,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
    ScopeSummary,
)
from mindroom.llm.errors import ModelSafeguardRefusal
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, ModelIdentity, NativeCompactionReplay, TokenCounter
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import NATIVE_CHECKPOINT_KEY
from mindroom.providers.assistant_turn import stamp_model
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import delete_runs, install_model_providers, prepare_history_for_run_for_test, seed_session
from tests.fakes.scripted_model import ScriptedModel, estimate_only_model
from tests.fakes.summary_model import ScriptedSummaryModel, summary_model
from tests.fakes.token_counter import fake_token_counter, quarter_tokens
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    RecordingCompactionLifecycle,
    ReplayEstimate,
    _completed_run,
    _completed_team_run,
    _make_config,
    _session,
    _team_session,
    compact_runs,
    completed_outcome,
    folded_run_ids,
    summary_rows_of,
)
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from mindroom.llm.runs import RunRecord, TeamRunRecord


def _included_run_count(
    previous_summary: str | None,
    compacted_runs: list[RunRecord | TeamRunRecord],
    budget: int,
) -> int:
    return len(
        build_summary_input(
            previous_summary=previous_summary,
            compacted_runs=compacted_runs,
            max_input_tokens=budget,
            history_settings=_ALL_HISTORY_SETTINGS,
            token_estimator=quarter_tokens,
        )[1],
    )


def test_prepare_scope_history_boundary_does_not_accept_execution_identity() -> None:
    assert "execution_identity" not in inspect.signature(prepare_agent_execution_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_bound_team_run_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_scope_history).parameters


@pytest.mark.asyncio
async def test_prepare_history_for_run_detects_persisted_team_history(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    scope_context = load_scope_session(
        scope=team_scope,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    )
    assert scope_context.scope == team_scope
    session = _team_session(
        "session-1",
        team_id="team-123",
        runs=[_completed_team_run("team-1", team_id="team-123")],
        summary=ScopeSummary(text="team summary", updated_at=datetime.now(UTC)),
    )
    seed_session(scope_context.storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        scope=team_scope,
    )

    assert prepared.replays_persisted_history is True
    assert prepared.compaction_reply_outcome == "none"


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_rewrites_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(
            return_value="merged summary",
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "merged summary"
    assert persisted.runs == []

    state = read_scope_state(persisted)
    assert len(folded_run_ids(storage)) == 4
    assert state.force_compact_before_next_run is False

    assert prepared.replays_persisted_history is True
    assert prepared.compaction_reply_outcome == "success"
    start, outcome = lifecycle.events
    assert isinstance(start, CompactionLifecycleStart)
    assert isinstance(outcome, CompactionOutcome)
    assert outcome.summary == "merged summary"
    # A requested compaction reports itself as manual from its first notice to its outcome.
    assert (start.mode, outcome.mode) == ("manual", "manual")


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_starts_lifecycle_before_summary_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    async def _summary_after_notice(*_args: object, **_kwargs: object) -> str:
        assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
        return "merged summary"

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=_summary_after_notice),
    ):
        prepared = await prepare_history_for_run_for_test(
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    with patch("mindroom.providers.registry.build_model_provider", side_effect=ValueError("bad summary model")):
        prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert read_scope_state(persisted).force_compact_before_next_run is False
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_reply_outcome == "failed"
    assert len(lifecycle.events) == 2
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].notice_event_id == "$compaction"
    assert lifecycle.events[1].failure_reason == "bad summary model"


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_failure_notice_redacts_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Room-visible compaction failure text must not carry credentials from the raised error."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1"), _completed_run("run-2")])
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()
    api_key = "sk-" + "proj" + "A1b2C3d4E5f6G7h8J9k0"
    password = "hunter" + "2secret"
    error = RuntimeError(f"Incorrect API key provided: {api_key} at https://user:{password}@llm.internal/v1")

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch("mindroom.history.runtime.compact_scope_history", new=AsyncMock(side_effect=error)):
        await prepare_history_for_run_for_test(
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
    monkeypatch: pytest.MonkeyPatch,
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    install_model_providers(monkeypatch, {"default": summary_model()})
    with (
        patch("mindroom.history.runtime.compact_scope_history", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await prepare_history_for_run_for_test(
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
    monkeypatch: pytest.MonkeyPatch,
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock(side_effect=TimeoutError)):
        prepared = await prepare_history_for_run_for_test(
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
async def test_prepare_history_for_run_keeps_thread_session_compaction_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    thread_session.metadata = with_force_compaction(thread_session.metadata)
    seed_session(storage, room_session)
    seed_session(storage, thread_session)

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(
            return_value="thread summary",
        ),
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id=thread_session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=thread_session,
        )

    persisted_room = storage.load_session(room_session_id)
    persisted_thread = storage.load_session(thread_session_id)
    assert persisted_room is not None
    assert persisted_thread is not None
    assert persisted_room.summary is None
    assert [run.run_id for run in persisted_room.runs] == ["room-1", "room-2", "room-3"]
    assert persisted_thread.summary is not None
    assert persisted_thread.summary.text == "thread summary"
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
    monkeypatch: pytest.MonkeyPatch,
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    history_settings = config.resolve_scope("test_agent").history_settings
    visible_runs = list(session.runs)
    first_summary_text = "first pass summary"
    second_summary_text = "final summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_run_count(None, visible_runs, budget) == 2
        and _included_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[2:],
        summary=ScopeSummary(text=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )
    assert (
        estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=history_settings,
            counter=fake_token_counter(),
        )
        > replay_budget
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
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
            first_summary_text,
            second_summary_text,
        ],
    )
    install_model_providers(monkeypatch, {"summary-model": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=summary_mock,
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
            compaction_lifecycle=lifecycle,
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == second_summary_text
    assert persisted.runs == []
    assert len(folded_run_ids(storage)) == 3
    assert summary_mock.await_count == 2
    assert prepared.compaction_reply_outcome == "success"
    assert completed_outcome(lifecycle).compacted_run_count == 3
    assert completed_outcome(lifecycle).runs_after == 0


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_compaction_runs_to_completion_before_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    history_settings = config.resolve_scope("test_agent").history_settings
    visible_runs = list(session.runs)
    first_summary_text = "first pass summary"
    second_summary_text = "second pass summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_run_count(None, visible_runs, budget) == 2
        and _included_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
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
            first_summary_text,
            second_summary_text,
        ],
    )
    install_model_providers(monkeypatch, {"summary-model": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=summary_mock,
    ):
        prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == second_summary_text
    assert persisted.runs == []
    assert summary_mock.await_count == 2
    assert prepared.compaction_reply_outcome == "success"
    assert len(folded_run_ids(storage)) == 3


@dataclass(frozen=True)
class _NativeRoute:
    """A route that supports native provider compaction."""

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay | None:
        del history_generation, allow_authored
        return None if threshold is None else NativeCompactionReplay(route="test-endpoint", threshold=threshold)


def _native_model() -> ScriptedModel:
    """Return a model on a route that supports native provider compaction."""
    return ScriptedModel([], capabilities=Capabilities(native_compaction=True), native_history_route=_NativeRoute())


@pytest.mark.asyncio
async def test_prepare_scope_history_skips_trigger_compaction_when_the_summary_alone_exceeds_the_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compacting cannot bring history under the trigger when the summary alone exceeds it.

    Otherwise every short reply on a text-only route would pay to rewrite the same summary.
    """
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    summary = ScopeSummary(text="critical work detail " * 500, updated_at=datetime.now(UTC))
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    summary_tokens = estimate_summary_tokens(_session("session-1", runs=[], summary=summary), fake_token_counter())
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-0",
                messages=[Message(role="user", content="thanks"), Message(role="assistant", content="you are welcome")],
            ),
        ],
        summary=summary,
    )
    seed_session(storage, session)
    # A summary exactly at the trigger already leaves no room for anything compaction keeps.
    trigger_budget = summary_tokens
    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
        text_compaction_available=True,
        explicit_compaction_model=False,
        compaction_model_name="summary-model",
        replay_window_tokens=64_000,
        trigger_threshold_tokens=trigger_budget,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=trigger_budget,
        hard_replay_budget_tokens=summary_tokens * 4,
        summary_input_budget_tokens=50_000,
        compaction_timeout_seconds=600.0,
    )
    resolved_inputs = replace(
        resolve_entity_preparation_inputs(
            history_settings=_ALL_HISTORY_SETTINGS,
            config=config,
            runtime_paths=runtime_paths,
            static_prompt_tokens=0,
            active_model_name=entity_model_name(config, "test_agent"),
            active_context_window=None,
        ),
        execution_plan=execution_plan,
    )
    summary_mock = AsyncMock(return_value=summary.text)
    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock):
        prepared = await prepare_scope_history(
            compaction_lifecycle=RecordingCompactionLifecycle(),
            model=estimate_only_model(),
            resolved_inputs=resolved_inputs,
            non_prompt_tokens=0,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=ScopeSessionContext(
                scope=scope,
                storage=storage,
                session=session,
                session_id="session-1",
                storage_factory=lambda: storage,
            ),
            pipeline_timing=None,
            allow_native_compaction=True,
        )

    assert prepared.compaction_decision.mode == "none"
    assert prepared.compaction_decision.reason == "trigger_unreachable"
    summary_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_model", "allow_native_compaction"),
    [(True, True), (True, False), (False, True)],
    ids=["native-route", "native-disallowed", "text-only-route"],
)
async def test_prepare_scope_history_compacts_to_trigger_budget_without_a_native_route(
    tmp_path: Path,
    *,
    native_model: bool,
    allow_native_compaction: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the trigger budget only an active native route may leave history uncompacted.

    A scheduled run with a history limit disallows native compaction, and a text-only route (OpenRouter, Gemini, a
    local model) never had it; neither has anything that compacts between the trigger and hard budgets.
    """
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
        history_settings=_ALL_HISTORY_SETTINGS,
        counter=fake_token_counter(),
    )
    trigger_budget = history_tokens // 2
    hard_budget = history_tokens * 2
    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
        text_compaction_available=True,
        explicit_compaction_model=False,
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
    resolved_inputs = replace(
        resolve_entity_preparation_inputs(
            history_settings=_ALL_HISTORY_SETTINGS,
            config=config,
            runtime_paths=runtime_paths,
            static_prompt_tokens=0,
            active_model_name=entity_model_name(config, "test_agent"),
            active_context_window=None,
        ),
        execution_plan=execution_plan,
    )
    summary_mock = AsyncMock(return_value="short summary")
    install_model_providers(monkeypatch, {"summary-model": summary_model()})
    with patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_scope_history(
            compaction_lifecycle=lifecycle,
            model=_native_model() if native_model else estimate_only_model(),
            resolved_inputs=resolved_inputs,
            non_prompt_tokens=0,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=ScopeSessionContext(
                scope=scope,
                storage=storage,
                session=session,
                session_id="session-1",
                storage_factory=lambda: storage,
            ),
            allow_native_compaction=allow_native_compaction,
            pipeline_timing=None,
        )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    if native_model and allow_native_compaction:
        assert prepared.compaction_decision.mode == "none"
        assert prepared.compaction_decision.reason == "within_hard_budget"
        assert prepared.compaction_reply_outcome == "none"
        summary_mock.assert_not_awaited()
        assert len(persisted.runs) == 3
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
            history_settings=_ALL_HISTORY_SETTINGS,
            counter=fake_token_counter(),
        )
        <= trigger_budget
    )


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_required_compaction_finishes_original_previous_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    seed_session(storage, session)
    history_settings = config.resolve_scope("test_agent").history_settings
    visible_runs = list(session.runs)
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
                token_estimator=quarter_tokens,
            )[1],
        )
        == 9
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[9:],
        summary=ScopeSummary(text=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )
    before_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )
    assert before_tokens > replay_budget

    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
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

    async def fake_summary(*, summary_input: str, **_kwargs: object) -> str:
        summary_inputs.append(summary_input)
        return first_summary_text if len(summary_inputs) == 1 else f"summary chunk {len(summary_inputs)}"

    lifecycle = RecordingCompactionLifecycle()

    install_model_providers(monkeypatch, {"summary-model": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=fake_summary),
    ):
        prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
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
    summary_only_tokens = estimate_summary_tokens(persisted, fake_token_counter())
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
    current_run_session = storage.load_session("session-1")
    assert current_run_session is not None
    assert [run.run_id for run in current_run_session.runs] == ["run-24"]
    assert "run-24" not in folded_run_ids(storage)
    assert (
        estimate_prompt_visible_history_tokens(
            session=current_run_session,
            history_settings=history_settings,
            counter=fake_token_counter(),
        )
        > summary_only_tokens
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_chunk", ["fails", "outrun"])
async def test_prepare_history_for_run_persists_successful_compaction_chunks_before_later_failure(
    tmp_path: Path,
    second_chunk: str,
    monkeypatch: pytest.MonkeyPatch,
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    history_settings = config.resolve_scope("test_agent").history_settings
    visible_runs = list(session.runs)
    first_summary_text = "first pass summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_run_count(None, visible_runs, budget) == 2
        and _included_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[2:],
        summary=ScopeSummary(text=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
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

    async def summarize(**_kwargs: object) -> str:
        if summary_mock.await_count == 1:
            return first_summary_text
        if second_chunk == "fails":
            msg = "summary failed"
            raise RuntimeError(msg)
        delete_runs(storage, ["run-3"], session_id="session-1")
        return "stale second summary"

    summary_mock = AsyncMock(side_effect=summarize)

    install_model_providers(monkeypatch, {"summary-model": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=summary_mock,
    ):
        lifecycle = RecordingCompactionLifecycle()
        prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == first_summary_text
    assert [run.run_id for run in persisted.runs] == (["run-3"] if second_chunk == "fails" else [])
    assert summary_mock.await_count == 2
    assert read_scope_state(persisted).force_compact_before_next_run is False
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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    visible_runs = list(session.runs)
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
                token_estimator=quarter_tokens,
            )[1],
        )
        == 1
        and len(
            build_summary_input(
                previous_summary=first_summary_text,
                compacted_runs=visible_runs[1:],
                max_input_tokens=budget,
                history_settings=_ALL_HISTORY_SETTINGS,
                token_estimator=quarter_tokens,
            )[1],
        )
        == 1
    )
    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
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
            ModelSafeguardRefusal(provider="fake", usage=MessageMetrics()),
            first_summary_text,
            RuntimeError("summary failed"),
        ],
    )
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.providers.registry.build_model_provider",
            side_effect=lambda _config, _paths, name, **_kwargs: summary_model(model_id=f"{name}-id"),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        await prepare_history_for_run_for_test(
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
    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == first_summary_text
    assert [run.run_id for run in persisted.runs] == ["run-2"]


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
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    execution_plan = ResolvedHistoryExecutionPlan(
        automatic_compaction=True,
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

    def _load_model(_config: object, _paths: object, name: str = "default", **_kwargs: object) -> ScriptedSummaryModel:
        if name == "fallback-model":
            message = "fallback provider SDK unavailable"
            raise RuntimeError(message)
        return summary_model(model_id=f"{name}-id")

    summary_mock = AsyncMock(
        return_value="primary summary",
    )
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch("mindroom.providers.registry.build_model_provider", side_effect=_load_model),
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock),
        patch("mindroom.history.compaction.logger.warning") as warning_mock,
    ):
        prepared = await prepare_history_for_run_for_test(
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
    assert summary_mock.await_args.kwargs["model"].identity.id == "summary-model-id"
    fallback_warnings = [
        call
        for call in warning_mock.call_args_list
        if call.args[0] == "Compaction fallback model failed to load; continuing without a fallback"
    ]
    assert len(fallback_warnings) == 1
    assert fallback_warnings[0].kwargs["fallback_model"] == "fallback-model"
    assert not any(isinstance(event, CompactionLifecycleFailure) for event in lifecycle.events)
    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "primary summary"


@pytest.mark.asyncio
async def test_prepare_history_for_run_reuses_completed_auto_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        return_value="all runs summary",
    )
    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=summary_mock,
    ):
        first_prepared = await prepare_history_for_run_for_test(
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
        persisted_before_second = storage.load_session("session-1")
        assert persisted_before_second is not None
        second_prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.text == "all runs summary"
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
    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )
    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.replay_plan is not None
    # Replay keeps the reserve, half of the 600-token window, free, so one of the 200-token runs fits.
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_replays_history_between_trigger_and_hard_budget_on_a_native_route(
    tmp_path: Path,
) -> None:
    """Native compaction owns history above the trigger, so replay is fitted only to the hard budget."""
    config, runtime_paths = _make_config(
        tmp_path,
        context_window=20_000,
        compaction=CompactionOverrideConfig(threshold_tokens=400),
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                f"run-{index}",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            )
            for index in range(1, 4)
        ],
    )
    seed_session(storage, session)

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        model=_native_model(),
        storage=storage,
        session=session,
    )

    # The three 200-token runs exceed the 400-token trigger but fit the hard budget, about half the 20,000-token window.
    assert prepared.compaction_decision.reason == "within_hard_budget"
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.estimated_tokens == 600
    assert prepared.replay_plan.num_history_runs is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_context_window_guard_skips_system_prompts(
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
                    Message(role="system", content="s" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="system", content="s" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
        ],
    )
    seed_session(storage, session)
    persisted = storage.load_session("session-1")
    assert persisted is not None

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=persisted,
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
async def test_prepare_history_for_run_compaction_failure_clears_force_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    flag_at_failure_notice: list[bool] = []

    @dataclass
    class _FlagAtFailure(RecordingCompactionLifecycle):
        async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
            stored = storage.load_session("session-1")
            assert stored is not None
            flag_at_failure_notice.append(read_scope_state(stored).force_compact_before_next_run)
            await super().complete_failure(event)

    install_model_providers(monkeypatch, {"default": summary_model()})
    with patch(
        "mindroom.history.compaction.generate_compaction_summary",
        new=AsyncMock(side_effect=RuntimeError("summary failed")),
    ):
        prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3", "run-4"]

    state = read_scope_state(persisted)
    assert state.force_compact_before_next_run is False
    assert summary_rows_of(storage, scope.key) == []

    assert prepared.compaction_reply_outcome == "failed"
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["before_summary", "during_summary"])
async def test_a_manual_compaction_the_conversation_outran_says_so_and_stays_requested(
    tmp_path: Path,
    race: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redaction landing before or during the summary call voids the chunk; the notice names it and the request stays."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1"), _completed_run("run-2")])
    session.metadata = with_force_compaction(session.metadata)
    seed_session(storage, session)
    lifecycle = RecordingCompactionLifecycle()

    def basis_after_a_redaction_cuts_a_run(*args: object, **kwargs: object) -> object:
        if race == "before_summary":
            delete_runs(storage, ["run-1"], session_id="session-1")
        return read_chunk_basis(*args, **kwargs)

    async def summary_while_a_redaction_cuts_a_run(**_kwargs: object) -> str:
        if race == "during_summary":
            delete_runs(storage, ["run-1"], session_id="session-1")
        return "stale summary"

    install_model_providers(monkeypatch, {"default": summary_model()})
    with (
        patch("mindroom.history.compaction.read_chunk_basis", new=basis_after_a_redaction_cuts_a_run),
        patch("mindroom.history.compaction.generate_compaction_summary", new=summary_while_a_redaction_cuts_a_run),
    ):
        prepared = await prepare_history_for_run_for_test(
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

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert read_scope_state(persisted).force_compact_before_next_run is True
    # The reply plans with what the conversation holds now, not the run the redaction removed.
    assert [run.run_id for run in session.runs] == ["run-2"]
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
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_reply_outcome == "none"
    assert prepared.replays_persisted_history is True


# Three runs of 2,000 + 2,000 characters estimate to 3,000 history tokens at chars / 4, under the trigger.
_REPORTED_USAGE_PLAN = ResolvedHistoryExecutionPlan(
    automatic_compaction=True,
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
    reasoning_tokens: int | None = None,
    plan_mode: Literal["configured", "limited"] = "configured",
    native_checkpoint: bool = False,
    non_prompt_tokens: int | None = 400,
) -> RunRecord:
    """Return a completed run whose answer carries the request size its provider reported and the model's stamp.

    The run publishes the replay plan ``plan_mode`` its request was sent with and, when not None, the
    ``non_prompt_tokens`` of system prompt and tools it was sized with, which a run records only when its request
    replayed the whole history; with ``native_checkpoint`` the answer holds a native compaction checkpoint.
    By default the answer's request starts the second after any summary a test writes. With ``reasoning_tokens``
    the answer also stores one encrypted reasoning item, as a Responses route records it, and reports that much
    reasoning.
    """
    answer = Message(
        role="assistant",
        content="a" * 2_000,
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=input_tokens, reasoning_tokens=reasoning_tokens or 0)),
        created_at=created_at,
    )
    if reasoning_tokens is not None:
        answer.provider_data = {
            "mindroom_response_output": [
                {"type": "reasoning", "id": "rs_answer", "summary": [], "encrypted_content": "gAAAA" * 1_000},
                {
                    "type": "message",
                    "id": "msg_answer",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "a" * 2_000, "annotations": []}],
                },
            ],
        }
    if native_checkpoint:
        checkpoint = {"route": "checkpoints", "items": [{"type": "compaction", "encrypted_content": "gAAAA"}]}
        answer.provider_data = {**(answer.provider_data or {}), NATIVE_CHECKPOINT_KEY: checkpoint}
    stamp_model(answer, ModelIdentity(id=model, provider="fake"))
    run = _completed_run(run_id, messages=[Message(role="user", content="u" * 2_000, images=images), answer])
    run.model = model
    run.model_provider = "fake"
    run.metadata = build_prepared_history_metadata_content(
        PreparedHistoryState(
            replay_plan=ResolvedReplayPlan(mode=plan_mode, estimated_tokens=600, add_history_to_context=True),
            prepared_context_tokens=1_000,
            non_prompt_tokens=non_prompt_tokens,
            native_history=None,
        ),
    )
    return run


async def _prepare_with_reported_usage(
    tmp_path: Path,
    latest: dict[str, object],
    *,
    history_settings: ResolvedHistorySettings = _ALL_HISTORY_SETTINGS,
    folded_run_ids: tuple[str, ...] = (),
    counter: TokenCounter | None = None,
    non_prompt_tokens: int = 400,
) -> tuple[PreparedScopeHistory, int, RecordingCompactionLifecycle]:
    """Prepare a reply after three runs; return it, the chars / 4 history estimate, and the compaction notices.

    ``latest`` configures the newest run, and a summary folding ``folded_run_ids`` is stored as written at
    ``_SUMMARY_WRITTEN_AT``. Of the plan's 1,000 static prompt tokens, ``non_prompt_tokens`` are today's system prompt
    and tools, while each run records the 400 its request was sized with. The answering model counts with
    ``counter``, a quarter token per character when it is None.
    """
    counter = counter or fake_token_counter()
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
        with patch("mindroom.session_storage.summary_rows.time", return_value=_SUMMARY_WRITTEN_AT):
            compact_runs(storage, session, list(folded_run_ids), "Earlier summary")
    estimate = estimate_prompt_visible_history_tokens(
        session=session,
        history_settings=history_settings,
        counter=fake_token_counter(),
    )
    resolved_inputs = replace(
        resolve_entity_preparation_inputs(
            history_settings=history_settings,
            config=config,
            runtime_paths=runtime_paths,
            static_prompt_tokens=0,
            active_model_name=entity_model_name(config, "test_agent"),
            active_context_window=None,
        ),
        execution_plan=_REPORTED_USAGE_PLAN,
    )
    lifecycle = RecordingCompactionLifecycle()
    with (
        patch(
            "mindroom.providers.registry.build_model_provider",
            return_value=summary_model(),
        ),
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(return_value="short summary"),
        ),
    ):
        prepared = await prepare_scope_history(
            compaction_lifecycle=lifecycle,
            # The model that answered the stored runs, so its reported request sizes apply.
            model=ScriptedModel(
                [],
                identity=ModelIdentity(id="fake-model", provider="fake"),
                token_counter=counter,
            ),
            resolved_inputs=resolved_inputs,
            non_prompt_tokens=non_prompt_tokens,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=ScopeSessionContext(
                scope=scope,
                storage=storage,
                session=session,
                session_id="session-1",
                storage_factory=lambda: storage,
            ),
            pipeline_timing=None,
            allow_native_compaction=True,
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
    outcome = completed_outcome(lifecycle)
    assert outcome.before_tokens == expected
    start = lifecycle.events[0]
    assert isinstance(start, CompactionLifecycleStart)
    # A compaction nobody requested reports itself as automatic from its first notice to its outcome.
    assert (start.mode, outcome.mode) == ("auto", "auto")


@pytest.mark.asyncio
async def test_the_reported_request_size_counts_its_answer_as_the_answering_route_sizes_it(tmp_path: Path) -> None:
    """The answer the reported request did not hold is sized as the model that answered replays it."""
    prepared, estimate, _lifecycle = await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 8_000},
        counter=fake_token_counter(replay=ReplayEstimate(1_200)),
    )

    # The route sizes the 500-token answer at 1,200 tokens; the whole history it sizes no higher than chars / 4.
    assert estimate < 4_000
    assert prepared.compaction_decision.current_history_tokens == 8_000 + 1_200 - 400


@pytest.mark.asyncio
async def test_history_preparation_counts_off_the_event_loop(tmp_path: Path) -> None:
    """Sizing the latest answer and the summary encodes them, which must never stall the replies sharing the loop."""
    loop_thread = threading.get_ident()
    counted_on: set[int] = set()

    def estimate(text: str) -> int:
        counted_on.add(threading.get_ident())
        return len(text) // 4

    await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 8_000},
        folded_run_ids=("run-0",),
        counter=TokenCounter(estimate=estimate),
    )

    assert counted_on
    assert loop_thread not in counted_on


@pytest.mark.asyncio
async def test_the_reported_request_size_decides_over_an_estimate_that_overcounts(tmp_path: Path) -> None:
    """One ratio per route cannot fit every conversation's content, so the provider's count of the history wins.

    An estimate that counts a conversation a third too high would otherwise compact it early.
    """
    one_per_character = TokenCounter(estimate=len)
    prepared, _estimate, lifecycle = await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 1_000},
        counter=one_per_character,
    )

    # The reported request plus the 2,000-character answer it did not hold, minus the system prompt and tools; the
    # estimate counts the three runs' 12,000 characters.
    assert prepared.compaction_decision.current_history_tokens == 1_000 + 2_000 - 400
    assert prepared.compaction_decision.reason == "under_trigger"
    assert lifecycle.events == []


def _finalize_with_history_budget(
    tmp_path: Path,
    prepared: PreparedScopeHistory,
    history_budget: int,
    policy: HistoryPolicy | None = None,
) -> PreparedHistoryState:
    """Finalize ``prepared`` under the agent's own settings, with a static prompt that leaves ``history_budget``.

    ``policy`` replaces the settings' replay policy when given.
    """
    config, _paths = _make_config(tmp_path, compaction=CompactionOverrideConfig(enabled=True), context_window=64_000)
    # The agent's own settings, which bound replay by the context window.
    history_settings = config.resolve_scope("test_agent").history_settings
    if policy is not None:
        history_settings = replace(history_settings, policy=policy)
    resolved_inputs = replace(prepared.resolved_inputs, history_settings=history_settings, active_context_window=64_000)
    unloaded = resolve_history_execution_plan(
        config=config,
        history_settings=history_settings,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=64_000,
        static_prompt_tokens=0,
    )
    assert unloaded.hard_replay_budget_tokens is not None
    return finalize_history_preparation(
        prepared_scope_history=replace(prepared, resolved_inputs=resolved_inputs),
        config=config,
        static_prompt_tokens=unloaded.hard_replay_budget_tokens - history_budget,
        pipeline_timing=None,
    )


@pytest.mark.asyncio
async def test_the_replay_plan_sizes_history_as_the_compaction_check_did(tmp_path: Path) -> None:
    """A history the reported size kept below the trigger replays whole, not trimmed by an estimate that overcounts."""
    one_per_character = TokenCounter(estimate=len)
    prepared, _estimate, _lifecycle = await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 1_000},
        counter=one_per_character,
    )
    reported = 1_000 + 2_000 - 400
    assert prepared.compaction_decision.current_history_tokens == reported

    # A budget between the reported size and the 12,000-character estimate.
    state = _finalize_with_history_budget(tmp_path, prepared, (reported + 12_000) // 2)

    assert state.replay_plan is not None
    assert state.replay_plan.mode != "limited"
    assert state.replay_plan.estimated_tokens == reported


@pytest.mark.asyncio
async def test_the_replay_plan_trims_by_the_reported_size_over_the_final_budget(tmp_path: Path) -> None:
    """A reported size over the final budget trims history even where the estimate alone would fit it.

    The final prompt can leave less room than the check had, and nothing compacts when automatic compaction is off.
    """
    prepared, estimate, _lifecycle = await _prepare_with_reported_usage(tmp_path, {"input_tokens": 3_500})
    reported = 3_500 + 500 - 400
    assert prepared.compaction_decision.current_history_tokens == reported
    assert estimate < 3_200 < reported

    state = _finalize_with_history_budget(tmp_path, prepared, 3_200)

    assert state.replay_plan is not None
    assert state.replay_plan.mode == "limited"
    assert state.replay_plan.num_history_runs == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("today", "taken_off"),
    [
        # Minus today's 7,000, history would show 1,500 tokens, under the 4,000-token trigger, as does the estimate.
        pytest.param(7_000, 400, id="grown"),
        pytest.param(100, 100, id="shrunk"),
    ],
)
async def test_the_reported_request_size_takes_off_the_smaller_system_prompt(
    tmp_path: Path,
    today: int,
    taken_off: int,
) -> None:
    """The reported request carried the 400-token system prompt and tools its run recorded, not today's.

    A prompt grown since, such as after more tools or longer context files, counts in today's static estimate, not as
    history that shrank. A prompt that shrank stays counted as history until the next run records it, because taking
    off an estimate's margin with it could undercount the history.
    """
    prepared, estimate, _lifecycle = await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 8_000},
        non_prompt_tokens=today,
    )

    assert estimate < 4_000
    assert prepared.compaction_decision.current_history_tokens == 8_000 + 500 - taken_off
    assert prepared.compaction_decision.reason == "history_exceeds_hard_budget"


@pytest.mark.asyncio
async def test_trimming_by_the_reported_size_turns_native_compaction_off(tmp_path: Path) -> None:
    """A provider's checkpoint of a trimmed request would replace history it never held, so trimming turns it off."""
    prepared, _estimate, _lifecycle = await _prepare_with_reported_usage(tmp_path, {"input_tokens": 3_500})
    native = NativeHistory(
        native_compaction=NativeCompactionReplay(route="checkpoints", threshold=None),
        portable_replay=True,
    )

    state = _finalize_with_history_budget(tmp_path, replace(prepared, native_history=native), 3_200)

    assert state.replay_plan is not None
    assert state.replay_plan.mode == "limited"
    assert state.native_history is not None
    assert state.native_history.native_compaction is None


@pytest.mark.asyncio
async def test_trimming_never_widens_the_budget_by_an_estimate_that_overcounts(tmp_path: Path) -> None:
    """An estimate's ratio to the reported size fits the whole history, not the newest runs trimming keeps.

    Widening the budget by it could keep newer runs that overcount less, past the real budget.
    """
    # Each run's 4,000 characters estimate at 2,000 tokens; the answer the request did not hold at 1,000.
    prepared, _estimate, _lifecycle = await _prepare_with_reported_usage(
        tmp_path,
        {"input_tokens": 2_000},
        counter=TokenCounter(estimate=lambda text: len(text) // 2),
    )
    assert prepared.compaction_decision.current_history_tokens == 2_000 + 1_000 - 400

    state = _finalize_with_history_budget(tmp_path, prepared, 2_500)

    assert state.replay_plan is not None
    assert state.replay_plan.mode == "limited"
    assert state.replay_plan.estimated_tokens <= 2_500


@pytest.mark.asyncio
async def test_the_reported_request_size_counts_the_reasoning_its_answer_replays(tmp_path: Path) -> None:
    """A model that renders earlier turns' reasoning holds the answer's reasoning, which its own request did not."""
    sized = {}
    for reasoning_tokens in (1_000, 3_000):
        root = tmp_path / str(reasoning_tokens)
        root.mkdir()
        prepared, _estimate, _lifecycle = await _prepare_with_reported_usage(
            root,
            {"input_tokens": 8_000, "reasoning_tokens": reasoning_tokens},
            counter=responses_adapter(root, store=False).token_counter,
        )
        sized[reasoning_tokens] = prepared.compaction_decision.current_history_tokens

    # Both answers size above their 500-token chars / 4 floor, so the difference is the extra reasoning alone.
    assert sized[3_000] - sized[1_000] == 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest", "setup"),
    [
        pytest.param({}, {}, id="no-reported-usage"),
        # A request whose replay plan cut the history reported the size of the part it sent.
        pytest.param({"input_tokens": 8_000, "plan_mode": "limited"}, {}, id="partial-replay"),
        # A request under an authored replay limit, since removed, records nothing to take off its size.
        pytest.param({"input_tokens": 8_000, "non_prompt_tokens": None}, {}, id="authored-limit-since-removed"),
        # A native checkpoint replays differently on each route, and its response was billed for the transcript it replaced.
        pytest.param({"input_tokens": 8_000, "native_checkpoint": True}, {}, id="native-checkpoint"),
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
