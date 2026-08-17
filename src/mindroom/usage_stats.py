"""Read-only aggregation of retained Agno token usage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mindroom.usage_stats_storage import (
    UsageReadBudget,
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    discover_admin_usage_sources,
    discover_self_usage_sources,
    iter_usage_storage_rows,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = [
    "AdminGroupBy",
    "SelfGroupBy",
    "TokenTotals",
    "UsageBreakdownRow",
    "UsageCoverage",
    "UsageReport",
    "UsageStatsValidationError",
    "collect_admin_usage",
    "collect_self_usage",
    "parse_usage_window",
]

SelfGroupBy = Literal["day"]
AdminGroupBy = Literal["entity", "requester", "day"]
type _GroupBy = SelfGroupBy | AdminGroupBy
type _Scope = Literal["self", "admin"]
type _Dimension = Literal["day", "entity", "requester"]

_TOKEN_FIELDS = (
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
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_BREAKDOWN_LIMIT = 200
_MAX_ROWS_PER_REQUEST = 250
_MAX_RUNS_PER_REQUEST = 25_000
_MAX_BYTES_PER_REQUEST = 64_000_000
_COVERAGE_NOTE = (
    "Retained top-level Agno runs only; team members are counted from agent storage; "
    "nested copies and compacted history are excluded."
)


class UsageStatsValidationError(ValueError):
    """A usage query contains an unsupported value."""


class _IncompleteRetainedRunError(ValueError):
    """A visible retained run has unusable usage fields."""


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """Token counters retained by Agno."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        """Return the public token counters."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "audio_input_tokens": self.audio_input_tokens,
            "audio_output_tokens": self.audio_output_tokens,
            "audio_total_tokens": self.audio_total_tokens,
        }

    def plus(self, other: TokenTotals) -> TokenTotals:
        """Add another retained run's counters."""
        return TokenTotals(**{field: getattr(self, field) + getattr(other, field) for field in _TOKEN_FIELDS})


@dataclass(frozen=True, slots=True)
class UsageBreakdownRow:
    """One aggregate group in a usage report."""

    dimension: Literal["day", "entity", "requester"]
    key: str
    totals: TokenTotals
    run_count: int

    def to_dict(self) -> dict[str, object]:
        """Return the public breakdown row."""
        return {
            "dimension": self.dimension,
            "key": self.key,
            "totals": self.totals.to_dict(),
            "run_count": self.run_count,
        }


