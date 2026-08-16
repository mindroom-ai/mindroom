"""Bounded, non-mutating reads of persisted Agno session usage."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

UsageStorageScope = Literal["shared_agent", "private_agent", "team"]
UsageMetricValue = int | float | str | None

MAX_JSON_BYTES = 1_000_000
MAX_NESTED_RESPONSE_DEPTH = 16
MAX_EXTRACTED_RUN_NODES = 1_000
_NESTED_RESPONSE_DEPTH_LIMIT = "nested response depth exceeds limit"
_RUN_NODE_COUNT_LIMIT = "run node count exceeds limit"

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REQUIRED_SESSION_COLUMNS = frozenset(
    {
        "session_id",
        "session_type",
        "agent_id",
        "team_id",
        "user_id",
        "session_data",
        "runs",
        "created_at",
        "updated_at",
    },
)
_METRIC_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "audio_total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost",
    },
)


@dataclass(frozen=True, slots=True)
class UsageStorageSource:
    """One trusted persisted-session database selected by source discovery."""

    path: Path
    path_label: str
    scope: UsageStorageScope
    expected_session_table: str
    source_agent_id: str | None
    allowed_agent_ids: frozenset[str]
    allowed_team_ids: frozenset[str]
    requester_isolated: bool


@dataclass(frozen=True, slots=True)
class UsageModelMetric:
    """Persisted usage metrics attributed to one model role."""

    model_type: str
    provider: str
    model_id: str
    metrics: Mapping[str, UsageMetricValue]


@dataclass(frozen=True, slots=True)
class UsageRunNode:
    """Field-selective persisted run with recursively retained team members."""

    agent_id: str | None
    team_id: str | None
    requester_id: str | None
    created_at: str | None
    model_provider: str | None
    model_id: str | None
    run_id: str | None
    status: str
    metrics: Mapping[str, UsageMetricValue]
    model_metrics: tuple[UsageModelMetric, ...]
    member_responses: tuple[UsageRunNode, ...]


@dataclass(frozen=True, slots=True)
class UsageSessionRow:
    """One field-selective persisted Agno session row."""

    source: UsageStorageSource
    entity_id: str
    entity_kind: Literal["agent", "team"]
    row_key: str
    session_user_id: str | None
    session_metrics: Mapping[str, UsageMetricValue] | None
    runs: tuple[UsageRunNode, ...]


@dataclass(frozen=True, slots=True)
class UsageStorageDiagnostic:
    """A bounded internal storage-read outcome without persisted content."""

    path_label: str
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "resource_limit", "partial"]
    detail: str


class _ResourceLimitError(ValueError):
    """A persisted value exceeded a reader allocation limit."""


@contextmanager
def _open_read_only_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database with no write-capable connection path."""
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&cache=private",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def iter_usage_storage_rows(source: UsageStorageSource) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
    """Yield field-selective session rows or source-safe diagnostics one at a time."""
    header_diagnostic = _preflight_database(source)
    if header_diagnostic is not None:
        yield header_diagnostic
        return

    try:
        with _open_read_only_database(source.path) as connection:
            table_diagnostic = _validate_session_table(connection, source)
            if table_diagnostic is not None:
                yield table_diagnostic
                return

            table_name = _quote_identifier(source.expected_session_table)
            query = """
                SELECT
                    session_id, session_type, agent_id, team_id, user_id, created_at,
                    CASE WHEN runs IS NULL OR length(CAST(runs AS BLOB)) <= ? THEN runs END AS runs,
                    CASE WHEN session_data IS NULL OR length(CAST(session_data AS BLOB)) <= ?
                        THEN session_data
                    END AS session_data,
                    CASE WHEN runs IS NOT NULL AND length(CAST(runs AS BLOB)) > ? THEN 1 ELSE 0 END
                        AS runs_over_limit,
                    CASE WHEN session_data IS NOT NULL AND length(CAST(session_data AS BLOB)) > ? THEN 1 ELSE 0 END
                        AS session_data_over_limit
                FROM
                """
            query += table_name
            cursor = connection.execute(
                query,
                (MAX_JSON_BYTES, MAX_JSON_BYTES, MAX_JSON_BYTES, MAX_JSON_BYTES),
            )
            while row := cursor.fetchone():
                if row["runs_over_limit"]:
                    yield _diagnostic(source, "resource_limit", "runs exceeds limit")
                    continue
                if row["session_data_over_limit"]:
                    yield _diagnostic(source, "resource_limit", "session_data exceeds limit")
                    continue
                try:
                    yield _extract_session_row(source, row)
                except _ResourceLimitError as error:
                    yield _diagnostic(source, "resource_limit", str(error))
                except (TypeError, ValueError, json.JSONDecodeError):
                    yield _diagnostic(source, "partial", "malformed session row")
    except sqlite3.Error as error:
        yield _sqlite_diagnostic(source, error)
    except OSError:
        yield _diagnostic(source, "partial", "database unavailable")


