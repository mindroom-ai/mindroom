"""Small read-only adapter for retained conversation SQLite sessions."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeGuard, cast

from mindroom.constants import RuntimePaths, resolve_session_state_root
from mindroom.llm.usage import TOKEN_FIELDS
from mindroom.private_storage.legacy_private_storage_aliases import is_verified_private_instance_alias
from mindroom.private_storage.private_instance_identity import (
    PrivateInstanceIdentityError,
    load_private_instance_identity,
)
from mindroom.requester_identity import equivalent_requester_ids
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.session_storage.schema import LEDGER_TOTALS_VERSION, TableNames, table_names
from mindroom.session_storage.usage_reader import (
    read_only_snapshot,
    recorded_meta,
    stored_sessions,
    stored_usage,
    unreadable_layout,
)
from mindroom.tool_system.worker_routing import build_requester_execution_identity, worker_dir_name
from mindroom.usage.legacy_usage_storage import current_provider_name
from mindroom.usage.storage import (
    SYSTEM_USAGE_ENTITY,
    SYSTEM_USAGE_STORAGE_NAME,
    USAGE_KINDS,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from mindroom.config.main import Config
    from mindroom.session_storage.usage_reader import RecordedMeta, StoredSession
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.usage.storage import UsageKind

__all__ = [
    "UsageModelMetrics",
    "UsageRequestMetrics",
    "UsageRunNode",
    "UsageSessionRow",
    "UsageStorageDiagnostic",
    "UsageStorageSource",
    "discover_admin_usage_sources",
    "discover_private_usage_sources",
    "discover_self_usage_sources",
    "iter_usage_storage_rows",
]

type _UsageStorageScope = Literal["shared_agent", "private_agent", "team", "system"]
type _MetricValue = int | float | str | None

_MAX_STRING_LENGTH = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+\Z")
_WORKER_DIRECTORY = re.compile(r"[A-Za-z0-9._@+-]+-[0-9a-f]{16}\Z")


@dataclass(frozen=True, slots=True)
class UsageStorageSource:
    """One fixed-layout conversation database."""

    path: Path
    path_label: str
    scope: _UsageStorageScope
    source_agent_id: str | None
    allowed_agent_ids: frozenset[str]
    requester_isolated: bool
    owner_id: str | None = None

    @property
    def names(self) -> TableNames:
        """The names of the database ``path`` holds."""
        return TableNames.of_database(self.path)


@dataclass(frozen=True, slots=True)
class UsageModelMetrics:
    """Content-free token counters attributed to one stored model."""

    model_provider: str | None
    model: str | None
    metrics: Mapping[str, _MetricValue]


@dataclass(frozen=True, slots=True)
class UsageRequestMetrics:
    """Content-free counters, timestamp and optional model for one provider request."""

    created_at: int | float
    metrics: Mapping[str, _MetricValue]
    model_provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class UsageRunNode:
    """Usage fields from one retained run, team member, or independent helper."""

    team_id: str | None
    requester_id: str | None
    run_id: str | None
    model_provider: str | None
    model: str | None
    metrics: Mapping[str, _MetricValue]
    parent_run_id: str | None = None
    created_at: int | float | None = None
    # Empty means no detailed attribution was stored; None means it was unusable.
    model_metrics: tuple[UsageModelMetrics, ...] | None = ()
    # None means request detail was absent or unusable, without discarding run totals.
    requests: tuple[UsageRequestMetrics, ...] | None = None
    kind: UsageKind = "run"
    voice_seconds: float | None = None
    voice_finalized: bool = False

    @property
    def run_count(self) -> int:
        """Members and helpers incur tokens without adding top-level AI replies."""
        return int(self.kind == "run" and self.parent_run_id is None)


@dataclass(frozen=True, slots=True)
class UsageSessionRow:
    """The usage ledger rows of one retained session, whose sum is its totals."""

    source: UsageStorageSource
    entity_id: str
    entity_kind: Literal["agent", "team"]
    row_key: str
    runs: tuple[UsageRunNode, ...]
    requester_id: str | None = None
    runs_available: bool = True
    # False when the rows may not sum to the totals: a whole-history read of a database whose conversion to the layout
    # version that keeps pre-ledger usage as a row (``LEDGER_TOTALS_VERSION``) has not run yet.
    totals_available: bool = True


@dataclass(frozen=True, slots=True)
class UsageStorageDiagnostic:
    """Content-free outcome for a source that could not be read."""

    path_label: str
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "partial"]
    detail: str
    scope: _UsageStorageScope | None = None


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
    names = TableNames(agent_name)
    database = names.database_path(resolved.session_state_root)
    if resolved.execution.is_private:
        worker_key = resolved.execution.worker_key
        if worker_key is None:
            return (_discovery_unavailable("self"),)
        relative = names.database_path(Path("private_instances") / worker_dir_name(worker_key) / agent_name)
        scope: _UsageStorageScope = "private_agent"
    else:
        relative = names.database_path(Path("agents") / agent_name)
        scope = "shared_agent"
    candidate = _safe_candidate(session_root, relative)
    if candidate is None or candidate != database.expanduser().resolve():
        return (_discovery_unavailable("self"),)
    return (
        _source(
            path=candidate,
            root=session_root,
            scope=scope,
            agent_name=agent_name,
            config=config,
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
    requester_ids = equivalent_requester_ids(requester_id, config, runtime_paths)
    sources: dict[str, UsageStorageSource | UsageStorageDiagnostic] = {}
    for agent_name, agent_config in config.agents.items():
        if agent_config.private is None:
            continue
        for user_id in sorted(requester_ids):
            identity = build_requester_execution_identity(
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                requester_id=user_id,
            )
            try:
                agent_sources = discover_self_usage_sources(
                    agent_name=agent_name,
                    config=config,
                    runtime_paths=runtime_paths,
                    execution_identity=identity,
                )
            except (OSError, ValueError):
                agent_sources = (_discovery_unavailable("self"),)
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
    system_path = _safe_candidate(
        root,
        TableNames(SYSTEM_USAGE_STORAGE_NAME).database_path(Path(SYSTEM_USAGE_STORAGE_NAME)),
    )
    if system_path is not None and system_path.is_file():
        sources.append(
            _source(
                path=system_path,
                root=root,
                scope="system",
                agent_name=None,
                config=config,
            ),
        )
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
        relative = TableNames(agent_name).database_path(Path("agents") / agent_name)
        candidate = _safe_candidate(root, relative)
        if candidate is None:
            sources.append(_discovery_unavailable(relative.as_posix(), "shared_agent"))
            continue
        sources.append(
            _source(
                path=candidate,
                root=root,
                scope="shared_agent",
                agent_name=agent_name,
                config=config,
            ),
        )
    return sources


def _private_agent_sources(
    root: Path,
    config: Config,
    state_root: Path,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    entries = _directory_entries(root / "private_instances")
    if isinstance(entries, UsageStorageDiagnostic):
        return [replace(entries, scope="private_agent")]
    private_agents = tuple(name for name, agent in config.agents.items() if agent.private is not None)
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for worker_directory in entries:
        if _WORKER_DIRECTORY.fullmatch(worker_directory.name) is None:
            continue
        if worker_directory.is_symlink() and is_verified_private_instance_alias(state_root, worker_directory):
            continue  # Its canonical directory is scanned separately.
        if worker_directory.is_symlink() or not worker_directory.is_dir():
            sources.append(_discovery_unavailable(worker_directory.relative_to(root).as_posix(), "private_agent"))
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
            relative = TableNames(agent_name).database_path(
                Path("private_instances") / worker_directory.name / agent_name,
            )
            candidate = _safe_candidate(root, relative)
            if candidate is None:
                sources.append(_discovery_unavailable(relative.as_posix(), "private_agent"))
                continue
            if not candidate.is_file():
                continue
            sources.append(
                _source(
                    path=candidate,
                    root=root,
                    scope="private_agent",
                    agent_name=agent_name,
                    config=config,
                    owner_id=owner.requester_id if owner is not None else None,
                ),
            )
    return sources


def _team_sources(
    root: Path,
    config: Config,
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    entries = _directory_entries(root / "teams")
    if isinstance(entries, UsageStorageDiagnostic):
        return [entries]
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for directory in entries:
        storage_name = directory.name
        if _IDENTIFIER.fullmatch(storage_name) is None:
            continue
        relative = TableNames(storage_name).database_path(Path("teams") / storage_name)
        candidate = _safe_candidate(root, relative)
        if candidate is None:
            sources.append(_discovery_unavailable(relative.as_posix(), "team"))
            continue
        if not directory.is_dir() or not candidate.is_file():
            continue
        sources.append(
            _source(
                path=candidate,
                root=root,
                scope="team",
                agent_name=None,
                config=config,
            ),
        )
    return sources


def _directory_entries(path: Path) -> tuple[Path, ...] | UsageStorageDiagnostic:
    try:
        path.lstat()
    except FileNotFoundError:
        return ()
    except OSError:
        return _discovery_unavailable("admin discovery")
    if path.is_symlink() or not path.is_dir():
        return _discovery_unavailable("admin discovery")
    try:
        entries = tuple(path.iterdir())
    except OSError:
        return _discovery_unavailable("admin discovery")
    return tuple(sorted(entries, key=lambda entry: entry.name))


def _safe_candidate(root: Path, relative: Path) -> Path | None:
    current = root
    candidate = root / relative
    try:
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                return None
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
    except OSError:
        return None  # An unsearchable directory is reported like a rejected path, not as a failed discovery.
    return resolved if resolved.is_relative_to(root) else None


def _source(
    *,
    path: Path,
    root: Path,
    scope: _UsageStorageScope,
    agent_name: str | None,
    config: Config,
    owner_id: str | None = None,
) -> UsageStorageSource:
    return UsageStorageSource(
        path=path,
        path_label=path.relative_to(root).as_posix(),
        scope=scope,
        source_agent_id=agent_name,
        allowed_agent_ids=frozenset(config.agents),
        requester_isolated=scope == "private_agent",
        owner_id=owner_id,
    )


def iter_usage_storage_rows(
    source: UsageStorageSource,
    *,
    since: float | None = None,
) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
    """Read each session's usage snapshots without writes.

    ``since`` reads only snapshots last updated at or after that epoch time, which leaves out the undated usage a
    session had before the ledger.

    The whole database is read in one snapshot that is closed before the first row is yielded, so the caller's work
    between rows never holds the database's read lock, which writers wait for. All of the database's rows are
    therefore held in memory at once.
    """
    yield from list(_read_usage_storage(source, since=since))


def _read_usage_storage(
    source: UsageStorageSource,
    *,
    since: float | None,
) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
    """Yield the database's rows, and a diagnostic for what it could not read, while its snapshot is open."""
    if not source.path.is_file():
        yield _source_diagnostic(source, "absent", "database absent")
        return
    try:
        with read_only_snapshot(source.path) as connection:
            problem = (
                "session table unavailable"
                if _IDENTIFIER.fullmatch(source.names.sessions) is None
                else unreadable_layout(connection, source.names)
            )
            if problem is not None:
                yield _source_diagnostic(source, "unsupported_schema", problem)
                return
            has_usage = source.names.usage in table_names(connection)
            meta: RecordedMeta | None = None
            # A dated read visits the sessions with a usage snapshot in range, or every one when there is no ledger.
            rows = stored_sessions(connection, source.names, since=since if has_usage else None)
            for row in rows:
                # Read once a row needs it, so a database without sessions reads alike in every layout.
                meta = meta or recorded_meta(connection, source.names)
                if meta is None:
                    yield _source_diagnostic(source, "unsupported_schema", "session owner unavailable")
                    return
                try:
                    yield _extract_row(source, row, meta, connection, has_usage=has_usage, since=since)
                except (RecursionError, TypeError, ValueError):
                    yield _source_diagnostic(source, "partial", "malformed retained session")
    except sqlite3.Error as error:
        yield _sqlite_diagnostic(source, error)
    except OSError:
        yield _source_diagnostic(source, "partial", "database unavailable")


