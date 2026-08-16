"""Regression coverage for the non-mutating persisted-usage reader."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from mindroom.usage_stats_storage import (
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    _open_read_only_database,
    iter_usage_storage_rows,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SESSION_COLUMNS = """
    session_id TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    agent_id TEXT,
    team_id TEXT,
    user_id TEXT,
    session_data TEXT,
    runs TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
"""


def _source(path: Path, *, table: str = "code_sessions") -> UsageStorageSource:
    return UsageStorageSource(
        path=path,
        path_label=path.name,
        scope="shared_agent",
        expected_session_table=table,
        source_agent_id="agent-code",
        allowed_agent_ids=frozenset({"agent-code"}),
        allowed_team_ids=frozenset({"team-engineering"}),
        requester_isolated=False,
    )


def _run(*, nested: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "agent_id": "agent-code",
        "user_id": "@alice:example.test",
        "created_at": 1_723_837_600,
        "model_provider": "openai",
        "model": "gpt-5.6",
        "status": "completed",
        "metrics": {
            "input_tokens": 12,
            "output_tokens": 8,
            "total_tokens": 20,
            "cost": 0.0125,
            "details": {
                "model": [
                    {
                        "id": "gpt-5.6",
                        "provider": "openai",
                        "input_tokens": 12,
                        "output_tokens": 8,
                        "total_tokens": 20,
                        "cost": 0.0125,
                    },
                ],
            },
        },
        "member_responses": nested or [],
        "messages": [{"content": "secret prompt"}],
        "tools": [{"tool_name": "secret_tool", "result": "secret output"}],
        "metadata": {"ignored": "secret metadata"},
    }


def _create_database(
    path: Path,
    *,
    table: str = "code_sessions",
    row_count: int = 1,
    runs: object | None = None,
    session_data: object | None = None,
    extra_table: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f'CREATE TABLE "{table}" ({SESSION_COLUMNS})')
        if extra_table:
            connection.execute(f'CREATE TABLE "other_sessions" ({SESSION_COLUMNS})')
        row_runs = json.dumps(runs if runs is not None else [_run()])
        row_session_data = json.dumps(
            session_data if session_data is not None else {"session_metrics": {"total_tokens": 20}},
        )
        for number in range(row_count):
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',  # noqa: S608 - local fixture table.
                (
                    f"session-{number}",
                    "agent",
                    "agent-code",
                    None,
                    "@alice:example.test",
                    row_session_data,
                    row_runs,
                    1_723_837_600,
                    1_723_837_600,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _read(source: UsageStorageSource) -> list[UsageSessionRow | UsageStorageDiagnostic]:
    return list(iter_usage_storage_rows(source))


def _snapshot_database_entries(path: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        entry.name: (entry.read_bytes(), entry.stat().st_size, entry.stat().st_mtime_ns)
        for entry in sorted(path.parent.iterdir())
        if entry.name.startswith(path.name)
    }


def test_reader_extracts_a_field_selective_immutable_agno_session_row(tmp_path: Path) -> None:
    """The reader keeps only immutable usage fields from an Agno row."""
    database = tmp_path / "code.db"
    _create_database(database)

    result = _read(_source(database))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert row.entity_id == "agent-code"
    assert row.entity_kind == "agent"
    assert row.session_user_id == "@alice:example.test"
    assert row.session_metrics == {"total_tokens": 20}
    assert row.runs[0].metrics == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cost": 0.0125,
    }
    assert row.runs[0].model_metrics[0].model_type == "model"
    assert row.runs[0].model_metrics[0].model_id == "gpt-5.6"
    assert not hasattr(row.runs[0], "messages")
    with pytest.raises(AttributeError):
        row.entity_id = "changed"  # type: ignore[misc]


def test_missing_database_is_absent_without_creating_a_file(tmp_path: Path) -> None:
    """An absent source remains absent after a read attempt."""
    database = tmp_path / "missing.db"

    result = _read(_source(database))

    assert result == [UsageStorageDiagnostic(path_label="missing.db", status="absent", detail="database absent")]
    assert not database.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_read_preserves_durable_entries_and_allows_existing_wal_shm_coordination_updates(
    tmp_path: Path, journal_mode: str,
) -> None:
    """Durable SQLite entries stay stable; an existing WAL-index may coordinate readers."""
    database = tmp_path / f"{journal_mode}.db"
    _create_database(database)
    holder = sqlite3.connect(database)
    try:
        assert holder.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
        if journal_mode == "wal":
            holder.execute("PRAGMA wal_autocheckpoint=0")
            holder.execute("INSERT INTO code_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                "live-session", "agent", "agent-code", None, "@alice:example.test", "{}", "[]", 1, 1,
            ))
            holder.commit()
            assert database.with_name(f"{database.name}-wal").exists()
            assert database.with_name(f"{database.name}-shm").exists()
        else:
            # A cold rollback-journal must not be opened, removed, or rewritten by the reader.
            database.with_name(f"{database.name}-journal").write_bytes(b"\x00cold journal")
        before = _snapshot_database_entries(database)

        result = _read(_source(database))

        assert any(isinstance(item, UsageSessionRow) for item in result)
        after = _snapshot_database_entries(database)
        assert after.keys() == before.keys()
        for name, snapshot in before.items():
            if name.endswith("-shm"):
                # SQLite owns the existing WAL-index mapping and may update its
                # transient read marks; it must neither grow nor be replaced.
                assert after[name][1] == snapshot[1]
                continue
            assert after[name] == snapshot
    finally:
        holder.close()


def test_wal_database_without_required_sidecars_is_reported_without_creating_them(tmp_path: Path) -> None:
    """A live WAL source without both sidecars is not opened."""
    database = tmp_path / "wal-missing.db"
    _create_database(database)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
    finally:
        connection.close()
    database.with_name(f"{database.name}-wal").unlink(missing_ok=True)
    database.with_name(f"{database.name}-shm").unlink(missing_ok=True)
    before = _snapshot_database_entries(database)

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="wal-missing.db", status="partial", detail="WAL sidecars unavailable"),
    ]
    assert _snapshot_database_entries(database) == before


def test_read_only_connection_rejects_insert(tmp_path: Path) -> None:
    """The connection boundary rejects an attempted write."""
    database = tmp_path / "code.db"
    _create_database(database)

    with _open_read_only_database(database) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO code_sessions VALUES ('other', 'agent', NULL, NULL, NULL, NULL, NULL, 1, 1)")


@pytest.mark.parametrize(
    ("setup", "expected_status"),
    [
        ("corrupt", "corrupt"),
        ("missing-table", "unsupported_schema"),
        ("invalid-json", "partial"),
    ],
)
def test_unreadable_or_malformed_storage_returns_bounded_diagnostics(
    tmp_path: Path, setup: str, expected_status: str,
) -> None:
    """Corrupt, unsupported, and malformed sources return safe diagnostics."""
    database = tmp_path / "code.db"
    if setup == "corrupt":
        database.write_bytes(b"not a sqlite database")
    elif setup == "missing-table":
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        finally:
            connection.close()
    else:
        _create_database(database)
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE code_sessions SET runs = ?", ("{",))
            connection.commit()
        finally:
            connection.close()

    result = _read(_source(database))

    assert len(result) == 1
    diagnostic = result[0]
    assert isinstance(diagnostic, UsageStorageDiagnostic)
    assert diagnostic.status == expected_status
    assert str(database) not in diagnostic.detail
    assert "secret" not in diagnostic.detail


def test_locked_database_returns_busy_diagnostic(tmp_path: Path) -> None:
    """A source locked beyond the bounded timeout is reported as busy."""
    database = tmp_path / "code.db"
    _create_database(database)
    holder = sqlite3.connect(database, timeout=1.0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        result = _read(_source(database))
    finally:
        holder.rollback()
        holder.close()

    assert result == [UsageStorageDiagnostic(path_label="code.db", status="busy", detail="database busy")]


@pytest.mark.parametrize("column", ["runs", "session_data"])
def test_oversized_json_cell_returns_resource_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str) -> None:
    """SQL length guards keep oversized JSON cells out of the decoder."""
    database = tmp_path / "code.db"
    _create_database(database)
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_JSON_BYTES", 16)
    connection = sqlite3.connect(database)
    try:
        if column == "session_data":
            connection.execute("UPDATE code_sessions SET runs = ?", ("[]",))
        connection.execute(
            f"UPDATE code_sessions SET {column} = ?",  # noqa: S608 - parametrization is two literals.
            (json.dumps({"payload": "x" * 64}),),
        )
        connection.commit()
    finally:
        connection.close()

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="resource_limit", detail=f"{column} exceeds limit"),
    ]


def test_excessive_nested_responses_returns_resource_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested member responses are bounded before full tree retention."""
    database = tmp_path / "code.db"
    nested = _run()
    for _ in range(3):
        nested = _run(nested=[nested])
    _create_database(database, runs=[nested])
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_NESTED_RESPONSE_DEPTH", 2)

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="resource_limit", detail="nested response depth exceeds limit"),
    ]


