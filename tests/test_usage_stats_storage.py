"""Regression coverage for the non-mutating persisted-usage reader."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.run import RunStatus

from mindroom import usage_stats_storage
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, worker_dir_name
from mindroom.usage_stats_storage import (
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    _open_read_only_database,
    discover_admin_usage_sources,
    discover_self_usage_sources,
    iter_usage_storage_rows,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def _create_discovery_database(path: Path, *, table: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _create_database(path, table=table)


def _discovery_config() -> Config:
    return Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                private=AgentPrivateConfig(per="user"),
            ),
            "shared": AgentConfig(display_name="Shared"),
            "linked": AgentConfig(display_name="Linked"),
            "absent": AgentConfig(display_name="Absent"),
        },
        teams={
            "configured": TeamConfig(
                display_name="Configured",
                role="Configured test team",
                agents=["shared"],
            ),
        },
    )


def _discovery_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "primary-storage",
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(tmp_path / "dedicated-sessions")},
    )


def _identity(*, requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def _private_database_path(
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    *,
    agent_name: str = "code",
) -> Path:
    worker_key = resolve_worker_key("user", identity, agent_name=agent_name)
    assert worker_key is not None
    return (
        runtime_paths.config_dir
        / "dedicated-sessions"
        / "private_instances"
        / worker_dir_name(worker_key)
        / agent_name
        / "sessions"
        / f"{agent_name}.db"
    )


def _source_labels(sources: tuple[UsageStorageSource, ...]) -> list[str]:
    return [source.path_label for source in sources]


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


def _snapshot_database_entries(path: Path) -> dict[str, tuple[bytes, int, int, tuple[int, int] | None]]:
    return {
        entry.name: (
            entry.read_bytes(),
            entry.stat().st_size,
            entry.stat().st_mtime_ns,
            _entry_identity(entry),
        )
        for entry in sorted(path.parent.iterdir())
        if entry.name.startswith(path.name)
    }


def _entry_identity(path: Path) -> tuple[int, int] | None:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino) if stat.st_ino else None


def _deep_ignored_json(*, column: str, depth: int) -> str:
    nested = '{"ignored":' * depth + "0" + "}" * depth
    if column == "runs":
        return '[{"agent_id":"agent-code","metrics":{},"ignored":' + nested + "}]"
    return '{"session_metrics":{},"ignored":' + nested + "}"


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


@pytest.mark.parametrize(
    ("persisted_status", "expected_status"),
    [
        (RunStatus.completed.value, "completed"),
        ("cancelled", "cancelled"),
        ("invented-secret-status", "unknown"),
    ],
)
def test_reader_normalizes_status_to_bounded_public_buckets(
    tmp_path: Path,
    persisted_status: str,
    expected_status: str,
) -> None:
    """Real Agno enum values and arbitrary persisted strings must map to a fixed lowercase vocabulary."""
    database = tmp_path / "code.db"
    run = _run()
    run["status"] = persisted_status
    _create_database(database, runs=[run])

    result = _read(_source(database))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert row.runs[0].status == expected_status


def test_missing_database_is_absent_without_creating_a_file(tmp_path: Path) -> None:
    """An absent source remains absent after a read attempt."""
    database = tmp_path / "missing.db"

    result = _read(_source(database))

    assert result == [UsageStorageDiagnostic(path_label="missing.db", status="absent", detail="database absent")]
    assert not database.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_read_preserves_durable_entries_and_allows_existing_wal_shm_coordination_updates(
    tmp_path: Path,
    journal_mode: str,
) -> None:
    """Durable SQLite entries stay stable; an existing WAL-index may coordinate readers."""
    database = tmp_path / f"{journal_mode}.db"
    _create_database(database)
    holder = sqlite3.connect(database)
    try:
        assert holder.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
        if journal_mode == "wal":
            holder.execute("PRAGMA wal_autocheckpoint=0")
            holder.execute(
                "INSERT INTO code_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "live-session",
                    "agent",
                    "agent-code",
                    None,
                    "@alice:example.test",
                    "{}",
                    "[]",
                    1,
                    1,
                ),
            )
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
                assert after[name][3] == snapshot[3]
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

    with _open_read_only_database(_source(database)) as connection, pytest.raises(sqlite3.OperationalError):
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
    tmp_path: Path,
    setup: str,
    expected_status: str,
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
def test_oversized_json_cell_returns_resource_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
) -> None:
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


def test_session_row_count_limit_stops_the_reader_with_a_bounded_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid but enormous session table must not create an unbounded reader stream."""
    database = tmp_path / "code.db"
    _create_database(database, row_count=3)
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_SESSION_ROWS_PER_SOURCE", 2)

    result = _read(_source(database))

    assert len(result) == 3
    assert all(isinstance(row, UsageSessionRow) for row in result[:2])
    assert result[2] == UsageStorageDiagnostic(
        path_label="code.db",
        status="resource_limit",
        detail="session row count exceeds limit",
    )