def _extract_row(
    source: UsageStorageSource,
    row: StoredSession,
    meta: RecordedMeta,
    connection: sqlite3.Connection,
    *,
    has_usage: bool,
    since: float | None,
) -> UsageSessionRow:
    entity_kind, entity_id, version = meta
    if entity_kind not in {"agent", "team"}:
        raise ValueError
    if source.scope == "system" and (entity_kind != "agent" or entity_id != SYSTEM_USAGE_ENTITY):
        raise ValueError
    row_key = row.session_id
    row_requester = _optional_string(row.user_id)
    if not isinstance(entity_id, str) or not entity_id or not isinstance(row_key, str) or not row_key:
        raise ValueError
    runs, runs_available = (
        _read_runs(
            connection,
            source,
            row_key,
            row_requester=row_requester,
            include_team_members=source.scope == "team" and entity_kind == "team",
            since=since,
        )
        if has_usage
        else ([], False)
    )
    return UsageSessionRow(
        source=source,
        entity_id=_bounded_string(entity_id),
        entity_kind=cast("Literal['agent', 'team']", entity_kind),
        row_key=_bounded_string(row_key),
        runs=tuple(runs),
        requester_id=row_requester,
        runs_available=runs_available,
        totals_available=since is not None or (isinstance(version, int) and version >= LEDGER_TOTALS_VERSION),
    )


