"""Small read-only adapter for retained Agno SQLite sessions."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from mindroom.constants import RuntimePaths, resolve_session_state_root
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import worker_dir_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = [
    "UsageRunNode",
    "UsageSessionRow",
    "UsageStorageDiagnostic",
    "UsageStorageSource",
    "discover_admin_usage_sources",
    "discover_self_usage_sources",
    "iter_usage_storage_rows",
]

type _UsageStorageScope = Literal["shared_agent", "private_agent", "team"]
type _RunStatus = Literal["pending", "running", "completed", "paused", "cancelled", "error", "unknown"]
type _MetricValue = int | float | str | None

_MAX_JSON_BYTES = 1_000_000
_MAX_ROWS_PER_SOURCE = 10_000
_MAX_DIRECTORY_ENTRIES = 1_000
_MAX_CANDIDATES = 10_000
_MAX_SOURCES = 1_000
_MAX_STRING_LENGTH = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+\Z")
_WORKER_DIRECTORY = re.compile(r"[A-Za-z0-9._@+-]+-[0-9a-f]{16}\Z")
_RUN_STATUSES = frozenset({"pending", "running", "completed", "paused", "cancelled", "error"})
_TOKEN_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "audio_total_tokens",
    },
)
_REQUIRED_COLUMNS = frozenset({"session_id", "session_type", "agent_id", "team_id", "user_id", "runs"})


@dataclass(frozen=True, slots=True)
class UsageStorageSource:
    """One fixed-layout Agno session database."""

    path: Path
    path_label: str
    scope: _UsageStorageScope
    expected_session_table: str
    source_agent_id: str | None
    allowed_agent_ids: frozenset[str]
    allowed_team_ids: frozenset[str]
    requester_isolated: bool


@dataclass(frozen=True, slots=True)
class UsageRunNode:
    """Usage fields from one top-level retained run."""

    team_id: str | None
    requester_id: str | None
    created_at: str | None
    model_provider: str | None
    model_id: str | None
    run_id: str | None
    status: _RunStatus
    metrics: Mapping[str, _MetricValue]


@dataclass(frozen=True, slots=True)
class UsageSessionRow:
    """Top-level usage runs from one retained Agno session."""

    source: UsageStorageSource
    entity_id: str
    entity_kind: Literal["agent", "team"]
    row_key: str
    runs: tuple[UsageRunNode, ...]


@dataclass(frozen=True, slots=True)
class UsageStorageDiagnostic:
    """Content-free outcome for a source that could not be read."""

    path_label: str
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "resource_limit", "partial"]
    detail: str


@contextmanager
def _open_read_only_database(source: UsageStorageSource) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"{source.path.resolve().as_uri()}?mode=ro&cache=private",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def discover_self_usage_sources(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> tuple[UsageStorageSource | UsageStorageDiagnostic, ...]:
    """Return only the current execution's resolved agent database."""
    session_root = _session_root(runtime_paths)
    resolved = resolve_agent_storage(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    database = resolved.session_state_root / "sessions" / f"{agent_name}.db"
    if resolved.execution.is_private:
        worker_key = resolved.execution.worker_key
        if worker_key is None:
            return (_diagnostic("self", "partial", "source discovery unavailable"),)
        relative = (
            Path("private_instances") / worker_dir_name(worker_key) / agent_name / "sessions" / f"{agent_name}.db"
        )
        scope: _UsageStorageScope = "private_agent"
    else:
        relative = Path("agents") / agent_name / "sessions" / f"{agent_name}.db"
        scope = "shared_agent"
    candidate = _safe_candidate(session_root, relative)
    if candidate is None or candidate != database.expanduser().resolve():
        return (_diagnostic("self", "partial", "source discovery unavailable"),)
    return (
        _source(
            path=candidate,
            root=session_root,
            scope=scope,
            table=f"{agent_name}_sessions",
            agent_name=agent_name,
            config=config,
            requester_isolated=resolved.execution.is_private,
        ),
    )


def discover_admin_usage_sources(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[UsageStorageSource | UsageStorageDiagnostic, ...]:
    """Find configured shared agents plus existing private and team databases."""
    root = _session_root(runtime_paths)
    sources = _shared_agent_sources(root, config)
    sources.extend(_private_agent_sources(root, config))
    sources.extend(_team_sources(root, config))
    return tuple(sorted(sources[:_MAX_SOURCES], key=lambda item: item.path_label))


def _session_root(runtime_paths: RuntimePaths) -> Path:
    return resolve_session_state_root(runtime_paths.storage_root, runtime_paths).expanduser().resolve()


def _shared_agent_sources(
    root: Path,
    config: Config,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for agent_name, agent_config in config.agents.items():
        if len(sources) >= _MAX_SOURCES:
            break
        if agent_config.private is not None:
            continue
        candidate = _safe_candidate(root, Path("agents") / agent_name / "sessions" / f"{agent_name}.db")
        if candidate is None:
            continue
        sources.append(
            _source(
                path=candidate,
                root=root,
                scope="shared_agent",
                table=f"{agent_name}_sessions",
                agent_name=agent_name,
                config=config,
                requester_isolated=False,
            ),
        )
    return sources


def _private_agent_sources(
    root: Path,
    config: Config,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    private_root = root / "private_instances"
    entries = _directory_entries(private_root)
    if entries is None:
        return []
    private_agents = tuple(name for name, agent in config.agents.items() if agent.private is not None)
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    candidates = 0
    for worker_directory in entries:
        if worker_directory.is_symlink() or not worker_directory.is_dir():
            continue
        if _WORKER_DIRECTORY.fullmatch(worker_directory.name) is None:
            continue
        for agent_name in private_agents:
            candidates += 1
            if candidates > _MAX_CANDIDATES or len(sources) >= _MAX_SOURCES:
                return sources
            relative = Path("private_instances") / worker_directory.name / agent_name / "sessions" / f"{agent_name}.db"
            candidate = _safe_candidate(root, relative)
            if candidate is None or not candidate.is_file():
                continue
            sources.append(
                _source(
                    path=candidate,
                    root=root,
                    scope="private_agent",
                    table=f"{agent_name}_sessions",
                    agent_name=agent_name,
                    config=config,
                    requester_isolated=True,
                ),
            )
    return sources


def _team_sources(
    root: Path,
    config: Config,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    entries = _directory_entries(root / "teams")
    if entries is None:
        return []
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for directory in entries:
        storage_name = directory.name
        if len(sources) >= _MAX_SOURCES:
            break
        if directory.is_symlink() or not directory.is_dir() or _IDENTIFIER.fullmatch(storage_name) is None:
            continue
        candidate = _safe_candidate(root, Path("teams") / storage_name / "sessions" / f"{storage_name}.db")
        if candidate is None or not candidate.is_file():
            continue
        sources.append(
            _source(
                path=candidate,
                root=root,
                scope="team",
                table=f"{storage_name}_sessions",
                agent_name=None,
                config=config,
                requester_isolated=False,
            ),
        )
    return sources


def _directory_entries(path: Path) -> tuple[Path, ...] | None:
    if path.is_symlink() or not path.is_dir():
        return None
    try:
        entries = tuple(sorted(path.iterdir(), key=lambda entry: entry.name))
    except OSError:
        return None
    return entries[:_MAX_DIRECTORY_ENTRIES]


def _safe_candidate(root: Path, relative: Path) -> Path | None:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            return None
    candidate = root / relative
    if candidate.is_symlink():
        return None
    resolved = candidate.resolve()
    return resolved if resolved.is_relative_to(root) else None


def _source(
    *,
    path: Path,
    root: Path,
    scope: _UsageStorageScope,
    table: str,
    agent_name: str | None,
    config: Config,
    requester_isolated: bool,
) -> UsageStorageSource:
    return UsageStorageSource(
        path=path,
        path_label=path.relative_to(root).as_posix(),
        scope=scope,
        expected_session_table=table,
        source_agent_id=agent_name,
        allowed_agent_ids=frozenset(config.agents),
        allowed_team_ids=frozenset(config.teams),
        requester_isolated=requester_isolated,
    )


def iter_usage_storage_rows(source: UsageStorageSource) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
    """Yield top-level usage fields without writing or creating a database."""
    if not source.path.is_file():
        yield _source_diagnostic(source, "absent", "database absent")
        return
    try:
        with _open_read_only_database(source) as connection:
            schema_error = _validate_schema(connection, source)
            if schema_error is not None:
                yield schema_error
                return
            table = _quote_identifier(source.expected_session_table)
            query = (
                "SELECT session_id, session_type, agent_id, team_id, user_id, "  # noqa: S608
                "CASE WHEN length(CAST(runs AS BLOB)) <= ? THEN runs END AS runs, "
                "CASE WHEN length(CAST(runs AS BLOB)) > ? THEN 1 ELSE 0 END AS too_large "
                f"FROM {table} LIMIT ?"
            )
            for row_index, row in enumerate(
                connection.execute(query, (_MAX_JSON_BYTES, _MAX_JSON_BYTES, _MAX_ROWS_PER_SOURCE + 1)),
            ):
                if row_index >= _MAX_ROWS_PER_SOURCE:
                    yield _source_diagnostic(source, "resource_limit", "session row limit exceeded")
                    return
                if row["too_large"]:
                    yield _source_diagnostic(source, "resource_limit", "runs payload too large")
                    continue
                try:
                    yield _extract_row(source, row)
                except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
                    yield _source_diagnostic(source, "partial", "malformed retained session")
    except sqlite3.Error as error:
        yield _sqlite_diagnostic(source, error)
    except OSError:
        yield _source_diagnostic(source, "partial", "database unavailable")


def _validate_schema(
    connection: sqlite3.Connection,
    source: UsageStorageSource,
) -> UsageStorageDiagnostic | None:
    table = source.expected_session_table
    if _IDENTIFIER.fullmatch(table) is None:
        return _source_diagnostic(source, "unsupported_schema", "session table unavailable")
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return _source_diagnostic(source, "unsupported_schema", "session table unavailable")
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
    if not _REQUIRED_COLUMNS.issubset(columns):
        return _source_diagnostic(source, "unsupported_schema", "session schema unsupported")
    return None


def _extract_row(source: UsageStorageSource, row: sqlite3.Row) -> UsageSessionRow:
    entity_kind = row["session_type"]
    if entity_kind not in {"agent", "team"}:
        raise ValueError
    entity_id = row["agent_id"] if entity_kind == "agent" else row["team_id"]
    row_key = row["session_id"]
    row_requester = _optional_string(row["user_id"])
    if not isinstance(entity_id, str) or not entity_id or not isinstance(row_key, str) or not row_key:
        raise ValueError
    raw_runs = json.loads(row["runs"] or "[]")
    if not isinstance(raw_runs, list):
        raise TypeError
    return UsageSessionRow(
        source=source,
        entity_id=_bounded_string(entity_id),
        entity_kind=cast("Literal['agent', 'team']", entity_kind),
        row_key=_bounded_string(row_key),
        runs=tuple(_extract_run(raw_run, row_requester=row_requester) for raw_run in raw_runs),
    )


def _extract_run(raw_run: object, *, row_requester: str | None) -> UsageRunNode:
    if not isinstance(raw_run, dict):
        raise TypeError
    run = cast("dict[str, object]", raw_run)
    metadata = run.get("metadata")
    metadata_requester = (
        _optional_string(cast("dict[str, object]", metadata).get("requester_id"))
        if isinstance(metadata, dict)
        else None
    )
    metrics = run.get("metrics", {})
    if not isinstance(metrics, dict):
        raise TypeError
    selected_metrics: dict[str, _MetricValue] = {}
    for field in _TOKEN_FIELDS:
        value = cast("dict[str, object]", metrics).get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float, str, type(None))):
            raise TypeError
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        if value is not None:
            selected_metrics[field] = value
    return UsageRunNode(
        team_id=_optional_string(run.get("team_id")),
        requester_id=metadata_requester or _optional_string(run.get("user_id")) or row_requester,
        created_at=_timestamp_string(run.get("created_at")),
        model_provider=_optional_string(run.get("model_provider")),
        model_id=_optional_string(run.get("model")),
        run_id=_optional_string(run.get("run_id")),
        status=_status(run.get("status")),
        metrics=MappingProxyType(selected_metrics),
    )


def _optional_string(value: object) -> str | None:
    return _bounded_string(value) if isinstance(value, str) and value else None


def _bounded_string(value: str) -> str:
    if len(value) > _MAX_STRING_LENGTH:
        raise ValueError
    return value


def _timestamp_string(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return _bounded_string(str(value))


def _status(value: object) -> _RunStatus:
    normalized = value.casefold() if isinstance(value, str) else "unknown"
    return cast("_RunStatus", normalized) if normalized in _RUN_STATUSES else "unknown"


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _diagnostic(
    path_label: str,
    status: Literal["partial"],
    detail: str,
) -> UsageStorageDiagnostic:
    return UsageStorageDiagnostic(path_label=path_label, status=status, detail=detail)


def _source_diagnostic(
    source: UsageStorageSource,
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "resource_limit", "partial"],
    detail: str,
) -> UsageStorageDiagnostic:
    return UsageStorageDiagnostic(path_label=source.path_label, status=status, detail=detail)


def _sqlite_diagnostic(source: UsageStorageSource, error: sqlite3.Error) -> UsageStorageDiagnostic:
    message = str(error).lower()
    if "locked" in message or "busy" in message:
        return _source_diagnostic(source, "busy", "database busy")
    if "malformed" in message or "not a database" in message:
        return _source_diagnostic(source, "corrupt", "database corrupt")
    return _source_diagnostic(source, "partial", "database unavailable")
