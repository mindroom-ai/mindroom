"""Automatic retirement of Agno 2 session run blobs."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom import legacy_session_migration
from mindroom.agent_storage import create_state_storage, get_agent_session, save_runs
from mindroom.constants import resolve_runtime_paths
from mindroom.legacy_session_migration import (
    _migrate_table,
    _run_legacy_session_migration,
)
from mindroom.orchestrator import _schedule_legacy_session_migration
from tests.conftest import create_agno_2_sessions_db

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

    from mindroom.constants import RuntimePaths


def _storage(tmp_path: Path) -> BaseDb:
    return create_state_storage(
        "code",
        tmp_path,
        subdir="sessions",
        session_table="code_sessions",
    )


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _blob_is_null(db_path: Path, session_id: str) -> bool:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT runs IS NULL FROM code_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        assert row is not None
        return bool(row[0])
    finally:
        connection.close()


def _loaded_run_ids(tmp_path: Path) -> list[str]:
    storage = _storage(tmp_path)
    try:
        session = get_agent_session(storage, "session-1")
        assert session is not None
        return [run.run_id or "" for run in session.runs or []]
    finally:
        storage.close()


def _schema_version(db_path: Path) -> str:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT version FROM agno_schema_versions WHERE table_name = 'code_sessions'",
        ).fetchone()
        assert row is not None
        return row[0]
    finally:
        connection.close()


def test_migration_preserves_the_visible_order_of_partially_migrated_sessions(tmp_path: Path) -> None:
    """Legacy runs stay first and newer row-only runs stay last after the blob is cleared."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    storage = _storage(tmp_path)
    try:
        session = get_agent_session(storage, "session-1")
        assert session is not None
        storage.delete_runs(["run-2"])
        edited_run_1 = deepcopy((session.runs or [])[0])
        edited_run_1.metadata = {"newer_row": True}
        run_4 = RunOutput(run_id="run-4", session_id="session-1", agent_id="code", messages=[])
        save_runs(storage, session, [edited_run_1, run_4])
    finally:
        storage.close()

    result = _migrate_table(db_path, "code_sessions")

    assert result.migrated_sessions == 1
    assert result.failed_sessions == 0
    assert _loaded_run_ids(tmp_path) == ["run-1", "run-3", "run-4"]
    storage = _storage(tmp_path)
    try:
        migrated = get_agent_session(storage, "session-1")
    finally:
        storage.close()
    assert migrated is not None
    assert (migrated.runs or [])[0].metadata == {"newer_row": True}
    assert _blob_is_null(db_path, "session-1")
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT run_id, run_index FROM code_sessions_runs ORDER BY run_index",
        ).fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()
    assert rows == [("run-1", 0), ("run-3", 1), ("run-4", 2)]
    assert journal_mode == ("delete",)
    assert _schema_version(db_path) == "3.0.0"
    assert _migrate_table(db_path, "code_sessions").migrated_sessions == 0


def test_malformed_session_is_left_untouched_without_blocking_other_sessions(tmp_path: Path) -> None:
    """One bad blob remains readable through compatibility code while valid sessions migrate."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, runs, created_at) VALUES (?, ?, ?, ?, ?)",
            ("broken-session", "agent", "code", "{broken", 1),
        )
        connection.commit()
    finally:
        connection.close()

    result = _migrate_table(db_path, "code_sessions")

    assert result.migrated_sessions == 1
    assert result.failed_sessions == 1
    assert _blob_is_null(db_path, "session-1")
    assert not _blob_is_null(db_path, "broken-session")
    assert _schema_version(db_path) == "2.5.6"


def test_blob_clear_failure_rolls_back_inserted_rows(tmp_path: Path) -> None:
    """A crash or write failure cannot leave a blob retired before its run rows commit."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TRIGGER refuse_blob_clear BEFORE UPDATE OF runs ON code_sessions "
            "BEGIN SELECT RAISE(ABORT, 'blob clear refused'); END",
        )
        connection.commit()
    finally:
        connection.close()

    result = _migrate_table(db_path, "code_sessions")

    assert result.migrated_sessions == 0
    assert result.failed_sessions == 1
    assert not _blob_is_null(db_path, "session-1")
    connection = sqlite3.connect(db_path)
    try:
        (stored_runs,) = connection.execute("SELECT COUNT(*) FROM code_sessions_runs").fetchone()
    finally:
        connection.close()
    assert stored_runs == 0


