"""A database's conversion to storage v2 seeds its usage ledger from the history it holds, reconstructing nothing."""

from __future__ import annotations

import importlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from structlog.testing import capture_logs

from mindroom.constants import resolve_runtime_paths
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_database, migrate_conversation_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_runs_sql
from mindroom.session_storage.schema import TableNames
from mindroom.usage import legacy_usage_storage
from mindroom.usage.stats_storage import UsageSessionRow, UsageStorageSource, iter_usage_storage_rows
from tests.conftest import create_agno_2_sessions_db

if TYPE_CHECKING:
    from pathlib import Path

_NAMES = AgnoTableNames("code")


def _convert(path: Path) -> None:
    migrate_conversation_database(path, TableNames.of_database(path))


def _usage(path: Path) -> list[tuple[str | None, object]]:
    with sqlite3.connect(path) as connection:
        return [
            (run_id, json.loads(data) if data is not None else None)
            for run_id, data in connection.execute("SELECT run_id, usage_data FROM code_sessions_usage ORDER BY id")
        ]


def _skipped_paths(logs: list[dict[str, object]]) -> list[object]:
    return [entry["path"] for entry in logs if entry["event"] == "conversation_conversion_skipped_store"]


def test_migration_imports_once_with_current_precedence(tmp_path: Path) -> None:
    """Current run rows win; absent historical dates and IDs must not be synthesized."""
    path = create_agno_2_sessions_db(_NAMES.database_path(tmp_path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE code_sessions SET runs = ?",
            (
                json.dumps(
                    [
                        {"run_id": "overlap", "created_at": 2, "metrics": {"total_tokens": 10}},
                        {"metrics": {"total_tokens": 3}, "content": "private history"},
                        {"run_id": "legacy", "metrics": {"total_tokens": 4}},
                    ],
                ),
            ),
        )
        for statement in agno_runs_sql(_NAMES):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO code_sessions_runs (session_id, run_id, run_type, run_index, run_data, created_at) "
            "VALUES (?, ?, 'agent', 0, ?, ?)",
            (
                "session-1",
                "overlap",
                json.dumps({"run_id": "overlap", "created_at": 99, "metrics": {"total_tokens": 20}}),
                1,
            ),
        )
    _convert(path)
    before = path.read_bytes()
    _convert(path)

    assert path.read_bytes() == before
    rows = dict(_usage(path))
    assert len(rows) == 3
    assert rows["overlap"]["metrics"]["total_tokens"] == 20
    assert rows["overlap"]["created_at"] == 1
    assert "created_at" not in rows[None]
    assert "user_id" not in rows[None]
    assert "private history" not in json.dumps(rows)


def test_migration_reads_the_agno_runs_table_beside_agno_sessions(tmp_path: Path) -> None:
    """Agno names the runs table beside ``agno_sessions`` ``agno_runs``, so an entity named agno keeps its run usage."""
    names = AgnoTableNames("agno")
    path = names.database_path(tmp_path)
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in agno_runs_sql(names):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO agno_sessions (session_id, session_type, agent_id, created_at) VALUES ('session-1', 'agent', 'agno', 1)",
        )
        connection.execute(
            "INSERT INTO agno_runs (session_id, run_id, run_type, run_index, run_data, created_at) "
            "VALUES (?, ?, 'agent', 0, ?, ?)",
            ("session-1", "run", json.dumps({"metrics": {"total_tokens": 7}}), 1),
        )

    _convert(path)

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT run_id, usage_data FROM agno_sessions_usage").fetchall()
    assert [(run_id, json.loads(data)["metrics"]["total_tokens"]) for run_id, data in rows] == [("run", 7)]


@pytest.mark.parametrize("created_at", [None, True, "invalid", [], {}, math.nan, math.inf])
def test_a_blob_run_with_an_unusable_timestamp_keeps_its_usage(tmp_path: Path, created_at: object) -> None:
    """A blob run whose timestamp is absent or malformed converts, and its usable token counters stay reported."""
    path = create_agno_2_sessions_db(_NAMES.database_path(tmp_path))
    blob = [{"run_id": "odd", "agent_id": "code", "created_at": created_at, "metrics": {"total_tokens": 20}}]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE code_sessions SET runs = ?", (json.dumps(json.dumps(blob)),))

    _convert(path)

    source = UsageStorageSource(
        path=path,
        path_label=path.name,
        scope="shared_agent",
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code"}),
        requester_isolated=False,
    )
    [row] = list(iter_usage_storage_rows(source))
    assert isinstance(row, UsageSessionRow)
    [run] = row.runs
    assert (run.run_id, run.created_at, run.metrics["total_tokens"]) == ("odd", None, 20)