def test_selected_string_length_limit_rejects_unbounded_breakdown_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted identifiers used by grouping and dedup cannot retain arbitrary-length strings."""
    database = tmp_path / "code.db"
    run = _run()
    run["model"] = "oversized-model"
    _create_database(database, runs=[run])
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_EXTRACTED_STRING_LENGTH", 8)

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="resource_limit", detail="selected string exceeds limit"),
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
        UsageStorageDiagnostic(
            path_label="code.db",
            status="resource_limit",
            detail="nested response depth exceeds limit",
        ),
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


@pytest.mark.parametrize("column", ["runs", "session_data"])
def test_deep_ignored_json_returns_resource_limit(tmp_path: Path, column: str) -> None:
    """Ignored structures cannot exceed the parser nesting budget."""
    database = tmp_path / "code.db"
    _create_database(database)
    depth = 128
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            f"UPDATE code_sessions SET {column} = ?",  # noqa: S608 - parametrization is two literals.
            (_deep_ignored_json(column=column, depth=depth),),
        )
        connection.commit()
    finally:
        connection.close()

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="resource_limit", detail="JSON nesting exceeds limit"),
    ]


@pytest.mark.parametrize("column", ["runs", "session_data"])
def test_json_parser_recursion_error_returns_resource_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
) -> None:
    """The decoder contains a recursion escape even if its preflight limit is raised."""
    database = tmp_path / "code.db"
    _create_database(database)
    depth = 8
    payload = _deep_ignored_json(column=column, depth=depth)
    loads = usage_stats_storage.json.loads

    def recursion_error_for_payload(value: str) -> object:
        if value == payload:
            raise RecursionError
        return loads(value)

    monkeypatch.setattr(usage_stats_storage.json, "loads", recursion_error_for_payload)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            f"UPDATE code_sessions SET {column} = ?",  # noqa: S608 - parametrization is two literals.
            (payload,),
        )
        connection.commit()
    finally:
        connection.close()

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(path_label="code.db", status="resource_limit", detail="JSON nesting exceeds limit"),
    ]


def test_excessive_model_metric_records_return_resource_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retained per-model usage records have their own bounded budget."""
    database = tmp_path / "code.db"
    run = _run()
    metrics = run["metrics"]
    assert isinstance(metrics, dict)
    metrics["details"] = {
        "model": [
            {"id": "gpt-5.6", "provider": "openai"},
            {"id": "gpt-5.6-mini", "provider": "openai"},
        ],
    }
    _create_database(database, runs=[run])
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_EXTRACTED_MODEL_METRICS", 1)

    result = _read(_source(database))

    assert result == [
        UsageStorageDiagnostic(
            path_label="code.db",
            status="resource_limit",
            detail="model metric count exceeds limit",
        ),
    ]