def test_run_id_owned_by_another_session_keeps_the_legacy_blob(tmp_path: Path) -> None:
    """A global run-id collision cannot make verification retire the authoritative blob."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    storage = _storage(tmp_path)
    try:
        storage.upsert_session(AgentSession(session_id="other-session", agent_id="code"))
        storage.upsert_run(
            RunOutput(run_id="run-1", session_id="other-session", agent_id="code"),
            session_id="other-session",
        )
    finally:
        storage.close()

    result = _migrate_table(db_path, "code_sessions")

    assert result.migrated_sessions == 0
    assert result.failed_sessions == 1
    assert not _blob_is_null(db_path, "session-1")
    connection = sqlite3.connect(db_path)
    try:
        owner = connection.execute(
            "SELECT session_id, run_index FROM code_sessions_runs WHERE run_id = 'run-1'",
        ).fetchone()
    finally:
        connection.close()
    assert owner == ("other-session", 0)


def test_concurrent_delete_finishes_during_decode_and_is_preserved_by_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expensive decode holds no writer lock; a changed blob is reread before migration."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    decode_started = threading.Event()
    allow_decode = threading.Event()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    original_decode = legacy_session_migration._validated_legacy_runs

    def blocked_decode(blob: object):  # noqa: ANN202
        decode_started.set()
        assert allow_decode.wait(5)
        return original_decode(blob)

    monkeypatch.setattr(legacy_session_migration, "_validated_legacy_runs", blocked_decode)
    migration = threading.Thread(target=_migrate_table, args=(db_path, "code_sessions"))
    migration.start()
    assert decode_started.wait(5)

    storage = _storage(tmp_path)

    def delete_run() -> None:
        delete_started.set()
        storage.delete_runs(["run-2"])
        delete_finished.set()

    deletion = threading.Thread(target=delete_run)
    deletion.start()
    assert delete_started.wait(5)
    assert delete_finished.wait(5)
    allow_decode.set()
    migration.join(5)
    deletion.join(5)
    storage.close()

    assert not migration.is_alive()
    assert not deletion.is_alive()
    assert _loaded_run_ids(tmp_path) == ["run-1", "run-3"]
    assert _blob_is_null(db_path, "session-1")


@pytest.mark.asyncio
async def test_spawned_process_migrates_the_storage_root(tmp_path: Path) -> None:
    """The production process boundary performs the complete storage-root scan."""
    db_path = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    await _run_legacy_session_migration(tmp_path, log_level="INFO", runtime_paths=_runtime_paths(tmp_path))

    assert _blob_is_null(db_path, "session-1")


@pytest.mark.asyncio
async def test_current_storage_does_not_spawn_a_migration_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary restart pays only the read-only preflight scan."""
    storage = _storage(tmp_path)
    storage.upsert_session(AgentSession(session_id="current-session", agent_id="code"))
    storage.close()

    class FakeContext:
        def Process(self, **_kwargs: object) -> None:  # noqa: N802
            pytest.fail("a migration process was spawned without legacy data")

    monkeypatch.setattr(
        legacy_session_migration.multiprocessing,
        "get_context",
        lambda _method: FakeContext(),
    )

    await _run_legacy_session_migration(tmp_path, log_level="INFO", runtime_paths=_runtime_paths(tmp_path))


def test_migration_child_configures_normal_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Child-process warnings use the same configured handlers as the runtime."""
    configured: list[tuple[str, object]] = []
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr(
        legacy_session_migration,
        "setup_logging",
        lambda *, level, runtime_paths: configured.append((level, runtime_paths)),
    )

    legacy_session_migration._migrate_storage_targets([], "WARNING", runtime_paths)

    assert configured == [("WARNING", runtime_paths)]


def test_preflight_skips_an_unreadable_database_without_hiding_valid_work(tmp_path: Path) -> None:
    """One broken database cannot prevent independent session databases from migrating."""
    broken = tmp_path / "a" / "sessions" / "broken.db"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not sqlite")
    valid = create_agno_2_sessions_db(tmp_path / "b" / "sessions" / "code.db")

    targets = legacy_session_migration._pending_migration_targets(tmp_path)

    assert targets == [(str(valid), "code_sessions")]


@pytest.mark.asyncio
async def test_orchestrator_starts_migration_after_readiness_against_the_session_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy scanning must never extend the unavailable startup window."""
    started = asyncio.Event()
    session_root = tmp_path / "session-state"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(session_root)},
    )
    orchestrator = MagicMock()
    runtime_ready_event = asyncio.Event()
    orchestrator._runtime_ready_event = runtime_ready_event
    expected_runtime_paths = runtime_paths

    async def record_start(storage_root: Path, *, log_level: str, runtime_paths: object) -> None:
        assert storage_root == session_root
        assert log_level == "INFO"
        assert runtime_paths is expected_runtime_paths
        started.set()

    monkeypatch.setattr(legacy_session_migration, "_run_legacy_session_migration", record_start)
    task = _schedule_legacy_session_migration(orchestrator, runtime_paths, "INFO")
    assert orchestrator._runtime_ready_event is runtime_ready_event
    await asyncio.sleep(0)
    assert not started.is_set()

    orchestrator._runtime_ready_event.set()
    await task
    assert started.is_set()


@pytest.mark.asyncio
async def test_background_process_is_terminated_when_runtime_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot leave the migration child running after runtime shutdown."""
    create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    join_started = threading.Event()
    release_join = threading.Event()

    class FakeProcess:
        exitcode = None
        terminated = False

        def start(self) -> None:
            return None

        def join(self) -> None:
            join_started.set()
            assert release_join.wait(5)

        def is_alive(self) -> bool:
            return not release_join.is_set()

        def terminate(self) -> None:
            self.terminated = True
            release_join.set()

    process = FakeProcess()

    class FakeContext:
        def Process(self, **_kwargs: object) -> FakeProcess:  # noqa: N802
            return process

    context = FakeContext()
    monkeypatch.setattr(legacy_session_migration.multiprocessing, "get_context", lambda _method: context)

    task = asyncio.create_task(
        _run_legacy_session_migration(tmp_path, log_level="INFO", runtime_paths=_runtime_paths(tmp_path)),
    )
    assert await asyncio.to_thread(join_started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated
