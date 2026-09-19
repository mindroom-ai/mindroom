"""Compaction preserves usage facts without preserving conversation content."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from sqlalchemy.exc import IntegrityError

import mindroom.usage_stats_storage as reader
from mindroom.agent_storage import create_state_storage, get_agent_session, save_runs
from mindroom.agents import remove_run_by_event_id
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SEEN_EVENT_IDS_METADATA_KEY, resolve_runtime_paths
from mindroom.history.storage import prune_reintroduced_runs, read_scope_state, record_compaction_chunk
from mindroom.history.types import HistoryScope
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage, collect_self_usage
from mindroom.usage_stats_storage import UsageSessionRow, UsageStorageSource, iter_usage_storage_rows
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb


def _setup(tmp_path: Path) -> tuple[BaseDb, UsageStorageSource]:
    storage = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    session = AgentSession(
        session_id="session",
        agent_id="code",
        user_id="@owner:example.test",
        session_data={"session_metrics": {"total_tokens": 30}},
        runs=[
            RunOutput(
                run_id=f"run-{index}",
                agent_id="code",
                created_at=1_700_000_000 + index * 86400,
                model="test-model",
                model_provider="test-provider",
                content="private response",
                metrics=RunMetrics(input_tokens=6, output_tokens=4, total_tokens=10),
                metadata={"requester_id": requester, MATRIX_EVENT_ID_METADATA_KEY: f"$event-{index}"},
            )
            for index, requester in enumerate(["@alice:example.test", "@bob:example.test", "@alice:example.test"])
        ],
    )
    seed_session(storage, session)
    source = UsageStorageSource(
        path=tmp_path / "sessions" / "code.db",
        path_label="code.db",
        scope="shared_agent",
        expected_session_table="code_sessions",
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code"}),
        allowed_team_ids=frozenset(),
        requester_isolated=False,
    )
    return storage, source


def _compact(storage: BaseDb, run_ids: list[str]) -> None:
    session = get_agent_session(storage, "session")
    assert session is not None
    working = deepcopy(session)
    working.summary = SessionSummary(summary="short summary")
    working.runs = [run for run in working.runs or [] if run.run_id not in run_ids]
    record_compaction_chunk(
        storage=storage,
        persisted_session=session,
        working_session=working,
        scope=HistoryScope(kind="agent", scope_id="code"),
        compacted_run_ids=run_ids,
    )


def _row(source: UsageStorageSource) -> UsageSessionRow:
    rows = list(iter_usage_storage_rows(source, mode="both"))
    assert len(rows) == 1
    assert isinstance(rows[0], UsageSessionRow)
    return rows[0]


def test_compaction_keeps_stored_usage_facts(tmp_path: Path) -> None:
    """Dropping transcript rows must not shrink dated, requester-attributed counters."""
    storage, source = _setup(tmp_path)
    try:
        before = _row(source)
        _compact(storage, ["run-0", "run-1"])
        after = _row(source)
        assert {run.run_id: run for run in after.runs} == {run.run_id: run for run in before.runs}
        assert after.session_metrics == {"total_tokens": 30}
        assert after.runs_available
        assert len(get_agent_session(storage, "session").runs) == 1
    finally:
        storage.close()


def test_archive_replay_and_live_collision_are_deduplicated(tmp_path: Path) -> None:
    """A retry or stale upsert must never double-count one run ID."""
    storage, source = _setup(tmp_path)
    try:
        original = get_agent_session(storage, "session").runs[0]
        _compact(storage, ["run-0"])
        storage.upsert_run(original, "session")
        assert len(_row(source).runs) == 3
        _compact(storage, ["run-0"])
        assert len(_row(source).runs) == 3
    finally:
        storage.close()


def test_redaction_of_compacted_event_removes_causal_suffix(tmp_path: Path) -> None:
    """A compacted source still owns erasure of later live and archived runs."""
    storage, source = _setup(tmp_path)
    try:
        _compact(storage, ["run-1"])
        assert remove_run_by_event_id(storage, "session", "$event-1", remove_following_runs=True)
        assert [run.run_id for run in _row(source).runs] == ["run-0"]
    finally:
        storage.close()


def test_archived_order_survives_empty_live_history_and_resurrection(tmp_path: Path) -> None:
    """New runs follow archives; resurrected IDs keep their earlier causal position."""
    storage, source = _setup(tmp_path)
    try:
        original = get_agent_session(storage, "session").runs[0]
        _compact(storage, ["run-0", "run-1", "run-2"])
        new = deepcopy(original)
        new.run_id = "new-run"
        new.metadata = {MATRIX_EVENT_ID_METADATA_KEY: "$new-event"}
        storage.upsert_run(new, "session")
        storage.upsert_run(original, "session")
        assert remove_run_by_event_id(storage, "session", "$event-1", remove_following_runs=True)
        assert [run.run_id for run in _row(source).runs] == ["run-0"]
    finally:
        storage.close()


def test_explicit_run_and_session_deletion_erase_archived_usage(tmp_path: Path) -> None:
    """Compaction preservation must not bypass explicit erasure APIs."""
    storage, source = _setup(tmp_path)
    try:
        _compact(storage, ["run-0", "run-1", "run-2"])
        storage.delete_runs(["run-0"])
        assert {run.run_id for run in _row(source).runs} == {"run-1", "run-2"}
        assert storage.delete_session("session")
        assert list(iter_usage_storage_rows(source, mode="both")) == []

        with sqlite3.connect(source.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM code_sessions_usage").fetchone()[0] == 0
    finally:
        storage.close()


def test_startup_prune_preserves_archive(tmp_path: Path) -> None:
    """Startup cleanup of stale writes removes transcripts but keeps archived counters."""
    storage, source = _setup(tmp_path)
    try:
        original = get_agent_session(storage, "session").runs[0]
        _compact(storage, ["run-0"])
        storage.upsert_run(original, "session")
        session = get_agent_session(storage, "session")
        assert prune_reintroduced_runs(
            storage,
            session,
            read_scope_state(session, HistoryScope(kind="agent", scope_id="code")),
        )
        assert len(session.runs) == 2
        assert len(_row(source).runs) == 3
    finally:
        storage.close()


def test_archive_contains_only_allowed_scalar_facts(tmp_path: Path) -> None:
    """Unrelated metadata and malformed identity objects cannot smuggle content into archives."""
    storage, source = _setup(tmp_path)
    try:
        session = get_agent_session(storage, "session")
        run = deepcopy(session.runs[0])
        run.metadata.update(
            {
                "secret": "private metadata",
                MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$seen", {"text": "private nested data"}],
            },
        )
        save_runs(storage, session, [run])
        _compact(storage, ["run-0"])
        with sqlite3.connect(source.path) as connection:
            payload = connection.execute("SELECT run_data FROM code_sessions_usage").fetchone()[0]
        assert "private" not in payload
        raw = json.loads(payload)
        assert set(raw) == {"run_id", "team_id", "user_id", "model", "model_provider", "metadata", "metrics"}
        assert raw["metadata"][MATRIX_SEEN_EVENT_IDS_METADATA_KEY] == ["$seen"]
    finally:
        storage.close()


def test_archive_failure_precedes_tombstone_and_removal(tmp_path: Path) -> None:
    """A failed preservation write leaves summary and live runs untouched."""
    storage, source = _setup(tmp_path)
    try:
        _compact(storage, ["run-0"])
        with sqlite3.connect(source.path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_archive BEFORE INSERT ON code_sessions_usage BEGIN SELECT RAISE(ABORT, 'archive unavailable'); END",
            )
        with pytest.raises(IntegrityError, match="archive unavailable"):
            _compact(storage, ["run-1"])
        session = get_agent_session(storage, "session")
        assert [run.run_id for run in session.runs] == ["run-1", "run-2"]
        assert read_scope_state(session, HistoryScope(kind="agent", scope_id="code")).compacted_run_ids == ("run-0",)
        assert len(_row(source).runs) == 3
    finally:
        storage.close()


def test_failed_removal_keeps_usage_and_can_resume_pruning(tmp_path: Path) -> None:
    """A crash after archiving and tombstoning remains deduplicated and recoverable."""
    storage, source = _setup(tmp_path)
    try:
        with sqlite3.connect(source.path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_delete BEFORE DELETE ON code_sessions_runs BEGIN SELECT RAISE(ABORT, 'delete unavailable'); END",
            )
        with pytest.raises(IntegrityError, match="delete unavailable"):
            _compact(storage, ["run-0"])
        assert len(_row(source).runs) == 3
        with sqlite3.connect(source.path) as connection:
            connection.execute("DROP TRIGGER fail_delete")
        session = get_agent_session(storage, "session")
        assert prune_reintroduced_runs(
            storage,
            session,
            read_scope_state(session, HistoryScope(kind="agent", scope_id="code")),
        )
        assert len(_row(source).runs) == 3
    finally:
        storage.close()


def test_malformed_archive_reports_missing_detail_without_losing_cumulative_metrics(tmp_path: Path) -> None:
    """Corrupt detail cannot masquerade as complete usage or change cumulative counters."""
    storage, source = _setup(tmp_path)
    try:
        _compact(storage, ["run-0"])
        with sqlite3.connect(source.path) as connection:
            connection.execute("UPDATE code_sessions_usage SET run_data = 'invalid json'")
        row = _row(source)
        assert not row.runs_available
        assert row.session_metrics_available
        assert row.session_metrics == {"total_tokens": 30}
    finally:
        storage.close()


def test_reports_keep_daily_model_requester_and_self_totals(tmp_path: Path) -> None:
    """Exported dates and shared-session requester isolation survive compaction."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    storage, _ = _setup(paths.storage_root / "agents" / "code")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.test",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    try:
        before = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True)
        own_before = collect_self_usage(
            agent_name="code",
            requester_id="@alice:example.test",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            include_daily=True,
        )
        assert before.totals.total_tokens == 30
        assert own_before.totals.total_tokens == 20
        assert {row.user_id: row.totals.total_tokens for row in before.user_breakdown} == {
            "@alice:example.test": 20,
            "@bob:example.test": 10,
        }
        _compact(storage, ["run-0", "run-1", "run-2"])
        after = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True)
        own_after = collect_self_usage(
            agent_name="code",
            requester_id="@alice:example.test",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            include_daily=True,
        )
        for attribute in ("totals", "daily_breakdown", "model_breakdown", "user_breakdown"):
            assert getattr(after, attribute) == getattr(before, attribute)
            assert getattr(own_after, attribute) == getattr(own_before, attribute)
    finally:
        storage.close()


