"""One-time migration imports available usage without reconstructing missing history."""

from __future__ import annotations

import importlib
import json
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from mindroom import legacy_usage_storage
from mindroom.constants import resolve_runtime_paths
from tests.conftest import create_agno_2_sessions_db

if TYPE_CHECKING:
    from pathlib import Path


def _usage(path: Path) -> list[tuple[str | None, object]]:
    with sqlite3.connect(path) as connection:
        return [
            (run_id, json.loads(data) if data is not None else None)
            for run_id, data in connection.execute("SELECT run_id, usage_data FROM code_sessions_usage ORDER BY id")
        ]


def test_migration_imports_once_with_current_precedence(tmp_path: Path) -> None:
    """Current run rows win; absent historical dates and IDs must not be synthesized."""
    path = create_agno_2_sessions_db(tmp_path / "code.db")
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
        connection.execute(
            "CREATE TABLE code_sessions_runs (session_id TEXT, run_id TEXT, run_data TEXT, created_at INTEGER)",
        )
        connection.execute(
            "INSERT INTO code_sessions_runs VALUES (?, ?, ?, ?)",
            (
                "session-1",
                "overlap",
                json.dumps({"run_id": "overlap", "created_at": 99, "metrics": {"total_tokens": 20}}),
                1,
            ),
        )
    legacy_usage_storage.migrate_usage_database(path, "code_sessions")
    before = path.read_bytes()
    legacy_usage_storage.migrate_usage_database(path, "code_sessions")

    assert path.read_bytes() == before
    rows = dict(_usage(path))
    assert len(rows) == 3
    assert rows["overlap"]["metrics"]["total_tokens"] == 20
    assert rows["overlap"]["created_at"] == 1
    assert "created_at" not in rows[None]
    assert "user_id" not in rows[None]
    assert "private history" not in json.dumps(rows)


def test_migration_rolls_back_schema_and_seed_on_interruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed first import cannot publish an empty or partially seeded usage table."""
    path = create_agno_2_sessions_db(tmp_path / "code.db")
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
            legacy_usage_storage.migrate_usage_database(path, "code_sessions")
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'code_sessions_usage'").fetchall()

    legacy_usage_storage.migrate_usage_database(path, "code_sessions")
    assert len(_usage(path)) == 3


def test_malformed_history_keeps_valid_records_and_a_gap(tmp_path: Path) -> None:
    """A malformed entry cannot discard adjacent facts or silently become zero usage."""
    path = create_agno_2_sessions_db(tmp_path / "code.db")
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
    legacy_usage_storage.migrate_usage_database(path, "code_sessions")
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

    await legacy_usage_storage.migrate_usage_storage(paths)
    await legacy_usage_storage.migrate_usage_storage(paths)

    assert [len(_usage(path)) for path in stores] == [3, 3, 3]
    assert (untouched.read_bytes(), outside.read_bytes()) == before


def test_missing_database_and_empty_database_stay_empty(tmp_path: Path) -> None:
    """Migration must not create new stores or force Agno's lazy tables into existence."""
    path = tmp_path / "code.db"
    legacy_usage_storage.migrate_usage_database(path, "code_sessions")
    assert not path.exists()
    with sqlite3.connect(path):
        pass
    legacy_usage_storage.migrate_usage_database(path, "code_sessions")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["api", "orchestrator"])
async def test_usage_migration_failure_prevents_runtime_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Neither entry point may serve exports or write runs after a failed initial import."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    path = create_agno_2_sessions_db(paths.storage_root / "agents/code/sessions/code.db")
    path.write_bytes(b"not a database")
    module = importlib.import_module(f"mindroom.{'api.main' if entrypoint == 'api' else 'orchestrator'}")
    monkeypatch.setattr(module, "sync_env_to_credentials", Mock(side_effect=AssertionError("credentials started")))
    if entrypoint == "api":
        monkeypatch.setattr(module, "_app_runtime_paths", lambda _app: paths)
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            async with module._lifespan(module.app):
                pytest.fail("API admitted runtime work")
    else:
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            await module.main("ERROR", paths, api=False)


def test_migration_keeps_invalid_parent_as_a_gap(tmp_path: Path) -> None:
    """Invalid nested classification must never promote a child into top-level usage."""
    path = create_agno_2_sessions_db(tmp_path / "code.db")
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
    legacy_usage_storage.migrate_usage_database(path, "code_sessions")
    payload = _usage(path)[0][1]
    assert payload["parent_run_id"] is False
    assert "private" not in json.dumps(payload)