def test_run_json_is_extracted_before_session_data_is_decoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader releases extracted run JSON before decoding session JSON."""
    database = tmp_path / "code.db"
    _create_database(database)
    events: list[str] = []
    decode = usage_stats_storage._decode_json_cell
    extract_runs = usage_stats_storage._extract_runs
    extract_session_metrics = usage_stats_storage._extract_session_metrics

    def tracked_decode(value: object, *, default: object) -> object:
        events.append("decode")
        return decode(value, default=default)

    def tracked_extract_runs(raw_runs: object) -> tuple[UsageRunNode, ...]:
        events.append("extract-runs")
        return extract_runs(raw_runs)

    def tracked_extract_session_metrics(raw_session_data: object) -> object:
        events.append("extract-session-data")
        return extract_session_metrics(raw_session_data)

    monkeypatch.setattr(usage_stats_storage, "_decode_json_cell", tracked_decode)
    monkeypatch.setattr(usage_stats_storage, "_extract_runs", tracked_extract_runs)
    monkeypatch.setattr(usage_stats_storage, "_extract_session_metrics", tracked_extract_session_metrics)

    result = _read(_source(database))

    assert isinstance(result[0], UsageSessionRow)
    assert events == ["decode", "extract-runs", "decode", "extract-session-data"]


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


def test_schema_validation_queries_only_the_expected_table_in_a_large_schema(tmp_path: Path) -> None:
    """Schema validation must not materialize every unrelated SQLite table name."""
    database = tmp_path / "code.db"
    _create_database(database)
    with sqlite3.connect(database) as connection:
        for index in range(100):
            connection.execute(f'CREATE TABLE "unrelated_{index}" (value TEXT)')
    statements: list[str] = []

    with sqlite3.connect(database) as connection:
        connection.set_trace_callback(statements.append)
        diagnostic = usage_stats_storage._validate_session_table(connection, _source(database))

    assert diagnostic is None
    schema_queries = [statement for statement in statements if "sqlite_master" in statement]
    assert len(schema_queries) == 1
    assert "name = 'code_sessions'" in schema_queries[0]
    assert "LIMIT 1" in schema_queries[0]


def test_empty_persisted_identities_are_treated_as_missing(tmp_path: Path) -> None:
    """Empty metadata identities cannot block requester fallback or become stable deduplication IDs."""
    database = tmp_path / "code.db"
    run = _run()
    run["run_id"] = ""
    run["user_id"] = "@alice:example.test"
    run["metadata"] = {"requester_id": ""}
    _create_database(database, runs=[run])

    result = _read(_source(database))

    assert len(result) == 1
    row = result[0]
    assert isinstance(row, UsageSessionRow)
    assert row.runs[0].requester_id == "@alice:example.test"
    assert row.runs[0].run_id is None


@pytest.mark.parametrize(
    ("table", "accepted"),
    [("123_sessions", True), ("bad-name_sessions", False), ("semi;drop_sessions", False)],
)
def test_reader_identifier_validation_matches_configured_entity_names(
    tmp_path: Path,
    table: str,
    accepted: bool,
) -> None:
    """Numeric-leading configured names work without admitting unsafe SQL identifiers."""
    database = tmp_path / "identifier.db"
    _create_database(database, table=table)

    result = _read(_source(database, table=table))

    if accepted:
        assert len(result) == 1
        assert isinstance(result[0], UsageSessionRow)
    else:
        assert result == [
            UsageStorageDiagnostic(
                path_label="identifier.db",
                status="unsupported_schema",
                detail="session table unavailable",
            ),
        ]


def test_discovery_self_uses_exact_runtime_database_and_team_sources(tmp_path: Path) -> None:
    """Self discovery cannot enumerate another requester's private agent database."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    alice = _identity(requester_id="@alice:example.test")
    bob = _identity(requester_id="@bob:example.test")
    alice_database = _private_database_path(runtime_paths, alice)
    bob_database = _private_database_path(runtime_paths, bob)
    _create_discovery_database(alice_database, table="code_sessions")
    _create_discovery_database(bob_database, table="code_sessions")
    session_root = runtime_paths.config_dir / "dedicated-sessions"
    configured_team_database = session_root / "teams" / "configured_store" / "sessions" / "configured_store.db"
    ad_hoc_team_database = session_root / "teams" / "ad_hoc_store" / "sessions" / "ad_hoc_store.db"
    _create_discovery_database(configured_team_database, table="configured_store_sessions")
    _create_discovery_database(ad_hoc_team_database, table="ad_hoc_store_sessions")

    sources = discover_self_usage_sources(
        agent_name="code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=alice,
    )

    assert _source_labels(sources) == [
        alice_database.resolve().relative_to(session_root.resolve()).as_posix(),
        "teams/ad_hoc_store/sessions/ad_hoc_store.db",
        "teams/configured_store/sessions/configured_store.db",
    ]
    private_source = sources[0]
    assert private_source.requester_isolated is True
    assert private_source.path == alice_database.resolve()
    assert all(source.path != bob_database.resolve() for source in sources)
    assert all(source.path.is_relative_to(session_root.resolve()) for source in sources)