@dataclass(frozen=True, slots=True)
class UsageCoverage:
    """Small, honest description of the retained scan."""

    scanned_sources: int
    unavailable_sources: int
    truncated: bool
    note: str = _COVERAGE_NOTE

    def to_dict(self) -> dict[str, object]:
        """Return coverage without implying billing completeness."""
        return {
            "scanned_sources": self.scanned_sources,
            "unavailable_sources": self.unavailable_sources,
            "truncated": self.truncated,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Aggregate-only retained token usage."""

    scope: _Scope
    start: str | None
    end: str
    timezone: str
    totals: TokenTotals
    run_count: int
    session_count: int
    first_observed_at: str | None
    last_observed_at: str | None
    breakdown: tuple[UsageBreakdownRow, ...]
    breakdown_truncated: bool
    coverage: UsageCoverage

    def to_dict(self) -> dict[str, object]:
        """Return the stable custom-tool payload fields."""
        return {
            "scope": self.scope,
            "window": {"start": self.start, "end": self.end, "timezone": self.timezone},
            "totals": self.totals.to_dict(),
            "run_count": self.run_count,
            "session_count": self.session_count,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "breakdown": [row.to_dict() for row in self.breakdown],
            "breakdown_truncated": self.breakdown_truncated,
            "coverage": self.coverage.to_dict(),
        }


@dataclass(slots=True)
class _Aggregate:
    totals: TokenTotals = TokenTotals()
    run_count: int = 0

    def add(self, totals: TokenTotals) -> None:
        self.totals = self.totals.plus(totals)
        self.run_count += 1


@dataclass(frozen=True, slots=True)
class _AcceptedRun:
    entity_id: str
    requester_id: str | None
    created_at: datetime
    totals: TokenTotals


def parse_usage_window(
    *,
    start: str | None,
    end: str | None,
    timezone_name: str,
    as_of: datetime,
) -> tuple[datetime | None, datetime]:
    """Parse an inclusive start and exclusive end into UTC."""
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        message = "Configured timezone is invalid."
        raise UsageStatsValidationError(message) from error
    normalized_as_of = _as_utc(as_of, "as_of must include a timezone")
    parsed_start = _parse_boundary(start, timezone) if start is not None else None
    parsed_end = _parse_boundary(end, timezone) if end is not None else normalized_as_of
    if parsed_start is not None and parsed_start >= parsed_end:
        message = "start must be before end"
        raise UsageStatsValidationError(message)
    return parsed_start, parsed_end


def collect_self_usage(
    *,
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    start: str | None,
    end: str | None,
    group_by: SelfGroupBy,
    as_of: datetime | None = None,
) -> UsageReport:
    """Collect one requester's direct usage for the current agent."""
    _validate_group_by(group_by, {"day"})
    expected_requester = config.authorization.resolve_alias(requester_id)
    return _collect_usage(
        sources=discover_self_usage_sources(
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        ),
        config=config,
        scope="self",
        group_by=group_by,
        start=start,
        end=end,
        as_of=as_of,
        expected_agent=agent_name,
        expected_requester=expected_requester,
        entity_filter=None,
        requester_filter=None,
    )


def collect_admin_usage(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    start: str | None,
    end: str | None,
    group_by: AdminGroupBy,
    entity_names: tuple[str, ...] | None,
    requester_ids: tuple[str, ...] | None,
    as_of: datetime | None = None,
) -> UsageReport:
    """Collect direct usage across configured agent, private-instance, and team stores."""
    _validate_group_by(group_by, {"day", "entity", "requester"})
    known_entities = frozenset((*config.agents, *config.teams))
    entity_filter = frozenset(entity_names) if entity_names is not None else None
    unknown_entities = sorted((entity_filter or frozenset()) - known_entities)
    if unknown_entities:
        message = f"Unknown usage entities: {', '.join(unknown_entities)}"
        raise UsageStatsValidationError(message)
    requester_filter = (
        frozenset(config.authorization.resolve_alias(requester_id) for requester_id in requester_ids)
        if requester_ids is not None
        else None
    )
    return _collect_usage(
        sources=discover_admin_usage_sources(config=config, runtime_paths=runtime_paths),
        config=config,
        scope="admin",
        group_by=group_by,
        start=start,
        end=end,
        as_of=as_of,
        expected_agent=None,
        expected_requester=None,
        entity_filter=entity_filter,
        requester_filter=requester_filter,
    )


def _collect_usage(  # noqa: C901
    *,
    sources: Iterable[UsageStorageSource | UsageStorageDiagnostic],
    config: Config,
    scope: _Scope,
    group_by: _GroupBy,
    start: str | None,
    end: str | None,
    as_of: datetime | None,
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
) -> UsageReport:
    query_as_of = _as_utc(as_of or datetime.now(UTC), "as_of must include a timezone")
    window_start, window_end = parse_usage_window(
        start=start,
        end=end,
        timezone_name=config.timezone,
        as_of=query_as_of,
    )
    timezone = ZoneInfo(config.timezone)
    total = _Aggregate()
    buckets: dict[tuple[_Dimension, str], _Aggregate] = {}
    sessions: set[tuple[str, str]] = set()
    observed: list[datetime] = []
    seen_runs: set[tuple[str, str, str, str]] = set()
    scanned_sources: set[str] = set()
    unavailable_sources: set[str] = set()
    budget = UsageReadBudget(
        row_limit=_MAX_ROWS_PER_REQUEST,
        byte_limit=_MAX_BYTES_PER_REQUEST,
        run_limit=_MAX_RUNS_PER_REQUEST,
    )
    truncated = False
    scan_limit_reached = False

    for discovered in sources:
        if isinstance(discovered, UsageStorageDiagnostic):
            unavailable_sources.add(discovered.path_label)
            truncated = truncated or discovered.status == "resource_limit"
            continue
        source = discovered
        scanned_sources.add(source.path_label)
        for item in iter_usage_storage_rows(source, budget=budget):
            if isinstance(item, UsageStorageDiagnostic):
                unavailable_sources.add(item.path_label)
                truncated = truncated or item.status == "resource_limit"
                continue
            for run in item.runs:
                try:
                    accepted = _accepted_run(
                        row=item,
                        run=run,
                        config=config,
                        scope=scope,
                        expected_agent=expected_agent,
                        expected_requester=expected_requester,
                        entity_filter=entity_filter,
                        requester_filter=requester_filter,
                        window_start=window_start,
                        window_end=window_end,
                    )
                except _IncompleteRetainedRunError:
                    unavailable_sources.add(source.path_label)
                    continue
                if accepted is None:
                    continue
                if run.run_id is not None:
                    identity = (source.path_label, item.row_key, accepted.entity_id, run.run_id)
                    if identity in seen_runs:
                        continue
                    seen_runs.add(identity)
                total.add(accepted.totals)
                sessions.add((source.path_label, item.row_key))
                observed.append(accepted.created_at)
                dimension, key = _breakdown_key(group_by, accepted, timezone)
                buckets.setdefault((dimension, key), _Aggregate()).add(accepted.totals)
        scan_limit_reached = budget.exhausted
        if scan_limit_reached:
            break

    rows = tuple(
        UsageBreakdownRow(
            dimension=dimension,
            key=key,
            totals=aggregate.totals,
            run_count=aggregate.run_count,
        )
        for (dimension, key), aggregate in sorted(
            buckets.items(),
            key=lambda item: (-item[1].totals.total_tokens, item[0]),
        )[:_BREAKDOWN_LIMIT]
    )
    return UsageReport(
        scope=scope,
        start=_format_time(window_start) if window_start is not None else None,
        end=_format_time(window_end),
        timezone=config.timezone,
        totals=total.totals,
        run_count=total.run_count,
        session_count=len(sessions),
        first_observed_at=_format_time(min(observed)) if observed else None,
        last_observed_at=_format_time(max(observed)) if observed else None,
        breakdown=rows,
        breakdown_truncated=len(buckets) > _BREAKDOWN_LIMIT,
        coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(unavailable_sources),
            truncated=truncated,
        ),
    )


