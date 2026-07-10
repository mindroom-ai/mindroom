"""Tests for history scope-state and seen-event-id storage."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.models import CompactionOverrideConfig
from mindroom.constants import (
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_REPLAY_STATE_INTERRUPTED,
    MINDROOM_REPLAY_STATE_METADATA_KEY,
    MINDROOM_RESTART_RECOVERY_PENDING_METADATA_KEY,
)
from mindroom.history.storage import (
    pending_restart_recovery_run_ids,
    read_scope_seen_event_ids,
    read_scope_state,
    record_compaction_chunk,
    scope_has_recovered_interrupted_event,
    seen_event_ids_for_runs,
    set_force_compaction_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import (
    HistoryScope,
    HistoryScopeState,
)
from tests.conftest import (
    FakeModel,
    prepare_history_for_run_for_test,
)
from tests.history_helpers import (  # noqa: F401
    _agent,
    _close_test_storages,
    _completed_run,
    _make_config,
    _session,
    _team_session,
)


def test_scope_seen_event_ids_survive_scope_state_writes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    assert update_scope_seen_event_ids(session, scope, ["event-1"]) is True
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))

    assert read_scope_seen_event_ids(session, scope) == {"event-1"}


def test_set_force_compaction_state_updates_only_force_flag(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session("session-1")
    state = HistoryScopeState(
        last_summary_model="summary-model",
        last_compacted_run_count=3,
    )

    forced_state = set_force_compaction_state(session, scope, state, force=True)

    assert forced_state == HistoryScopeState(
        last_summary_model="summary-model",
        last_compacted_run_count=3,
        force_compact_before_next_run=True,
    )
    assert read_scope_state(session, scope) == forced_state

    cleared_state = set_force_compaction_state(session, scope, forced_state, force=False)

    assert cleared_state == HistoryScopeState(
        last_summary_model="summary-model",
        last_compacted_run_count=3,
        force_compact_before_next_run=False,
    )
    assert read_scope_state(session, scope) == cleared_state


def test_scope_seen_event_ids_include_persisted_response_event_ids(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    run = _completed_run("run-1")
    run.metadata = {
        "matrix_seen_event_ids": ["question-1"],
        "matrix_response_event_id": "answer-1",
    }
    session = _session("session-1", runs=[run])

    assert read_scope_seen_event_ids(session, scope) == {"question-1", "answer-1"}


def test_pending_restart_recovery_run_ids_only_keeps_latest_flagged_replay() -> None:
    """Generic failures retain protection; repeated restart cancels replace it."""
    first = _completed_run("first")
    first.metadata = {
        MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED,
        MINDROOM_RESTART_RECOVERY_PENDING_METADATA_KEY: True,
    }
    second = _completed_run("second")
    second.metadata = dict(first.metadata)
    provider_error = _completed_run("provider-error")
    provider_error.metadata = {MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED}

    assert pending_restart_recovery_run_ids([first, second]) == {"second"}
    assert pending_restart_recovery_run_ids([first, provider_error]) == {"first"}


@pytest.mark.parametrize("excluded_status", [RunStatus.cancelled, RunStatus.error, RunStatus.paused])
def test_scope_seen_event_ids_skip_runs_agno_history_drops(tmp_path: Path, excluded_status: RunStatus) -> None:
    """Events on cancelled/error/paused runs stay unseen so the next turn re-reads them."""
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    completed = _completed_run("run-1")
    completed.metadata = {"matrix_seen_event_ids": ["answered-question"]}
    dropped = _completed_run("run-2")
    dropped.status = excluded_status
    dropped.metadata = {
        "matrix_seen_event_ids": ["orphaned-question"],
        "matrix_response_event_id": "orphaned-placeholder",
    }
    session = _session("session-1", runs=[completed, dropped])

    assert read_scope_seen_event_ids(session, scope) == {"answered-question"}


@pytest.mark.parametrize("excluded_status", [RunStatus.cancelled, RunStatus.error, RunStatus.paused])
def test_seen_event_ids_for_runs_skip_runs_agno_history_drops(tmp_path: Path, excluded_status: RunStatus) -> None:
    """Preserved-state writers must never record excluded-status runs' event ids as seen."""
    _make_config(tmp_path)
    completed = _completed_run("run-1")
    completed.metadata = {"matrix_seen_event_ids": ["answered-question"]}
    dropped = _completed_run("run-2")
    dropped.status = excluded_status
    dropped.metadata = {"matrix_seen_event_ids": ["orphaned-question"]}

    assert seen_event_ids_for_runs([completed, dropped]) == {"answered-question"}