@pytest.mark.parametrize(
    ("metadata", "event_id", "include_seen", "matches"),
    [
        ({"matrix_source_event_ids": ["$source"]}, "$source", False, True),
        ({"matrix_turn_discovery_event_ids": ["$discovery"]}, "$discovery", False, True),
        ({"matrix_seen_event_ids": ["$seen"]}, "$seen", False, False),
        ({"matrix_seen_event_ids": ["$seen"]}, "$seen", True, True),
        ({"matrix_source_event_revisions": {"$source": [1, "$revision"]}}, "$revision", False, False),
        ({"matrix_source_event_revisions": {"$source": [1, "$revision"]}}, "$revision", True, True),
    ],
)
def test_archived_event_matching_preserves_erasure_flags(
    tmp_path: Path,
    metadata: dict[str, object],
    event_id: str,
    include_seen: bool,
    matches: bool,
) -> None:
    """Source, discovery, consumed-history and revision IDs retain existing deletion semantics."""
    storage, source = _setup(tmp_path)
    try:
        session = get_agent_session(storage, "session")
        run = deepcopy(session.runs[1])
        run.metadata.update(metadata)
        save_runs(storage, session, [run])
        _compact(storage, ["run-0", "run-1", "run-2"])
        assert remove_run_by_event_id(storage, "session", event_id, include_seen_event_ids=include_seen) is matches
        assert len(_row(source).runs) == (2 if matches else 3)
    finally:
        storage.close()