def _preflight_database(source: UsageStorageSource) -> UsageStorageDiagnostic | None:  # noqa: PLR0911
    """Check the SQLite header and WAL sidecars without involving SQLite."""
    path = source.path
    if not path.exists():
        return _diagnostic(source, "absent", "database absent")
    try:
        with path.open("rb") as database_file:
            header = database_file.read(100)
    except OSError:
        return _diagnostic(source, "partial", "database unavailable")
    if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
        return _diagnostic(source, "corrupt", "database header invalid")
    if header[18] != 2 and header[19] != 2:
        return None
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        if sidecar.is_symlink() or not sidecar.is_file():
            return _diagnostic(source, "partial", "WAL sidecars unavailable")
        try:
            with sidecar.open("rb") as sidecar_file:
                sidecar_file.read(1)
        except OSError:
            return _diagnostic(source, "partial", "WAL sidecars unavailable")
    return None


def _validate_session_table(
    connection: sqlite3.Connection, source: UsageStorageSource,
) -> UsageStorageDiagnostic | None:
    """Confirm the one expected Agno table and its required columns."""
    table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    expected = source.expected_session_table
    if not _IDENTIFIER.fullmatch(expected) or expected not in table_names:
        return _diagnostic(source, "unsupported_schema", "session table unavailable")
    quoted_table = _quote_identifier(expected)
    column_names = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")}
    if not _REQUIRED_SESSION_COLUMNS.issubset(column_names):
        return _diagnostic(source, "unsupported_schema", "session schema unsupported")
    return None


def _extract_session_row(source: UsageStorageSource, row: sqlite3.Row) -> UsageSessionRow:
    """Decode one bounded database row and immediately discard its raw JSON values."""
    entity_kind = row["session_type"]
    if entity_kind not in {"agent", "team"}:
        raise ValueError
    entity_id = row["agent_id"] if entity_kind == "agent" else row["team_id"]
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError
    row_key = row["session_id"]
    if not isinstance(row_key, str) or not row_key:
        raise ValueError

    raw_runs = _decode_json_cell(row["runs"], default=[])
    raw_session_data = _decode_json_cell(row["session_data"], default=None)
    runs = _extract_runs(raw_runs)
    session_metrics = _extract_session_metrics(raw_session_data)
    return UsageSessionRow(
        source=source,
        entity_id=entity_id,
        entity_kind=entity_kind,
        row_key=row_key,
        session_user_id=_optional_string(row["user_id"]),
        session_metrics=session_metrics,
        runs=runs,
    )


def _decode_json_cell(value: object, *, default: object) -> object:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TypeError
    return json.loads(value)


def _extract_session_metrics(session_data: object) -> Mapping[str, UsageMetricValue] | None:
    if session_data is None:
        return None
    if not isinstance(session_data, dict):
        raise TypeError
    metrics = session_data.get("session_metrics")
    if metrics is None:
        return None
    return _extract_metric_values(metrics)


def _extract_runs(raw_runs: object) -> tuple[UsageRunNode, ...]:
    if not isinstance(raw_runs, list):
        raise TypeError
    extracted_nodes = [0]
    return tuple(_extract_run_node(run, depth=0, extracted_nodes=extracted_nodes) for run in raw_runs)