def test_migration_rolls_back_schema_and_seed_on_interruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed first import cannot publish an empty or partially seeded usage table."""
    path = create_agno_2_sessions_db(_NAMES.database_path(tmp_path))
    project = legacy_usage_storage.project_usage
    calls = 0

    def interrupt(run: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "interrupted seed"
            raise RuntimeError(message)
        return project(run)

    with monkeypatch.context() as patch:
        patch.setattr(legacy_usage_storage, "project_usage", interrupt)
        with pytest.raises(RuntimeError, match="interrupted seed"):
            _convert(path)
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'code_sessions_usage'").fetchall()

    _convert(path)
    assert len(_usage(path)) == 3


def test_malformed_history_keeps_valid_records_and_a_gap(tmp_path: Path) -> None:
    """A malformed entry cannot discard adjacent facts or silently become zero usage."""
    path = create_agno_2_sessions_db(_NAMES.database_path(tmp_path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE code_sessions SET runs = ?",
            (
                json.dumps(
                    [
                        {"run_id": "valid", "metrics": {"total_tokens": 5}},
                        "malformed",
                    ],
                ),
            ),
        )
    _convert(path)
    rows = _usage(path)
    assert rows[0][1]["metrics"]["total_tokens"] == 5
    assert rows[1] == (None, None)


@pytest.mark.asyncio
async def test_startup_migrates_dormant_stores_and_skips_aliases(tmp_path: Path) -> None:
    """Inventory is independent of config, honors the session root, and never follows symlinks."""
    root = tmp_path / "sessions"
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "state",
        process_env={"MINDROOM_SESSION_STORAGE_PATH": "sessions"},
    )
    stores = [
        create_agno_2_sessions_db(root / relative / "code.db")
        for relative in (
            "agents/code/sessions",
            "private_instances/dormant/code/sessions",
            "teams/code/sessions",
        )
    ]
    untouched = create_agno_2_sessions_db(tmp_path / "state/agents/code/sessions/code.db")
    outside = create_agno_2_sessions_db(tmp_path / "outside/code/sessions/code.db")
    (root / "agents/alias").symlink_to(outside.parent.parent, target_is_directory=True)
    (root / "private_instances/alias").symlink_to(root / "private_instances/dormant", target_is_directory=True)
    before = untouched.read_bytes(), outside.read_bytes()

    await migrate_conversation_storage(paths)
    await migrate_conversation_storage(paths)

    assert [len(_usage(path)) for path in stores] == [3, 3, 3]
    assert (untouched.read_bytes(), outside.read_bytes()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["api", "orchestrator"])
async def test_unreadable_session_database_does_not_stop_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Worker code can corrupt one session database; both entry points still import the rest and continue."""

    class _NextStartupStepError(Exception):
        pass

    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    corrupt = paths.storage_root / "agents/broken/sessions/broken.db"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a database")
    healthy = create_agno_2_sessions_db(paths.storage_root / "agents/code/sessions/code.db")
    module = importlib.import_module(f"mindroom.{'api.main' if entrypoint == 'api' else 'orchestrator'}")
    if entrypoint == "api":
        monkeypatch.setattr(module, "migrate_tool_credential_defaults", Mock(side_effect=_NextStartupStepError))
    else:
        # The orchestrator converts after it sets up logging and retires old workers, right before it builds itself.
        monkeypatch.setattr(module, "setup_logging", Mock())
        monkeypatch.setattr(module, "_MultiAgentOrchestrator", Mock(side_effect=_NextStartupStepError))
    with capture_logs() as logs:
        if entrypoint == "api":
            monkeypatch.setattr(module, "_app_runtime_paths", lambda _app: paths)
            with pytest.raises(_NextStartupStepError):
                async with module._lifespan(module.app):
                    pytest.fail("API admitted runtime work")
        else:
            with pytest.raises(_NextStartupStepError):
                await module.main("ERROR", paths, api=False)

    assert len(_usage(healthy)) == 3
    assert corrupt.read_bytes() == b"not a database"
    assert _skipped_paths(logs) == [str(corrupt)]


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize(
    ("blocked", "skipped"),
    [
        ("agents/broken", "agents/broken/sessions/broken.db"),
        ("agents/broken/sessions", "agents/broken/sessions/broken.db"),
        ("private_instances/scope", "private_instances/scope"),
    ],
)
async def test_unsearchable_store_directory_does_not_stop_startup(tmp_path: Path, blocked: str, skipped: str) -> None:
    """Worker code can make a store directory unsearchable; startup skips the stores beneath it and imports the rest."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    for store in ("agents/broken", "private_instances/scope/broken"):
        (paths.storage_root / store / "sessions").mkdir(parents=True)
    healthy = create_agno_2_sessions_db(paths.storage_root / "agents/code/sessions/code.db")
    blocked_dir = paths.storage_root / blocked
    blocked_dir.chmod(0)
    try:
        with capture_logs() as logs:
            await migrate_conversation_storage(paths)
    finally:
        blocked_dir.chmod(0o755)

    assert len(_usage(healthy)) == 3
    assert _skipped_paths(logs) == [str(paths.storage_root / skipped)]


@pytest.mark.asyncio
async def test_locked_store_is_skipped_quickly_and_imported_by_its_owner(tmp_path: Path) -> None:
    """Startup skips a store worker code keeps locked without waiting, and its owner still imports it later."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    locked = create_agno_2_sessions_db(paths.storage_root / "agents/code/sessions/code.db")
    holder = sqlite3.connect(locked, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with capture_logs() as logs:
            await migrate_conversation_storage(paths)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert elapsed < 10
    assert _skipped_paths(logs) == [str(locked)]
    _convert(locked)
    assert len(_usage(locked)) == 3


def test_migration_keeps_invalid_parent_as_a_gap(tmp_path: Path) -> None:
    """Invalid nested classification must never promote a child into top-level usage."""
    path = create_agno_2_sessions_db(_NAMES.database_path(tmp_path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE code_sessions SET runs = ?",
            (
                json.dumps(
                    [
                        {"run_id": "child", "parent_run_id": {"private": "parent"}, "metrics": {"total_tokens": 500}},
                    ],
                ),
            ),
        )
    # The seeding step alone: the blob-to-rows step after it cannot store a parent that is not text.
    with closing(sqlite3.connect(path)) as connection, connection:
        legacy_usage_storage.seed_usage_ledger(connection, _NAMES)
    payload = _usage(path)[0][1]
    assert payload["parent_run_id"] is False
    assert "private" not in json.dumps(payload)