def _read_runs(
    connection: sqlite3.Connection,
    source: UsageStorageSource,
    session_id: str,
    *,
    row_requester: str | None,
    include_team_members: bool,
    since: float | None,
) -> tuple[list[UsageRunNode], bool]:
    """Read one session's usage snapshots, reporting whether every snapshot was usable."""
    runs: list[UsageRunNode] = []
    runs_available = True
    for payload in stored_usage(connection, source.names, session_id, since=since):
        try:
            run = _extract_run(
                _decoded_snapshot(payload),
                row_requester=row_requester,
                include_team_members=include_team_members,
            )
            if run is not None:
                runs.append(run)
        except (RecursionError, TypeError, ValueError):
            runs_available = False
    return runs, runs_available


def _decoded_snapshot(payload: object) -> object:
    """Decode one stored usage snapshot; a value that is not text raises as text that is not JSON does."""
    if not isinstance(payload, (str, bytes)):
        raise TypeError
    return json.loads(payload)


def _extract_run(
    raw_run: object,
    *,
    row_requester: str | None,
    include_team_members: bool,
) -> UsageRunNode | None:
    if not isinstance(raw_run, dict):
        raise TypeError
    run = cast("dict[str, object]", raw_run)
    kind = run.get("kind", "run")
    if kind not in USAGE_KINDS:
        raise ValueError
    parent_run_id = run.get("parent_run_id")
    if parent_run_id is not None:
        if not isinstance(parent_run_id, str) or not parent_run_id:
            raise TypeError
        parent_run_id = _bounded_string(parent_run_id)
        if not include_team_members:
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
    voice_seconds = run.get("voice_seconds") if kind == "live_voice" else None
    voice_finalized = run.get("voice_finalized", False)
    if kind == "live_voice" and (
        isinstance(voice_seconds, bool)
        or not isinstance(voice_seconds, (int, float))
        or not 0 <= voice_seconds <= sys.float_info.max
        or not isinstance(voice_finalized, bool)
    ):
        raise ValueError
    return UsageRunNode(
        team_id=_optional_string(run.get("team_id")),
        requester_id=(
            _optional_string(run.get("user_id"))
            if kind != "run"
            else metadata_requester
            or _optional_string(run.get("user_id"))
            or (row_requester if parent_run_id is None else None)
        ),
        run_id=_optional_string(run.get("run_id")),
        parent_run_id=parent_run_id,
        model_provider=_stored_provider(run.get("model_provider")),
        model=_optional_string(run.get("model")),
        metrics=selected_metrics,
        created_at=created_at if _is_timestamp(created_at) else None,
        model_metrics=_extract_model_metrics(run_metrics.get("details")),
        requests=_extract_request_metrics(run.get("requests")),
        kind=cast("UsageKind", kind),
        voice_seconds=float(voice_seconds) if isinstance(voice_seconds, (int, float)) else None,
        voice_finalized=voice_finalized is True,
    )