def test_excessive_extracted_nodes_returns_resource_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader stops once its retained-node budget is exhausted."""
    database = tmp_path / "code.db"
    _create_database(database, runs=[_run(), _run()])
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_EXTRACTED_RUN_NODES", 1)

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="resource_limit", detail="run node count exceeds limit"),
    ]


def test_large_run_discards_messages_prompts_and_tool_payloads_while_streaming_rows(tmp_path: Path) -> None:
    """Large ignored payloads never enter the field-selective result."""
    database = tmp_path / "code.db"
    large_run = _run()
    large_run["messages"] = [{"content": "prompt " + "x" * 100_000}]
    large_run["tools"] = [{"result": "tool output " + "x" * 100_000}]
    _create_database(database, row_count=2, runs=[large_run])

    rows: Iterator[UsageSessionRow | UsageStorageDiagnostic] = iter_usage_storage_rows(_source(database))
    first = next(rows)

    assert isinstance(first, UsageSessionRow)
    assert first.runs[0].metrics["total_tokens"] == 20
    assert not hasattr(first.runs[0], "tools")
    second = next(rows)
    assert isinstance(second, UsageSessionRow)
    with pytest.raises(StopIteration):
        next(rows)


def test_reader_uses_only_the_expected_session_table(tmp_path: Path) -> None:
    """The source contract selects exactly one validated session table."""
    database = tmp_path / "code.db"
    _create_database(database, extra_table=True)

    accepted = _read(_source(database))
    rejected = _read(_source(database, table="unknown_sessions"))

    assert len(accepted) == 1
    assert isinstance(accepted[0], UsageSessionRow)
    assert rejected == [
        UsageStorageDiagnostic(path_label="code.db", status="unsupported_schema", detail="session table unavailable"),
    ]
