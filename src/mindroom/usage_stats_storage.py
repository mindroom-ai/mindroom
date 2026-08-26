"""Small read-only adapter for retained Agno SQLite sessions."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    "TOKEN_FIELDS",
    "UsageRunNode",
    "UsageSessionRow",
    "UsageStorageDiagnostic",
    "UsageStorageSource",
    "discover_admin_usage_sources",
    "discover_self_usage_sources",
    "iter_usage_storage_rows",
]

type _UsageStorageScope = Literal["shared_agent", "private_agent", "team"]
type _UsageReadMode = Literal["runs", "session_metrics", "both"]
type _MetricValue = int | float | str | None

_MAX_STRING_LENGTH = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+\Z")
_WORKER_DIRECTORY = re.compile(r"[A-Za-z0-9._@+-]+-[0-9a-f]{16}\Z")
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
    "audio_total_tokens",
)
_REQUIRED_COLUMNS = frozenset(
    {"session_id", "session_type", "agent_id", "team_id", "user_id", "runs", "session_data"},
)


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
    run_id: str | None
    model_provider: str | None
    model: str | None
    metrics: Mapping[str, _MetricValue]


@dataclass(frozen=True, slots=True)
class UsageSessionRow:
    """Top-level usage runs from one retained Agno session."""

    source: UsageStorageSource
    entity_id: str
    entity_kind: Literal["agent", "team"]
    row_key: str
    runs: tuple[UsageRunNode, ...]
    session_metrics: Mapping[str, _MetricValue] = field(default_factory=lambda: MappingProxyType({}))
    payload_bytes: int = 0
    runs_available: bool = True
    session_metrics_available: bool = True


@dataclass(frozen=True, slots=True)
class UsageStorageDiagnostic:
    """Content-free outcome for a source that could not be read."""

    path_label: str
    status: Literal["absent", "busy", "corrupt", "unsupported_schema", "partial"]
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
) -> list[UsageStorageSource | UsageStorageDiagnostic]:
    private_root = root / "private_instances"
    directory_entries = _directory_entries(private_root)
    if isinstance(directory_entries, UsageStorageDiagnostic):
        return [directory_entries]
    entries = directory_entries
    private_agents = tuple(name for name, agent in config.agents.items() if agent.private is not None)
    sources: list[UsageStorageSource | UsageStorageDiagnostic] = []
    for worker_directory in entries:
        if worker_directory.is_symlink() or not worker_directory.is_dir():
            continue
        if _WORKER_DIRECTORY.fullmatch(worker_directory.name) is None:
            continue
        for agent_name in private_agents:
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
            schema_error = _validate_schema(connection, source)
            if schema_error is not None:
                yield schema_error
                return
            table = _quote_identifier(source.expected_session_table)
            if mode == "both":
                query = (
                    "SELECT session_id, session_type, agent_id, team_id, user_id, "  # noqa: S608
                    "runs AS runs_payload, "
                    "length(CAST(runs AS BLOB)) AS runs_payload_bytes, "
                    "session_data AS session_payload, "
                    "length(CAST(session_data AS BLOB)) AS session_payload_bytes "
                    f"FROM {table}"
                )
            else:
                payload_column = "runs" if mode == "runs" else "session_data"
                query = (
                    "SELECT session_id, session_type, agent_id, team_id, user_id, "  # noqa: S608
                    f"{payload_column} AS payload, "
                    f"length(CAST({payload_column} AS BLOB)) AS payload_bytes "
                    f"FROM {table}"
                )
            for row in connection.execute(query):
                payload_sizes = (
                    (row["runs_payload_bytes"], row["session_payload_bytes"])
                    if mode == "both"
                    else (row["payload_bytes"],)
                )
                if any(
                    isinstance(size, bool) or (size is not None and (not isinstance(size, int) or size < 0))
                    for size in payload_sizes
                ):
                    yield _source_diagnostic(source, "partial", "malformed retained session")
                    continue
                try:
                    yield _extract_row(
                        source,
                        row,
                        mode=mode,
                        payload_bytes=sum(size or 0 for size in payload_sizes),
                    )
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


def _extract_row(
    source: UsageStorageSource,
    row: sqlite3.Row,
    *,
    mode: _UsageReadMode,
    payload_bytes: int,
) -> UsageSessionRow:
    entity_kind = row["session_type"]
    if entity_kind not in {"agent", "team"}:
        raise ValueError
    entity_id = row["agent_id"] if entity_kind == "agent" else row["team_id"]
    row_key = row["session_id"]
    row_requester = _optional_string(row["user_id"])
    if not isinstance(entity_id, str) or not entity_id or not isinstance(row_key, str) or not row_key:
        raise ValueError
    runs_available = True
    session_metrics_available = True
    if mode == "runs":
        runs = _extract_runs(row["payload"], row_requester=row_requester)
        session_metrics = MappingProxyType({})
    elif mode == "session_metrics":
        runs = ()
        session_metrics = _decode_session_metrics(row["payload"])
    else:
        try:
            runs = _extract_runs(row["runs_payload"], row_requester=row_requester)
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
        payload_bytes=payload_bytes,
        runs_available=runs_available,
        session_metrics_available=session_metrics_available,
    )


def _extract_runs(raw_value: object, *, row_requester: str | None) -> tuple[UsageRunNode, ...]:
    runs: list[UsageRunNode] = []
    for raw_run in _decode_runs(raw_value):
        extracted = _extract_run(raw_run, row_requester=row_requester)
        if extracted is not None:
            runs.append(extracted)
    return tuple(runs)


def _decode_runs(raw_value: object) -> list[object]:
    decoded = _decode_agno_json(raw_value)
    if decoded is None:
        return []
    if not isinstance(decoded, list):
        raise TypeError
    return cast("list[object]", decoded)


def _decode_session_metrics(raw_value: object) -> Mapping[str, _MetricValue]:
    decoded = _decode_agno_json(raw_value)
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


def _decode_agno_json(raw_value: object) -> object:
    if raw_value is None:
        return None
    if not isinstance(raw_value, (str, bytes, bytearray)):
        raise TypeError
    decoded = json.loads(raw_value)
    return json.loads(decoded) if isinstance(decoded, str) else decoded


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
    selected_metrics = _select_metrics(cast("dict[str, object]", metrics))
    return UsageRunNode(
        team_id=_optional_string(run.get("team_id")),
        requester_id=metadata_requester or _optional_string(run.get("user_id")) or row_requester,
        run_id=_optional_string(run.get("run_id")),
        model_provider=_optional_string(run.get("model_provider")),
        model=_optional_string(run.get("model")),
        metrics=selected_metrics,
    )


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
) -> UsageStorageDiagnostic:
    return UsageStorageDiagnostic(path_label=path_label, status=status, detail=detail)


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