def test_seen_event_ids_skip_child_runs_agno_history_drops(tmp_path: Path) -> None:
    """Child-run metadata cannot consume events that Agno omits from top-level model history."""
    _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    completed = _completed_run("run-1")
    completed.metadata = {"matrix_seen_event_ids": ["answered-question"]}
    child = _completed_run("run-2")
    child.parent_run_id = completed.run_id
    child.metadata = {"matrix_seen_event_ids": ["orphaned-question"]}
    session = _session("session-1", runs=[completed, child])

    assert read_scope_seen_event_ids(session, scope) == {"answered-question"}
    assert seen_event_ids_for_runs([completed, child]) == {"answered-question"}


def test_sync_restart_recovery_ignores_interrupted_replay_until_later_visible_run(tmp_path: Path) -> None:
    """The synthetic interrupted replay must not suppress its own live retry."""
    _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    interrupted = _completed_run("interrupted")
    interrupted.metadata = {
        "matrix_seen_event_ids": ["source-event"],
        MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED,
    }
    session = _session("session-1", runs=[interrupted])

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is False

    prior_continuation = _completed_run("prior-continuation")
    prior_continuation.metadata = {"matrix_seen_event_ids": ["source-event"]}
    session.runs = [prior_continuation, interrupted]

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is False

    child = _completed_run("child")
    child.parent_run_id = interrupted.run_id
    child.metadata = {"matrix_seen_event_ids": ["newer-child-event"]}
    session.runs = [interrupted, child]

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is False

    recovered = _completed_run("recovered")
    recovered.metadata = {"matrix_seen_event_ids": ["newer-event"]}
    session.runs = [interrupted, recovered]

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is True

    later_failed_turn = _completed_run("later-failed-turn")
    later_failed_turn.metadata = {
        "matrix_seen_event_ids": ["newer-event"],
        MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED,
    }
    session.runs = [interrupted, later_failed_turn]

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is True


def test_sync_restart_recovery_includes_compaction_preserved_seen_events(tmp_path: Path) -> None:
    """A recovered run stays authoritative after compaction removes its run row."""
    _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session("session-1")
    update_scope_seen_event_ids(session, scope, ["source-event"])

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is True

    interrupted = _completed_run("interrupted")
    interrupted.metadata = {
        "matrix_seen_event_ids": ["source-event"],
        MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED,
    }
    session.runs = [interrupted]

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is False


def test_sync_restart_recovery_prefers_live_interrupted_team_replay_over_preserved_seen_state(
    tmp_path: Path,
) -> None:
    """Pre-delivery team seen state cannot suppress the interrupted turn's own retry."""
    _make_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="team-123")
    interrupted = TeamRunOutput(
        run_id="interrupted",
        team_id="team-123",
        status=RunStatus.completed,
        metadata={
            "matrix_seen_event_ids": ["source-event"],
            MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED,
        },
    )
    session = _team_session("session-1", team_id="team-123", runs=[interrupted])
    update_scope_seen_event_ids(session, scope, ["source-event"])

    assert scope_has_recovered_interrupted_event(session, scope, "source-event") is False


def test_scope_states_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    write_scope_state(session, agent_scope, HistoryScopeState(force_compact_before_next_run=True))
    write_scope_state(session, team_scope, HistoryScopeState(last_summary_model="summary-model"))

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is True
    assert read_scope_state(session, agent_scope).last_summary_model is None
    assert read_scope_state(session, team_scope).force_compact_before_next_run is False
    assert read_scope_state(session, team_scope).last_summary_model == "summary-model"