def _extract_request_metrics(requests: object) -> tuple[UsageRequestMetrics, ...] | None:
    """Reject malformed request details independently from usable aggregate counters."""
    if not isinstance(requests, list) or not requests:
        return None
    selected: list[UsageRequestMetrics] = []
    try:
        for raw_request in requests:
            if not isinstance(raw_request, dict):
                return None
            request = cast("dict[str, object]", raw_request)
            created_at = request.get("created_at")
            metrics = request.get("metrics")
            if not _is_timestamp(created_at) or not isinstance(metrics, dict):
                return None
            if any(key in request and not _optional_string(request[key]) for key in ("model_provider", "model")):
                return None
            selected.append(
                UsageRequestMetrics(
                    created_at,
                    _select_metrics(cast("dict[str, object]", metrics)),
                    model_provider=_stored_provider(request.get("model_provider")),
                    model=_optional_string(request.get("model")),
                ),
            )
    except (TypeError, ValueError):
        return None
    return tuple(selected)


def _extract_model_metrics(details: object) -> tuple[UsageModelMetrics, ...] | None:
    """Read the per-role model lists of stored metrics details without discarding usable run totals."""
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
                        model_provider=_stored_provider(model_metrics.get("provider")),
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


def _is_timestamp(value: object) -> TypeGuard[int | float]:
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_string(value: object) -> str | None:
    return _bounded_string(value) if isinstance(value, str) and value else None


def _stored_provider(value: object) -> str | None:
    provider = _optional_string(value)
    return current_provider_name(provider) if provider is not None else None


def _bounded_string(value: str) -> str:
    if len(value) > _MAX_STRING_LENGTH:
        raise ValueError
    return value


def _discovery_unavailable(path_label: str, scope: _UsageStorageScope | None = None) -> UsageStorageDiagnostic:
    return UsageStorageDiagnostic(path_label, "partial", "source discovery unavailable", scope)


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