def _extract_run_node(raw_run: object, *, depth: int, extracted_nodes: list[int]) -> UsageRunNode:
    if depth > MAX_NESTED_RESPONSE_DEPTH:
        raise _ResourceLimitError(_NESTED_RESPONSE_DEPTH_LIMIT)
    if not isinstance(raw_run, dict):
        raise TypeError
    extracted_nodes[0] += 1
    if extracted_nodes[0] > MAX_EXTRACTED_RUN_NODES:
        raise _ResourceLimitError(_RUN_NODE_COUNT_LIMIT)

    metadata = raw_run.get("metadata")
    requester_id = _optional_string(metadata.get("requester_id")) if isinstance(metadata, dict) else None
    if requester_id is None:
        requester_id = _optional_string(raw_run.get("user_id"))
    nested = raw_run.get("member_responses", [])
    if not isinstance(nested, list):
        raise TypeError
    return UsageRunNode(
        agent_id=_optional_string(raw_run.get("agent_id")),
        team_id=_optional_string(raw_run.get("team_id")),
        requester_id=requester_id,
        created_at=_timestamp_string(raw_run.get("created_at")),
        model_provider=_optional_string(raw_run.get("model_provider")),
        model_id=_optional_string(raw_run.get("model")),
        run_id=_optional_string(raw_run.get("run_id")),
        status=_optional_string(raw_run.get("status")) or "unknown",
        metrics=_extract_metric_values(raw_run.get("metrics", {})),
        model_metrics=_extract_model_metrics(raw_run.get("metrics", {})),
        member_responses=tuple(
            _extract_run_node(response, depth=depth + 1, extracted_nodes=extracted_nodes) for response in nested
        ),
    )


def _extract_metric_values(raw_metrics: object) -> Mapping[str, UsageMetricValue]:
    if not isinstance(raw_metrics, dict):
        raise TypeError
    metrics: dict[str, UsageMetricValue] = {}
    for field in _METRIC_FIELDS:
        if field not in raw_metrics:
            continue
        value = raw_metrics[field]
        if isinstance(value, bool) or not isinstance(value, (int, float, str, type(None))):
            raise TypeError
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        metrics[field] = value
    return MappingProxyType(metrics)


def _extract_model_metrics(raw_metrics: object) -> tuple[UsageModelMetric, ...]:
    if not isinstance(raw_metrics, dict):
        raise TypeError
    details = raw_metrics.get("details")
    if details is None:
        return ()
    if not isinstance(details, dict):
        raise TypeError
    metrics: list[UsageModelMetric] = []
    for model_type, model_entries in details.items():
        if not isinstance(model_type, str) or not isinstance(model_entries, list):
            raise TypeError
        for entry in model_entries:
            if not isinstance(entry, dict):
                raise TypeError
            model_id = entry.get("id", "")
            provider = entry.get("provider", "")
            if not isinstance(model_id, str) or not isinstance(provider, str):
                raise TypeError
            metrics.append(
                UsageModelMetric(
                    model_type=model_type,
                    provider=provider,
                    model_id=model_id,
                    metrics=_extract_metric_values(entry),
                ),
            )
    return tuple(metrics)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _timestamp_string(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError
    return str(value)


def _quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace('"', '""')}"'


def _diagnostic(
    source: UsageStorageSource,
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "resource_limit", "partial"],
    detail: str,
) -> UsageStorageDiagnostic:
    return UsageStorageDiagnostic(path_label=source.path_label, status=status, detail=detail)


def _sqlite_diagnostic(source: UsageStorageSource, error: sqlite3.Error) -> UsageStorageDiagnostic:
    message = str(error).lower()
    if "locked" in message or "busy" in message:
        return _diagnostic(source, "busy", "database busy")
    if "malformed" in message or "not a database" in message or "file is not a database" in message:
        return _diagnostic(source, "corrupt", "database corrupt")
    return _diagnostic(source, "partial", "database unavailable")