def test_archive_keeps_model_details_and_column_timestamp(tmp_path: Path) -> None:
    """Authoritative stored timestamps and selected per-model counters survive sanitization."""
    storage, source = _setup(tmp_path)
    try:
        session = get_agent_session(storage, "session")
        run = deepcopy(session.runs[0])
        run.created_at = 42
        run.metrics = RunMetrics(
            input_tokens=6,
            output_tokens=4,
            total_tokens=10,
            details={
                "model": [
                    ModelMetrics(
                        id="detailed-model",
                        provider="detailed-provider",
                        input_tokens=6,
                        output_tokens=4,
                        total_tokens=10,
                    ),
                ],
            },
        )
        save_runs(storage, session, [run])
        before = next(run for run in _row(source).runs if run.run_id == "run-0")
        assert before.created_at == 1_700_000_000
        assert before.model_metrics[0].model == "detailed-model"
        _compact(storage, ["run-0"])
        assert next(run for run in _row(source).runs if run.run_id == "run-0") == before
    finally:
        storage.close()


def test_usage_scan_has_consistent_snapshot_during_first_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First archive creation between schema discovery and row reads cannot lose run facts."""
    storage, source = _setup(tmp_path)
    try:
        # WAL allows the forced concurrent writer to commit while this scan holds its snapshot.
        with sqlite3.connect(source.path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
        load_runs = reader._persisted_runs
        compacted = False

        def concurrent_compaction(
            connection: sqlite3.Connection,
            runs_table: str,
            session_id: object,
            *,
            read_runs: bool = True,
            archive_table: str | None = None,
        ) -> reader._PersistedRuns:
            nonlocal compacted
            if not compacted:
                compacted = True
                _compact(storage, ["run-0", "run-1", "run-2"])
            return load_runs(connection, runs_table, session_id, read_runs=read_runs, archive_table=archive_table)

        monkeypatch.setattr(reader, "_persisted_runs", concurrent_compaction)
        assert len(_row(source).runs) == 3
        assert len(_row(source).runs) == 3
    finally:
        storage.close()


def test_compaction_usage_scope_stays_top_level(tmp_path: Path) -> None:
    """Nested member counters are not promoted into top-level archived usage."""
    storage, source = _setup(tmp_path)
    try:
        session = get_agent_session(storage, "session")
        child = deepcopy(session.runs[1])
        child.parent_run_id = "run-0"
        child.metrics = RunMetrics(total_tokens=999)
        save_runs(storage, session, [child])
        _compact(storage, ["run-0", "run-1"])
        assert {run.run_id for run in _row(source).runs} == {"run-0", "run-2"}
        assert sum(run.metrics["total_tokens"] for run in _row(source).runs) == 20
    finally:
        storage.close()


def test_live_metrics_win_collision_and_recompaction(tmp_path: Path) -> None:
    """Current stored facts win a replay collision and remain after the next compaction."""
    storage, source = _setup(tmp_path)
    try:
        updated = deepcopy(get_agent_session(storage, "session").runs[0])
        _compact(storage, ["run-0"])
        updated.metrics = RunMetrics(total_tokens=12)
        storage.upsert_run(updated, "session")
        assert next(run for run in _row(source).runs if run.run_id == "run-0").metrics["total_tokens"] == 12
        _compact(storage, ["run-0"])
        assert next(run for run in _row(source).runs if run.run_id == "run-0").metrics["total_tokens"] == 12
    finally:
        storage.close()


@pytest.mark.parametrize("keep_modern", [False, True])
def test_legacy_compaction_preserves_raw_facts_and_causal_order(tmp_path: Path, keep_modern: bool) -> None:
    """Legacy list order and absent timestamps survive compaction beside newer row indexes."""
    storage, source = _setup(tmp_path)
    try:
        with sqlite3.connect(source.path) as connection:
            payloads = [
                json.loads(row[0])
                for row in connection.execute("SELECT run_data FROM code_sessions_runs ORDER BY run_index")
            ]
            payloads[0].pop("created_at", None)
            payloads[1]["created_at"] = 0
            legacy = payloads[:2] if keep_modern else payloads
            connection.execute("ALTER TABLE code_sessions ADD COLUMN runs JSON")
            connection.execute("UPDATE code_sessions SET runs = ?", (json.dumps(legacy),))
            connection.execute(
                "DELETE FROM code_sessions_runs WHERE run_id != 'run-2'"
                if keep_modern
                else "DELETE FROM code_sessions_runs",
            )
        storage.close()
        storage = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
        before = {run.run_id: run for run in _row(source).runs}
        assert before["run-0"].created_at is None
        assert before["run-1"].created_at == 0
        _compact(storage, ["run-1"])
        assert {run.run_id: run for run in _row(source).runs} == before
        assert remove_run_by_event_id(storage, "session", "$event-1", remove_following_runs=True)
        assert [run.run_id for run in _row(source).runs] == ["run-0"]
        _compact(storage, ["run-0"])
        assert _row(source).runs[0].created_at is None
    finally:
        storage.close()


def test_empty_run_metrics_do_not_block_compaction(tmp_path: Path) -> None:
    """A canceled run with null metrics retains missing usage without inventing zero counters."""
    storage, source = _setup(tmp_path)
    try:
        with sqlite3.connect(source.path) as connection:
            raw = json.loads(
                connection.execute("SELECT run_data FROM code_sessions_runs WHERE run_id = 'run-1'").fetchone()[0],
            )
            raw["metrics"] = None
            connection.execute("UPDATE code_sessions_runs SET run_data = ? WHERE run_id = 'run-1'", (json.dumps(raw),))
        before = _row(source)
        assert before.runs_available
        assert next(run for run in before.runs if run.run_id == "run-1").metrics == {}
        _compact(storage, ["run-0"])
        _compact(storage, ["run-1", "run-2"])
        assert {run.run_id: run for run in _row(source).runs} == {run.run_id: run for run in before.runs}
    finally:
        storage.close()


def test_nested_identity_keeps_order_across_intervening_compaction(tmp_path: Path) -> None:
    """A later nested event must not erase a preceding compacted top-level run."""
    storage, source = _setup(tmp_path)
    try:
        runs = get_agent_session(storage, "session").runs
        child = deepcopy(runs[0])
        child.run_id = "child"
        child.parent_run_id = "run-0"
        child.metrics = None
        child.metadata = {MATRIX_EVENT_ID_METADATA_KEY: "$child"}
        storage.delete_runs(["run-2"])
        storage.upsert_run(child, "session")
        storage.upsert_run(runs[2], "session")
        _compact(storage, ["run-1"])
        assert remove_run_by_event_id(storage, "session", "$child", remove_following_runs=True)
        assert {run.run_id for run in _row(source).runs} == {"run-0", "run-1"}
    finally:
        storage.close()


def test_explicit_deletion_erases_subtree_split_between_archive_and_live_rows(tmp_path: Path) -> None:
    """Deleting an archived parent also removes archived children and resurrected live grandchildren."""
    storage, source = _setup(tmp_path)
    try:
        parent = get_agent_session(storage, "session").runs[0]
        child = deepcopy(parent)
        child.run_id = "child"
        child.parent_run_id = "run-0"
        grandchild = deepcopy(child)
        grandchild.run_id = "grandchild"
        grandchild.parent_run_id = "child"
        storage.upsert_run(child, "session")
        storage.upsert_run(grandchild, "session")
        _compact(storage, ["run-0"])
        with sqlite3.connect(source.path) as connection:
            raw = json.loads(
                connection.execute("SELECT run_data FROM code_sessions_usage WHERE run_id = 'child'").fetchone()[0],
            )
            assert set(raw) == {"run_id", "parent_run_id", "metadata"}
        storage.upsert_run(grandchild, "session")
        storage.delete_runs(["run-0"])
        assert {run.run_id for run in _row(source).runs} == {"run-1", "run-2"}
        assert {run.run_id for run in get_agent_session(storage, "session").runs} == {"run-1", "run-2"}
        with sqlite3.connect(source.path) as connection:
            assert {row[0] for row in connection.execute("SELECT run_id FROM code_sessions_usage")} == {
                "run-1",
                "run-2",
            }
    finally:
        storage.close()