def test_team_discovery_never_opens_sqlite_or_creates_wal_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery returns structural candidates without crossing the preflighted reader boundary."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    session_root = runtime_paths.config_dir / "dedicated-sessions"
    database = session_root / "teams" / "wal_team" / "sessions" / "wal_team.db"
    _create_discovery_database(database, table="wal_team_sessions")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
    finally:
        connection.close()
    database.with_name(f"{database.name}-wal").unlink(missing_ok=True)
    database.with_name(f"{database.name}-shm").unlink(missing_ok=True)
    before = _snapshot_database_entries(database)

    def fail_if_opened(_source: UsageStorageSource) -> Iterator[sqlite3.Connection]:
        message = "discovery opened SQLite"
        raise AssertionError(message)

    def fail_if_connected(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        message = "discovery connected to SQLite"
        raise AssertionError(message)

    monkeypatch.setattr(usage_stats_storage, "_open_read_only_database", fail_if_opened)
    monkeypatch.setattr(usage_stats_storage.sqlite3, "connect", fail_if_connected)

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert "teams/wal_team/sessions/wal_team.db" in _source_labels(sources)
    assert _snapshot_database_entries(database) == before


def test_unreadable_structural_team_candidate_reaches_reader_diagnostics(tmp_path: Path) -> None:
    """A corrupt team source is discovered so the reader can report bounded partial coverage."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    session_root = runtime_paths.config_dir / "dedicated-sessions"
    database = session_root / "teams" / "corrupt_team" / "sessions" / "corrupt_team.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)
    source = next(source for source in sources if source.path == database.resolve())

    assert _read(source) == [
        UsageStorageDiagnostic(
            path_label="teams/corrupt_team/sessions/corrupt_team.db",
            status="corrupt",
            detail="database header invalid",
        ),
    ]


def test_discovery_admin_uses_fixed_safe_layouts_and_current_config_attribution(tmp_path: Path) -> None:
    """Admin discovery accepts only configured agent layouts and valid fixed-depth team databases."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    session_root = runtime_paths.config_dir / "dedicated-sessions"
    shared_database = session_root / "agents" / "shared" / "sessions" / "shared.db"
    _create_discovery_database(shared_database, table="shared_sessions")
    removed_database = session_root / "agents" / "removed" / "sessions" / "removed.db"
    _create_discovery_database(removed_database, table="removed_sessions")
    linked_target = session_root / "outside.db"
    _create_discovery_database(linked_target, table="linked_sessions")
    linked_database = session_root / "agents" / "linked" / "sessions" / "linked.db"
    linked_database.parent.mkdir(parents=True)
    linked_database.symlink_to(linked_target)

    alice = _identity(requester_id="@alice:example.test")
    bob = _identity(requester_id="@bob:example.test")
    alice_database = _private_database_path(runtime_paths, alice)
    bob_database = _private_database_path(runtime_paths, bob)
    _create_discovery_database(alice_database, table="code_sessions")
    _create_discovery_database(bob_database, table="code_sessions")
    bob_worker_key = resolve_worker_key("user", bob, agent_name="code")
    assert bob_worker_key is not None
    raw_worker_database = session_root / "private_instances" / bob_worker_key / "code" / "sessions" / "code.db"
    _create_discovery_database(raw_worker_database, table="code_sessions")
    charlie = _identity(requester_id="@charlie:example.test")
    charlie_worker_key = resolve_worker_key("user", charlie, agent_name="code")
    assert charlie_worker_key is not None
    symlinked_private_instance = session_root / "private_instances" / worker_dir_name(charlie_worker_key)
    symlinked_private_instance.parent.mkdir(parents=True, exist_ok=True)
    symlinked_private_instance.symlink_to(alice_database.parents[2], target_is_directory=True)

    configured_team_database = session_root / "teams" / "configured_store" / "sessions" / "configured_store.db"
    ad_hoc_team_database = session_root / "teams" / "ad_hoc_store" / "sessions" / "ad_hoc_store.db"
    invalid_team_database = session_root / "teams" / "wrong_table" / "sessions" / "wrong_table.db"
    _create_discovery_database(configured_team_database, table="configured_store_sessions")
    _create_discovery_database(ad_hoc_team_database, table="ad_hoc_store_sessions")
    _create_discovery_database(invalid_team_database, table="other_sessions")

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert _source_labels(sources) == [
        "agents/absent/sessions/absent.db",
        "agents/shared/sessions/shared.db",
        alice_database.resolve().relative_to(session_root.resolve()).as_posix(),
        bob_database.resolve().relative_to(session_root.resolve()).as_posix(),
        "teams/ad_hoc_store/sessions/ad_hoc_store.db",
        "teams/configured_store/sessions/configured_store.db",
        "teams/wrong_table/sessions/wrong_table.db",
    ]
    assert all(not source.path.is_symlink() for source in sources)
    assert all(source.path.is_relative_to(session_root.resolve()) for source in sources)
    assert sources[0].requester_isolated is False
    assert sources[2].requester_isolated is True
    assert all(source.source_agent_id != "removed" for source in sources)
    assert all(bob_worker_key not in source.path_label for source in sources)


def test_admin_discovery_turns_directory_errors_into_bounded_partial_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private-root permission or removal race must not discard independently discoverable sources."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    private_root = runtime_paths.config_dir / "dedicated-sessions" / "private_instances"
    private_root.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def guarded_iterdir(path: Path) -> Iterator[Path]:
        if path == private_root:
            raise PermissionError
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    outcomes = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert any(isinstance(outcome, UsageStorageSource) for outcome in outcomes)
    assert (
        UsageStorageDiagnostic(
            path_label="private_instances",
            status="partial",
            detail="source discovery unavailable",
        )
        in outcomes
    )


def test_discovery_directory_entry_limit_returns_resource_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory enumeration has a hard request-independent entry bound."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    teams_root = runtime_paths.config_dir / "dedicated-sessions" / "teams"
    (teams_root / "one").mkdir(parents=True)
    (teams_root / "two").mkdir()
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_DISCOVERY_DIRECTORY_ENTRIES", 1)

    outcomes = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert (
        UsageStorageDiagnostic(
            path_label="teams",
            status="resource_limit",
            detail="source discovery entry count exceeds limit",
        )
        in outcomes
    )


def test_discovery_candidate_limit_bounds_fixed_path_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate validation remains bounded even when directory entries are independently valid."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    teams_root = runtime_paths.config_dir / "dedicated-sessions" / "teams"
    (teams_root / "one").mkdir(parents=True)
    (teams_root / "two").mkdir()
    monkeypatch.setattr("mindroom.usage_stats_storage.MAX_DISCOVERY_CANDIDATES", 1)

    outcomes = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert (
        UsageStorageDiagnostic(
            path_label="teams",
            status="resource_limit",
            detail="source candidate count exceeds limit",
        )
        in outcomes
    )


def test_self_discovery_does_not_reconcile_private_workspace_links(tmp_path: Path) -> None:
    """Read-only usage discovery must not invoke normal runtime workspace reconciliation."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    identity = _identity(requester_id="@alice:example.test")
    worker_key = resolve_worker_key("user", identity, agent_name="code")
    assert worker_key is not None
    workspace = runtime_paths.storage_root / "private_instances" / worker_dir_name(worker_key) / "code" / "code_data"
    stale_target = workspace / "stale-target"
    stale_target.mkdir(parents=True)
    stale_link = workspace / "knowledge" / "stale"
    stale_link.parent.mkdir()
    stale_link.symlink_to(stale_target, target_is_directory=True)

    discover_self_usage_sources(
        agent_name="code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )

    assert stale_link.is_symlink()
    assert stale_link.resolve() == stale_target.resolve()


def test_discovery_does_not_scan_canonical_state_root_when_sessions_are_redirected(tmp_path: Path) -> None:
    """An explicit session root is the sole discovery root for every source shape."""
    config = _discovery_config()
    runtime_paths = _discovery_runtime_paths(tmp_path)
    canonical_database = runtime_paths.storage_root / "agents" / "shared" / "sessions" / "shared.db"
    _create_discovery_database(canonical_database, table="shared_sessions")

    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)

    assert _source_labels(sources) == [
        "agents/absent/sessions/absent.db",
        "agents/linked/sessions/linked.db",
        "agents/shared/sessions/shared.db",
    ]
    assert all(source.path != canonical_database.resolve() for source in sources)