def test_legacy_scope_state_metadata_is_ignored(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session(
        "session-1",
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "version": 1,
                "force_compact_before_next_run": True,
            },
        },
    )

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is False

    write_scope_state(session, agent_scope, HistoryScopeState(force_compact_before_next_run=True))

    assert session.metadata == {
        MINDROOM_COMPACTION_METADATA_KEY: {
            "version": 2,
            "states": {
                agent_scope.key: {
                    "force_compact_before_next_run": True,
                },
            },
        },
    }


def test_scope_seen_event_ids_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session(
        "session-1",
        runs=[
            RunOutput(
                run_id="agent-run",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["agent-event"]},
            ),
            TeamRunOutput(
                run_id="team-run",
                team_id="team-123",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["team-event"]},
            ),
        ],
    )
    update_scope_seen_event_ids(session, team_scope, ["preserved-team-event"])

    assert read_scope_seen_event_ids(session, agent_scope) == {"agent-event"}
    assert read_scope_seen_event_ids(session, team_scope) == {"team-event", "preserved-team-event"}


def test_compaction_progress_preserves_newer_seen_event_ids(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    persisted_session = _session("session-1")
    working_session = _session("session-1")
    latest_session = _session("session-1")
    update_scope_seen_event_ids(working_session, scope, ["compacted-event"])
    update_scope_seen_event_ids(latest_session, scope, ["newer-event"])
    storage.upsert_session(latest_session)

    record_compaction_chunk(
        storage=storage,
        persisted_session=persisted_session,
        working_session=working_session,
        scope=scope,
        compacted_run_ids=(),
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert read_scope_seen_event_ids(persisted, scope) == {"compacted-event", "newer-event"}


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_preserves_seen_event_ids(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            RunOutput(
                run_id="run-1",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-1", "event-2"],
                    "matrix_response_event_id": "response-1",
                },
            ),
            RunOutput(
                run_id="run-2",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-3"],
                    "matrix_response_event_id": "response-2",
                },
            ),
            RunOutput(
                run_id="run-3",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-4"],
                    "matrix_response_event_id": "response-3",
                },
            ),
            RunOutput(
                run_id="run-4",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-5"],
                    "matrix_response_event_id": "response-4",
                },
            ),
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
            new=AsyncMock(
                return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
            ),
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
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert read_scope_seen_event_ids(persisted, scope) == {
        "event-1",
        "event-2",
        "event-3",
        "event-4",
        "event-5",
        "response-1",
        "response-2",
        "response-3",
        "response-4",
    }


@pytest.mark.parametrize(
    ("restart_recovery_pending", "expected_run_ids"),
    [(True, ["interrupted"]), (False, [])],
)
@pytest.mark.asyncio
async def test_compaction_only_keeps_pending_restart_recovery_replay(
    tmp_path: Path,
    restart_recovery_pending: bool,
    expected_run_ids: list[str],
) -> None:
    """Compaction protects only provenance needed by a pending sync-restart retry."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    completed = _completed_run("completed")
    completed.metadata = {"matrix_seen_event_ids": ["older-event"]}
    interrupted = _completed_run("interrupted")
    interrupted.metadata = {
        "matrix_seen_event_ids": ["source-event"],
        MINDROOM_REPLAY_STATE_METADATA_KEY: MINDROOM_REPLAY_STATE_INTERRUPTED,
    }
    if restart_recovery_pending:
        interrupted.metadata[MINDROOM_RESTART_RECOVERY_PENDING_METADATA_KEY] = True
    session = _session("session-1", runs=[completed, interrupted])
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
            new=AsyncMock(
                return_value=SessionSummary(summary="older summary", updated_at=datetime.now(UTC)),
            ),
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
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs or []] == expected_run_ids
    if restart_recovery_pending:
        assert scope_has_recovered_interrupted_event(persisted, scope, "source-event") is False
