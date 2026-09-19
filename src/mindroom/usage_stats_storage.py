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
from mindroom.legacy_session_storage import decode_persisted_session_json
from mindroom.private_instance_identity import PrivateInstanceIdentityError, load_private_instance_identity
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import build_tool_execution_identity, worker_dir_name
from mindroom.usage_storage import TOKEN_FIELDS, quote_identifier

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
type _UsageReadMode = Literal["runs", "both"]
type _MetricValue = int | float | str | None

_MAX_STRING_LENGTH = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+\Z")
_WORKER_DIRECTORY = re.compile(r"[A-Za-z0-9._@+-]+-[0-9a-f]{16}\Z")
_REQUIRED_COLUMNS = frozenset(
    {"session_id", "session_type", "agent_id", "team_id", "user_id", "session_data"},
)
_USAGE_REQUIRED_COLUMNS = frozenset({"id", "session_id", "run_id", "usage_data"})


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
    """Content-free token counters attributed to one stored model."""

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
    # Empty means no detailed attribution was stored; None means it was unusable.
    session_model_metrics: tuple[UsageModelMetrics, ...] | None = ()
    requester_id: str | None = None
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
    """Read independent usage snapshots and optional cumulative session counters without writes."""
    if mode not in {"runs", "both"}:
        raise ValueError(mode)
    if not source.path.is_file():
        yield _source_diagnostic(source, "absent", "database absent")
        return
    try:
        with _open_read_only_database(source) as connection:
            diagnostic = _validate_schema(connection, source)
            if diagnostic is not None:
                yield diagnostic
                return
            table = quote_identifier(source.expected_session_table)
            usage_table = f"{source.expected_session_table}_usage"
            has_usage = _table_exists(connection, usage_table)
            query = (
                "SELECT session_id, session_type, agent_id, team_id, user_id, session_data "  # noqa: S608
                f"FROM {table}"
            )
            for row in connection.execute(query):
                try:
                    yield _extract_row(source, row, connection, mode=mode, has_usage=has_usage)
                except (RecursionError, TypeError, ValueError):
                    yield _source_diagnostic(source, "partial", "malformed retained session")
    except sqlite3.Error as error:
        yield _sqlite_diagnostic(source, error)
    except OSError:
        yield _source_diagnostic(source, "partial", "database unavailable")


def _validate_schema(connection: sqlite3.Connection, source: UsageStorageSource) -> UsageStorageDiagnostic | None:
    table = source.expected_session_table
    if _IDENTIFIER.fullmatch(table) is None or not _table_exists(connection, table):
        return _source_diagnostic(source, "unsupported_schema", "session table unavailable")
    if not _REQUIRED_COLUMNS.issubset(_table_columns(connection, table)):
        return _source_diagnostic(source, "unsupported_schema", "session schema unsupported")
    usage_table = f"{table}_usage"
    if _table_exists(connection, usage_table) and not _USAGE_REQUIRED_COLUMNS.issubset(
        _table_columns(connection, usage_table),
    ):
        return _source_diagnostic(source, "unsupported_schema", "usage schema unsupported")
    return None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")}


def _extract_row(
    source: UsageStorageSource,
    row: sqlite3.Row,
    connection: sqlite3.Connection,
    *,
    mode: _UsageReadMode,
    has_usage: bool,
) -> UsageSessionRow:
    entity_kind = row["session_type"]
    if entity_kind not in {"agent", "team"}:
        raise ValueError
    entity_id = row["agent_id"] if entity_kind == "agent" else row["team_id"]
    row_key = row["session_id"]
    row_requester = _optional_string(row["user_id"])
    if not isinstance(entity_id, str) or not entity_id or not isinstance(row_key, str) or not row_key:
        raise ValueError
    runs: list[UsageRunNode] = []
    runs_available = has_usage
    if has_usage:
        query = (
            f"SELECT usage_data FROM {quote_identifier(source.expected_session_table + '_usage')} "  # noqa: S608
            "WHERE session_id = ? ORDER BY id"
        )
        for (payload,) in connection.execute(query, (row_key,)):
            try:
                run = _extract_run(json.loads(payload), row_requester=row_requester)
                if run is not None:
                    runs.append(run)
            except (RecursionError, TypeError, ValueError):
                runs_available = False
    session_metrics: Mapping[str, _MetricValue] = MappingProxyType({})
    session_model_metrics: tuple[UsageModelMetrics, ...] | None = ()
    session_metrics_available = mode == "both"
    if session_metrics_available:
        try:
            session_metrics, session_model_metrics = _decode_session_usage(row["session_data"])
        except (RecursionError, TypeError, ValueError):
            session_model_metrics = None
            session_metrics_available = False
    return UsageSessionRow(
        source=source,
        entity_id=_bounded_string(entity_id),
        entity_kind=cast("Literal['agent', 'team']", entity_kind),
        row_key=_bounded_string(row_key),
        runs=tuple(runs),
        session_metrics=session_metrics,
        session_model_metrics=session_model_metrics,
        requester_id=row_requester,
        runs_available=runs_available,
        session_metrics_available=session_metrics_available,
    )


def _decode_session_usage(
    raw_value: object,
) -> tuple[Mapping[str, _MetricValue], tuple[UsageModelMetrics, ...] | None]:
    decoded = decode_persisted_session_json(raw_value)
    if decoded is None:
        return MappingProxyType({}), ()
    if not isinstance(decoded, dict):
        raise TypeError
    raw_metrics = cast("dict[str, object]", decoded).get("session_metrics")
    if raw_metrics is None:
        return MappingProxyType({}), ()
    if not isinstance(raw_metrics, dict):
        raise TypeError
    session_metrics = cast("dict[str, object]", raw_metrics)
    return _select_metrics(session_metrics), _extract_model_metrics(session_metrics.get("details"))


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
    metrics = run.get("metrics", {})
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