def _accepted_run(  # noqa: C901, PLR0911
    *,
    row: UsageSessionRow,
    run: UsageRunNode,
    config: Config,
    scope: _Scope,
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
    window_start: datetime | None,
    window_end: datetime,
) -> _AcceptedRun | None:
    entity_id = _entity_id(row, run)
    requester_id = config.authorization.resolve_alias(run.requester_id) if run.requester_id else None
    if entity_id is None:
        return None
    if scope == "self":
        if entity_id != expected_agent:
            return None
        if not row.source.requester_isolated:
            if requester_id is None:
                raise _IncompleteRetainedRunError
            if requester_id != expected_requester:
                return None
    elif (entity_filter is not None and entity_id not in entity_filter) or (
        requester_filter is not None and requester_id not in requester_filter
    ):
        return None
    created_at = _run_timestamp(run.created_at)
    if created_at is None:
        raise _IncompleteRetainedRunError
    if (window_start is not None and created_at < window_start) or created_at >= window_end:
        return None
    if not any(run.metrics.get(field) is not None for field in _TOKEN_FIELDS):
        return None
    totals = _metrics_totals(run.metrics)
    if totals is None:
        raise _IncompleteRetainedRunError
    return _AcceptedRun(
        entity_id=entity_id,
        requester_id=requester_id,
        created_at=created_at,
        totals=totals,
    )


def _entity_id(row: UsageSessionRow, run: UsageRunNode) -> str | None:
    if row.source.scope in {"shared_agent", "private_agent"}:
        entity_id = row.source.source_agent_id
        return entity_id if entity_id in row.source.allowed_agent_ids else None
    entity_id = run.team_id or (row.entity_id if row.entity_kind == "team" else None)
    return entity_id if entity_id in row.source.allowed_team_ids else None


def _breakdown_key(group_by: _GroupBy, run: _AcceptedRun, timezone: ZoneInfo) -> tuple[_Dimension, str]:
    if group_by == "entity":
        return "entity", run.entity_id
    if group_by == "requester":
        return "requester", run.requester_id or "unknown"
    return "day", run.created_at.astimezone(timezone).date().isoformat()


def _metrics_totals(metrics: Mapping[str, object]) -> TokenTotals | None:
    if not any(metrics.get(field) is not None for field in _TOKEN_FIELDS):
        return None
    values: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        value = _token_value(metrics.get(field))
        if value is None:
            return None
        values[field] = value
    return TokenTotals(**values)


def _token_value(value: object) -> int | None:
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str) and len(value) <= 32:
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed >= 0 else None


def _run_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            return datetime.fromtimestamp(float(value), tz=UTC)
        parsed = datetime.fromisoformat(value)
        return _as_utc(parsed, "run timestamp must include a timezone")
    except (OverflowError, OSError, ValueError):
        return None


def _parse_boundary(value: str, timezone: ZoneInfo) -> datetime:
    try:
        if _DATE_ONLY.fullmatch(value):
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone).astimezone(UTC)
        parsed = datetime.fromisoformat(value)
    except (OverflowError, ValueError) as error:
        message = "Invalid usage time boundary."
        raise UsageStatsValidationError(message) from error
    return _as_utc(parsed, "timestamps must include an explicit UTC offset")


def _as_utc(value: datetime, message: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise UsageStatsValidationError(message)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _validate_group_by(group_by: str, allowed: set[str]) -> None:
    if group_by not in allowed:
        message = "Unsupported usage statistics grouping."
        raise UsageStatsValidationError(message)
