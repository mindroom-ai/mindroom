"""Small read-only adapter for retained Agno SQLite sessions."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from mindroom.constants import RuntimePaths, resolve_session_state_root
from mindroom.legacy_private_storage_aliases import is_verified_private_instance_alias
from mindroom.legacy_session_storage import (
    decode_persisted_session_json,
    legacy_session_runs_projection,
    merge_legacy_run_payloads,
)
from mindroom.private_instance_identity import PrivateInstanceIdentityError, load_private_instance_identity
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import build_tool_execution_identity, worker_dir_name
from mindroom.usage_tokens import TOKEN_FIELDS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = [
    "TOKEN_FIELDS",
    "UsageModelMetrics",
    "UsageRunNode",
    "UsageSessionRow",
    "UsageStorageDiagnostic",
    "UsageStorageSource",
    "discover_admin_usage_sources",
    "discover_private_usage_sources",
    "discover_self_usage_sources",
    "iter_usage_storage_rows",
]

type _UsageStorageScope = Literal["shared_agent", "private_agent", "team"]
type _UsageReadMode = Literal["runs", "session_metrics", "both"]
type _MetricValue = int | float | str | None

_MAX_STRING_LENGTH = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+\Z")
_WORKER_DIRECTORY = re.compile(r"[A-Za-z0-9._@+-]+-[0-9a-f]{16}\Z")
_REQUIRED_COLUMNS = frozenset(
    {"session_id", "session_type", "agent_id", "team_id", "user_id", "session_data"},
)
# Agno 3 keeps one row per run in ``<session table>_runs``. Until background
# migration retires an agno 2.x ``runs`` blob (or when migration refuses a bad
# blob), this reader merges blob and rows the same way as agno.
_RUNS_TABLE_REQUIRED_COLUMNS = frozenset({"run_id", "session_id", "run_index", "run_data", "created_at"})


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
    owner_id: str | None = None


@dataclass(frozen=True, slots=True)
class UsageModelMetrics:
    """Token counters attributed to one model within a retained run."""

    model_provider: str | None
    model: str | None
    metrics: Mapping[str, _MetricValue]


@dataclass(frozen=True, slots=True)
class UsageRunNode:
    """Usage fields from one top-level retained run."""

    team_id: str | None
    requester_id: str | None
    run_id: str | None
    model_provider: str | None
    model: str | None
    metrics: Mapping[str, _MetricValue]
    created_at: int | float | None = None
    # Empty means no detailed attribution was stored; None means it was unusable.
    model_metrics: tuple[UsageModelMetrics, ...] | None = ()


@dataclass(frozen=True, slots=True)
class UsageSessionRow:
    """Top-level usage runs from one retained Agno session."""

    source: UsageStorageSource
    entity_id: str
    entity_kind: Literal["agent", "team"]
    row_key: str
    runs: tuple[UsageRunNode, ...]
    session_metrics: Mapping[str, _MetricValue] = field(default_factory=lambda: MappingProxyType({}))
    requester_id: str | None = None
    payload_bytes: int = 0
    runs_available: bool = True
    session_metrics_available: bool = True


@dataclass(frozen=True, slots=True)
class UsageStorageDiagnostic:
    """Content-free outcome for a source that could not be read."""

    path_label: str
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "partial"]
    detail: str
    scope: _UsageStorageScope | None = None


@contextmanager
def _open_read_only_database(source: UsageStorageSource) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"{source.path.resolve().as_uri()}?mode=ro&cache=private",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
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
            owner_id=execution_identity.requester_id if resolved.execution.is_private else None,
        ),
    )


def discover_private_usage_sources(
    *,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[UsageStorageSource | UsageStorageDiagnostic, ...]:
    """Resolve existing private databases for this canonical requester and known aliases only."""
    canonical = resolve_human_requester_alias(requester_id, config, runtime_paths)
    requester_ids = {canonical}
    requester_ids.update(
        alias
        for aliases in config.authorization.aliases.values()
        for alias in aliases
        if resolve_human_requester_alias(alias, config, runtime_paths) == canonical
    )
    sources: dict[str, UsageStorageSource | UsageStorageDiagnostic] = {}
    for agent_name, agent_config in config.agents.items():
        if agent_config.private is None:
            continue
        for user_id in sorted(requester_ids):
            identity = build_tool_execution_identity(
                channel="matrix",
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                requester_id=user_id,
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
                session_id=None,
            )
            try:
                agent_sources = discover_self_usage_sources(
                    agent_name=agent_name,
                    config=config,
                    runtime_paths=runtime_paths,
                    execution_identity=identity,
                )
            except (OSError, ValueError):
                agent_sources = (_diagnostic("self", "partial", "source discovery unavailable"),)
            for source in agent_sources:
                if isinstance(source, UsageStorageDiagnostic):
                    diagnostic = replace(
                        source,
                        path_label=f"private discovery:{agent_name}:{user_id}",
                        scope="private_agent",
                    )
                    sources[diagnostic.path_label] = diagnostic
                elif source.path.is_file():
                    sources[source.path_label] = source
    return tuple(sources[key] for key in sorted(sources))


def discover_admin_usage_sources(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[UsageStorageSource | UsageStorageDiagnostic, ...]:
    """Find configured shared agents plus existing private and team databases."""
    root = _session_root(runtime_paths)
    sources = _shared_agent_sources(root, config)
    sources.extend(_private_agent_sources(root, config, runtime_paths.storage_root))
    sources.extend(_team_sources(root, config))
    return tuple(sorted(sources, key=lambda item: item.path_label))


def _session_root(runtime_paths: RuntimePaths) -> Path:
    return resolve_session_state_root(runtime_paths.storage_root, runtime_paths).expanduser().resolve()


def _shared_agent_sources(
    root: Path,
    config: Config,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for agent_name, agent_config in config.agents.items():
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
    state_root: Path,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    private_root = root / "private_instances"
    directory_entries = _directory_entries(private_root)
    if isinstance(directory_entries, UsageStorageDiagnostic):
        return [replace(directory_entries, scope="private_agent")]
    entries = directory_entries
    private_agents = tuple(name for name, agent in config.agents.items() if agent.private is not None)
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for worker_directory in entries:
        if _WORKER_DIRECTORY.fullmatch(worker_directory.name) is None:
            continue
        if worker_directory.is_symlink() and is_verified_private_instance_alias(state_root, worker_directory):
            continue  # Its canonical directory is scanned separately.
        if worker_directory.is_symlink() or not worker_directory.is_dir():
            sources.append(
                _diagnostic(
                    worker_directory.relative_to(root).as_posix(),
                    "partial",
                    "source discovery unavailable",
                    scope="private_agent",
                ),
            )
            continue
        # Ownership lives with runtime state, even when sessions use a separate root.
        try:
            owner = load_private_instance_identity(
                state_root,
                state_root / "private_instances" / worker_directory.name,
            )
        except (PrivateInstanceIdentityError, OSError):
            owner = None
        for agent_name in private_agents:
            relative = Path("private_instances") / worker_directory.name / agent_name / "sessions" / f"{agent_name}.db"
            candidate = _safe_candidate(root, relative)
            if candidate is None:
                sources.append(
                    _diagnostic(relative.as_posix(), "partial", "source discovery unavailable", scope="private_agent"),
                )
                continue
            if not candidate.is_file():
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
                    owner_id=owner.requester_id if owner is not None else None,
                ),
            )
    return sources


def _team_sources(
    root: Path,
    config: Config,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    directory_entries = _directory_entries(root / "teams")
    if isinstance(directory_entries, UsageStorageDiagnostic):
        return [directory_entries]
    entries = directory_entries
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for directory in entries:
        storage_name = directory.name
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


def _directory_entries(path: Path) -> tuple[Path, ...] | UsageStorageDiagnostic:
    try:
        path.lstat()
    except FileNotFoundError:
        return ()
    except OSError:
        return _diagnostic("admin discovery", "partial", "source discovery unavailable")
    if path.is_symlink() or not path.is_dir():
        return _diagnostic("admin discovery", "partial", "source discovery unavailable")
    try:
        entries = tuple(path.iterdir())
    except OSError:
        return _diagnostic("admin discovery", "partial", "source discovery unavailable")
    return tuple(sorted(entries, key=lambda entry: entry.name))


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
    owner_id: str | None = None,
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
        owner_id=owner_id,
    )


def iter_usage_storage_rows(
    source: UsageStorageSource,
    *,
    mode: _UsageReadMode = "runs",
) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
    """Yield the requested aggregate-only fields without writing or creating a database."""
    if mode not in {"runs", "session_metrics", "both"}:
        raise ValueError(mode)
    if not source.path.is_file():
        yield _source_diagnostic(source, "absent", "database absent")
        return
    try:
        with _open_read_only_database(source) as connection:
            schema = _validate_schema(connection, source)
            if isinstance(schema, UsageStorageDiagnostic):
                yield schema
                return
            table = _quote_identifier(source.expected_session_table)
            payload_columns = (
                "session_data AS session_payload, length(CAST(session_data AS BLOB)) AS session_payload_bytes, "
                f"{legacy_session_runs_projection(schema)}"
            )
            query = (
                "SELECT session_id, session_type, agent_id, team_id, user_id, "  # noqa: S608
                f"{payload_columns} FROM {table}"
            )
            runs_table = _runs_table(source.expected_session_table)
            read_runs = mode != "session_metrics" and _table_exists(connection, runs_table)
            archive_table = f"{source.expected_session_table}_usage"
            read_archive = mode != "session_metrics" and _table_exists(connection, archive_table)
            for row in connection.execute(query):
                session_payload_bytes = row["session_payload_bytes"]
                legacy_payload_bytes = row["legacy_runs_payload_bytes"]
                if not _is_valid_payload_size(session_payload_bytes) or not _is_valid_payload_size(
                    legacy_payload_bytes,
                ):
                    yield _source_diagnostic(source, "partial", "malformed retained session")
                    continue
                try:
                    yield _extract_row(
                        source,
                        row,
                        mode=mode,
                        persisted_runs=_persisted_runs(
                            connection,
                            runs_table,
                            row["session_id"],
                            read_runs=read_runs,
                            archive_table=archive_table if read_archive else None,
                        ),
                        legacy_runs_payload=row["legacy_runs_payload"],
                        legacy_payload_bytes=legacy_payload_bytes or 0,
                        session_payload_bytes=session_payload_bytes or 0,
                    )
                except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
                    yield _source_diagnostic(source, "partial", "malformed retained session")
    except sqlite3.Error as error:
        yield _sqlite_diagnostic(source, error)
    except OSError:
        yield _source_diagnostic(source, "partial", "database unavailable")


def _is_valid_payload_size(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(slots=True)
class _PersistedRun:
    """One raw run payload and its authoritative creation timestamp."""

    payload: object
    created_at: object
    archived: bool = False
    archive_run_id: object = None


@dataclass(slots=True)
class _PersistedRuns:
    """Raw run payloads for one session, in run-table order."""

    payloads: list[_PersistedRun] = field(default_factory=list)
    payload_bytes: int = 0


def _validate_schema(
    connection: sqlite3.Connection,
    source: UsageStorageSource,
) -> set[str] | UsageStorageDiagnostic:
    table = source.expected_session_table
    if _IDENTIFIER.fullmatch(table) is None:
        return _source_diagnostic(source, "unsupported_schema", "session table unavailable")
    if not _table_exists(connection, table):
        return _source_diagnostic(source, "unsupported_schema", "session table unavailable")
    columns = _table_columns(connection, table)
    if not _REQUIRED_COLUMNS.issubset(columns):
        return _source_diagnostic(source, "unsupported_schema", "session schema unsupported")
    runs_table = _runs_table(table)
    if _table_exists(connection, runs_table) and not _RUNS_TABLE_REQUIRED_COLUMNS.issubset(
        _table_columns(connection, runs_table),
    ):
        return _source_diagnostic(source, "unsupported_schema", "runs schema unsupported")
    archive_table = f"{table}_usage"
    if _table_exists(connection, archive_table) and not _RUNS_TABLE_REQUIRED_COLUMNS.issubset(
        _table_columns(connection, archive_table),
    ):
        return _source_diagnostic(source, "unsupported_schema", "usage archive schema unsupported")
    return columns


def _runs_table(session_table: str) -> str:
    return f"{session_table}_runs"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}


def _persisted_runs(
    connection: sqlite3.Connection,
    runs_table: str,
    session_id: object,
    *,
    read_runs: bool = True,
    archive_table: str | None = None,
) -> _PersistedRuns:
    """Read one session's run rows in run order, bounded by that session's history."""
    persisted = _PersistedRuns()
    if not isinstance(session_id, str):
        return persisted
    tables = ([runs_table] if read_runs else []) + ([archive_table] if archive_table else [])
    for table in tables:
        query = (
            "SELECT run_id, run_data AS run_payload, created_at, "  # noqa: S608
            "length(CAST(run_data AS BLOB)) AS run_payload_bytes "
            f"FROM {_quote_identifier(table)} WHERE session_id = ? "
            "ORDER BY run_index ASC, created_at ASC, run_id ASC"
        )
        for row in connection.execute(query, (session_id,)):
            payload_bytes = row["run_payload_bytes"]
            persisted.payloads.append(
                _PersistedRun(
                    payload=row["run_payload"],
                    created_at=row["created_at"],
                    archived=table == archive_table,
                    archive_run_id=row["run_id"] if table == archive_table else None,
                ),
            )
            if isinstance(payload_bytes, int) and not isinstance(payload_bytes, bool) and payload_bytes > 0:
                persisted.payload_bytes += payload_bytes
    return persisted


def _extract_row(
    source: UsageStorageSource,
    row: sqlite3.Row,
    *,
    mode: _UsageReadMode,
    persisted_runs: _PersistedRuns,
    legacy_runs_payload: object,
    legacy_payload_bytes: int,
    session_payload_bytes: int,
) -> UsageSessionRow:
    entity_kind = row["session_type"]
    if entity_kind not in {"agent", "team"}:
        raise ValueError
    entity_id = row["agent_id"] if entity_kind == "agent" else row["team_id"]
    row_key = row["session_id"]
    row_requester = _optional_string(row["user_id"])
    if not isinstance(entity_id, str) or not entity_id or not isinstance(row_key, str) or not row_key:
        raise ValueError
    runs_available = mode != "session_metrics"
    session_metrics_available = mode != "runs"
    payload_bytes = session_payload_bytes if mode != "runs" else 0
    if mode != "session_metrics":
        payload_bytes += persisted_runs.payload_bytes + legacy_payload_bytes
    if mode == "runs":
        runs = _extract_runs(persisted_runs.payloads, legacy_runs_payload, row_requester=row_requester)
        session_metrics = MappingProxyType({})
    elif mode == "session_metrics":
        runs = ()
        session_metrics = _decode_session_metrics(row["session_payload"])
    else:
        try:
            runs = _extract_runs(persisted_runs.payloads, legacy_runs_payload, row_requester=row_requester)
        except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
            runs = ()
            runs_available = False
        try:
            session_metrics = _decode_session_metrics(row["session_payload"])
        except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
            session_metrics = MappingProxyType({})
            session_metrics_available = False
    return UsageSessionRow(
        source=source,
        entity_id=_bounded_string(entity_id),
        entity_kind=cast("Literal['agent', 'team']", entity_kind),
        row_key=_bounded_string(row_key),
        runs=runs,
        session_metrics=session_metrics,
        requester_id=row_requester,
        payload_bytes=payload_bytes,
        runs_available=runs_available,
        session_metrics_available=session_metrics_available,
    )


def _extract_runs(
    run_payloads: list[_PersistedRun],
    legacy_runs_payload: object,
    *,
    row_requester: str | None,
) -> tuple[UsageRunNode, ...]:
    """Merge run-table rows with any legacy blob runs the run table does not hold yet.

    Run-table rows come first and win on ``run_id``; legacy-only runs are
    appended, followed by archive-only facts. Aggregation does not depend on the order.
    """
    current_runs: list[object] = []
    archived_runs: list[object] = []
    for persisted_run in run_payloads:
        decoded = decode_persisted_session_json(persisted_run.payload)
        if not isinstance(decoded, dict):
            raise TypeError
        if persisted_run.archived:
            archived = cast("dict[str, object]", decoded)
            if (
                not isinstance(persisted_run.archive_run_id, str)
                or archived.get("run_id") != persisted_run.archive_run_id
            ):
                raise ValueError
        # Agno preserves the column on updates, even when run_data loses or changes its timestamp.
        target = archived_runs if persisted_run.archived else current_runs
        target.append({**decoded, "created_at": persisted_run.created_at})
    raw_runs = merge_legacy_run_payloads(current_runs, legacy_runs_payload)
    live_ids = {cast("dict[str, object]", run).get("run_id") for run in raw_runs if isinstance(run, dict)}
    raw_runs.extend(
        run
        for run in archived_runs
        if isinstance(run, dict) and cast("dict[str, object]", run).get("run_id") not in live_ids
    )
    runs: list[UsageRunNode] = []
    for raw_run in raw_runs:
        extracted = _extract_run(raw_run, row_requester=row_requester)
        if extracted is not None:
            runs.append(extracted)
    return tuple(runs)


def _decode_session_metrics(raw_value: object) -> Mapping[str, _MetricValue]:
    decoded = decode_persisted_session_json(raw_value)
    if decoded is None:
        return MappingProxyType({})
    if not isinstance(decoded, dict):
        raise TypeError
    raw_metrics = cast("dict[str, object]", decoded).get("session_metrics")
    if raw_metrics is None:
        return MappingProxyType({})
    if not isinstance(raw_metrics, dict):
        raise TypeError
    return _select_metrics(cast("dict[str, object]", raw_metrics))


def _extract_run(raw_run: object, *, row_requester: str | None) -> UsageRunNode | None:
    if not isinstance(raw_run, dict):
        raise TypeError
    run = cast("dict[str, object]", raw_run)
    parent_run_id = run.get("parent_run_id")
    if parent_run_id is not None:
        if not isinstance(parent_run_id, str) or not parent_run_id:
            raise TypeError
        return None
    metadata = run.get("metadata")
    metadata_requester = (
        _optional_string(cast("dict[str, object]", metadata).get("requester_id"))
        if isinstance(metadata, dict)
        else None
    )
    metrics = run.get("metrics")
    if metrics is None:
        metrics = {}
    if not isinstance(metrics, dict):
        raise TypeError
    run_metrics = cast("dict[str, object]", metrics)
    selected_metrics = _select_metrics(run_metrics)
    created_at = run.get("created_at")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or (isinstance(created_at, float) and not math.isfinite(created_at))
    ):
        created_at = None
    return UsageRunNode(
        team_id=_optional_string(run.get("team_id")),
        requester_id=metadata_requester or _optional_string(run.get("user_id")) or row_requester,
        run_id=_optional_string(run.get("run_id")),
        model_provider=_optional_string(run.get("model_provider")),
        model=_optional_string(run.get("model")),
        metrics=selected_metrics,
        created_at=created_at,
        model_metrics=_extract_model_metrics(run_metrics.get("details")),
    )


def _extract_model_metrics(details: object) -> tuple[UsageModelMetrics, ...] | None:
    """Read Agno's per-role model lists without discarding usable run totals."""
    if details is None:
        return ()
    if not isinstance(details, dict):
        return None
    models: list[UsageModelMetrics] = []
    try:
        for entries in details.values():
            if not isinstance(entries, list):
                return None
            for entry in entries:
                if not isinstance(entry, dict):
                    return None
                model_metrics = cast("dict[str, object]", entry)
                models.append(
                    UsageModelMetrics(
                        model_provider=_optional_string(model_metrics.get("provider")),
                        model=_optional_string(model_metrics.get("id")),
                        metrics=_select_metrics(model_metrics),
                    ),
                )
    except (TypeError, ValueError):
        return None
    return tuple(models) if models else None


def _select_metrics(metrics: Mapping[str, object]) -> Mapping[str, _MetricValue]:
    selected_metrics: dict[str, _MetricValue] = {}
    for metric_name in TOKEN_FIELDS:
        value = metrics.get(metric_name)
        if isinstance(value, bool) or not isinstance(value, (int, float, str, type(None))):
            raise TypeError
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        if value is not None:
            selected_metrics[metric_name] = value
    return MappingProxyType(selected_metrics)


def _optional_string(value: object) -> str | None:
    return _bounded_string(value) if isinstance(value, str) and value else None


def _bounded_string(value: str) -> str:
    if len(value) > _MAX_STRING_LENGTH:
        raise ValueError
    return value


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _diagnostic(
    path_label: str,
    status: Literal["partial"],
    detail: str,
    *,
    scope: _UsageStorageScope | None = None,
) -> UsageStorageDiagnostic:
    return UsageStorageDiagnostic(path_label=path_label, status=status, detail=detail, scope=scope)


def _source_diagnostic(
    source: UsageStorageSource,
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "partial"],
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
