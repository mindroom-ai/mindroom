"""Privacy-safe aggregation of retained direct run usage."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mindroom.usage_stats_storage import (
    UsageModelMetric,
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
    "CostCoverage",
    "SelfGroupBy",
    "TokenTotals",
    "UsageBreakdownRow",
    "UsageCoverage",
    "UsageReport",
    "UsageStatsSourceUnavailableError",
    "UsageStatsValidationError",
    "collect_admin_usage",
    "collect_self_usage",
    "parse_usage_window",
]

SelfGroupBy = Literal["day", "model"]
AdminGroupBy = Literal["entity", "requester", "model", "day"]
_UsageGroupBy = SelfGroupBy | AdminGroupBy
type _BreakdownDimension = Literal["day", "model", "entity", "requester"]
type _BreakdownKey = tuple[_BreakdownDimension, str, str | None, str | None, str | None]
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
_TIMESTAMP_OFFSET = re.compile(r".*(?:Z|[+-]\d{2}:\d{2})\Z")
_MAX_BREAKDOWN_ROWS = 200
_MAX_PERSISTED_NUMERIC_TEXT_LENGTH = 128
_MAX_PERSISTED_INTEGER = 10**_MAX_PERSISTED_NUMERIC_TEXT_LENGTH - 1
_SELF_GROUPS = frozenset({"day", "model"})
_ADMIN_GROUPS = frozenset({"day", "entity", "model", "requester"})


class UsageStatsValidationError(ValueError):
    """A usage query contains an unsupported caller-controlled value."""


class UsageStatsSourceUnavailableError(RuntimeError):
    """No expected retained-history source could be read safely."""


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """Independent retained-run token counters."""

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
        """Serialize only the named aggregate counters."""
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


@dataclass(frozen=True, slots=True)
class CostCoverage:
    """Known retained-run cost subtotal and coverage counts."""

    known_cost: str
    runs_with_cost: int
    runs_without_cost: int

    def to_dict(self) -> dict[str, object]:
        """Serialize the cost subtotal and coverage counts."""
        return {
            "known_cost": self.known_cost,
            "runs_with_cost": self.runs_with_cost,
            "runs_without_cost": self.runs_without_cost,
        }


@dataclass(frozen=True, slots=True)
class UsageBreakdownRow:
    """One bounded aggregate breakdown row."""

    dimension: Literal["day", "model", "entity", "requester"]
    key: str
    model_type: str | None
    provider: str | None
    model_id: str | None
    totals: TokenTotals
    cost: CostCoverage
    run_count: int

    def to_dict(self) -> dict[str, object]:
        """Serialize aggregate dimensions without persisted identifiers."""
        payload: dict[str, object] = {
            "dimension": self.dimension,
            "key": self.key,
            "totals": self.totals.to_dict(),
            "cost": self.cost.to_dict(),
            "run_count": self.run_count,
        }
        if self.dimension == "model":
            payload["model"] = {
                "type": self.model_type,
                "provider": self.provider,
                "id": self.model_id,
            }
        return payload


@dataclass(frozen=True, slots=True)
class UsageCoverage:
    """Coverage information for a retained-run scan."""

    status: Literal["complete_retained", "partial", "unknown"]
    scanned_sources: int
    partial_sources: int
    scanned_sessions: int
    retained_runs: int
    skipped_runs: int
    malformed_runs: int
    missing_requester_runs: int
    missing_timestamp_runs: int
    compacted_sessions: int
    note: str

    def to_dict(self) -> dict[str, object]:
        """Serialize bounded scan coverage."""
        return {
            "status": self.status,
            "scanned_sources": self.scanned_sources,
            "partial_sources": self.partial_sources,
            "scanned_sessions": self.scanned_sessions,
            "retained_runs": self.retained_runs,
            "skipped_runs": self.skipped_runs,
            "malformed_runs": self.malformed_runs,
            "missing_requester_runs": self.missing_requester_runs,
            "missing_timestamp_runs": self.missing_timestamp_runs,
            "compacted_sessions": self.compacted_sessions,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class UsageReport:
    """One aggregate-only retained-run usage report."""

    scope: Literal["self", "admin"]
    start: str | None
    end: str
    timezone: str
    as_of: str
    totals: TokenTotals
    cost: CostCoverage
    turn_count: int
    run_count: int
    session_count: int
    first_observed_at: str | None
    last_observed_at: str | None
    status_counts: Mapping[str, int]
    breakdown: tuple[UsageBreakdownRow, ...]
    breakdown_truncated: bool
    breakdown_omitted: int
    coverage: UsageCoverage

    def to_dict(self) -> dict[str, object]:
        """Serialize the public aggregate report shape."""
        return {
            "scope": self.scope,
            "window": {"start": self.start, "end": self.end, "timezone": self.timezone},
            "as_of": self.as_of,
            "totals": self.totals.to_dict(),
            "cost": self.cost.to_dict(),
            "turn_count": self.turn_count,
            "run_count": self.run_count,
            "session_count": self.session_count,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "status_counts": dict(self.status_counts),
            "breakdown": [row.to_dict() for row in self.breakdown],
            "breakdown_truncated": self.breakdown_truncated,
            "breakdown_omitted": self.breakdown_omitted,
            "coverage": self.coverage.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _MetricContribution:
    model_type: str
    provider: str
    model_id: str
    totals: TokenTotals
    cost: Decimal | None


@dataclass(frozen=True, slots=True)
class _DirectRunEntity:
    """The one attributed entity for a direct retained run."""

    kind: Literal["agent", "team"]
    entity_id: str


@dataclass(frozen=True, slots=True)
class _UsageMetricRecord:
    """One normalized retained metric contribution with no raw persisted payload."""

    entity_id: str
    requester_id: str | None
    created_at: datetime
    model_type: str
    provider: str
    model_id: str
    status: str
    totals: TokenTotals
    cost: Decimal | None


@dataclass(slots=True)
class _Aggregate:
    totals: TokenTotals = TokenTotals()
    known_cost: Decimal = Decimal(0)
    runs_with_cost: int = 0
    runs_without_cost: int = 0
    run_count: int = 0

    def add(self, *, totals: TokenTotals, cost: Decimal | None) -> None:
        self.totals = _add_totals(self.totals, totals)
        if cost is None:
            self.runs_without_cost += 1
        else:
            self.known_cost += cost
            self.runs_with_cost += 1
        self.run_count += 1

    def cost_coverage(self) -> CostCoverage:
        return CostCoverage(
            known_cost=str(self.known_cost),
            runs_with_cost=self.runs_with_cost,
            runs_without_cost=self.runs_without_cost,
        )


@dataclass(slots=True)
class _CollectionState:
    aggregate: _Aggregate
    breakdowns: dict[_BreakdownKey, _Aggregate]
    status_counts: dict[str, int]
    included_sessions: set[tuple[str, str]]
    observed_at: list[datetime]
    partial_source_labels: set[str]
    unreadable_source_labels: set[str]
    readable_source_labels: set[str]
    stable_run_ids: set[tuple[str, str, str]]
    history_stable_run_ids: set[tuple[str, str, str, str, str]]
    structural_run_ids: set[tuple[str, str, str]]
    row_history_totals: dict[tuple[str, str], TokenTotals]
    row_session_metrics: dict[tuple[str, str], TokenTotals | None]
    row_comparison_allowed: dict[tuple[str, str], bool]
    row_history_complete: dict[tuple[str, str], bool]
    top_level_turn_ids: set[tuple[str, str, str]]
    scanned_sources: int = 0
    scanned_sessions: int = 0
    retained_runs: int = 0
    turn_count: int = 0
    skipped_runs: int = 0
    malformed_runs: int = 0
    missing_requester_runs: int = 0
    missing_timestamp_runs: int = 0
    coverage_exclusions: int = 0


def parse_usage_window(
    *,
    start: str | None,
    end: str | None,
    timezone_name: str,
    as_of: datetime,
) -> tuple[datetime | None, datetime]:
    """Parse an inclusive UTC start and exclusive UTC end for a usage query."""
    timezone = _usage_timezone(timezone_name)
    normalized_as_of = _as_utc(as_of, error="as_of must include a timezone")
    parsed_start = _parse_boundary(start, timezone=timezone, is_end=False) if start is not None else None
    parsed_end = _parse_boundary(end, timezone=timezone, is_end=True) if end is not None else normalized_as_of
    if parsed_start is not None and parsed_start >= parsed_end:
        msg = "start must be before end"
        raise UsageStatsValidationError(msg)
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
    """Collect direct retained usage for one agent and requester without session totals."""
    _validate_group_by(group_by, allowed=_SELF_GROUPS)
    query_as_of = _query_as_of(as_of)
    window_start, window_end = parse_usage_window(
        start=start,
        end=end,
        timezone_name=config.timezone,
        as_of=query_as_of,
    )
    expected_requester = config.authorization.resolve_alias(requester_id)
    sources = discover_self_usage_sources(
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    state = _collect(
        sources=sources,
        config=config,
        scope="self",
        group_by=group_by,
        window_start=window_start,
        window_end=window_end,
        expected_agent=agent_name,
        expected_requester=expected_requester,
        entity_filter=None,
        requester_filter=None,
    )
    return _report(
        state=state,
        scope="self",
        start=window_start,
        end=window_end,
        timezone_name=config.timezone,
        as_of=query_as_of,
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
    """Collect direct retained usage from current configured agent and team sources."""
    _validate_group_by(group_by, allowed=_ADMIN_GROUPS)
    entity_filter = _validate_entity_filter(entity_names, config)
    requester_filter = (
        frozenset(config.authorization.resolve_alias(requester_id) for requester_id in requester_ids)
        if requester_ids is not None
        else None
    )
    query_as_of = _query_as_of(as_of)
    window_start, window_end = parse_usage_window(
        start=start,
        end=end,
        timezone_name=config.timezone,
        as_of=query_as_of,
    )
    sources = discover_admin_usage_sources(config=config, runtime_paths=runtime_paths)
    state = _collect(
        sources=sources,
        config=config,
        scope="admin",
        group_by=group_by,
        window_start=window_start,
        window_end=window_end,
        expected_agent=None,
        expected_requester=None,
        entity_filter=entity_filter,
        requester_filter=requester_filter,
    )
    return _report(
        state=state,
        scope="admin",
        start=window_start,
        end=window_end,
        timezone_name=config.timezone,
        as_of=query_as_of,
    )


def _collect(
    *,
    sources: Iterable[UsageStorageSource],
    config: Config,
    scope: Literal["self", "admin"],
    group_by: _UsageGroupBy,
    window_start: datetime | None,
    window_end: datetime,
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
) -> _CollectionState:
    state = _CollectionState(
        aggregate=_Aggregate(),
        breakdowns={},
        status_counts=defaultdict(int),
        included_sessions=set(),
        observed_at=[],
        partial_source_labels=set(),
        unreadable_source_labels=set(),
        readable_source_labels=set(),
        stable_run_ids=set(),
        history_stable_run_ids=set(),
        structural_run_ids=set(),
        row_history_totals={},
        row_session_metrics={},
        row_comparison_allowed={},
        row_history_complete={},
        top_level_turn_ids=set(),
    )
    timezone = _usage_timezone(config.timezone)
    for source in sources:
        state.scanned_sources += 1
        source_had_outcome = False
        for outcome in iter_usage_storage_rows(source):
            source_had_outcome = True
            if isinstance(outcome, UsageStorageDiagnostic):
                if outcome.status != "absent":
                    state.partial_source_labels.add(outcome.path_label)
                    state.unreadable_source_labels.add(outcome.path_label)
                continue
            state.readable_source_labels.add(source.path_label)
            _collect_row(
                state=state,
                row=outcome,
                config=config,
                scope=scope,
                group_by=group_by,
                timezone=timezone,
                window_start=window_start,
                window_end=window_end,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
                entity_filter=entity_filter,
                requester_filter=requester_filter,
            )
        if not source_had_outcome:
            state.readable_source_labels.add(source.path_label)
    if scope == "self" and not state.readable_source_labels and state.unreadable_source_labels:
        msg = "Usage source unavailable"
        raise UsageStatsSourceUnavailableError(msg)
    return state


def _collect_row(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    config: Config,
    scope: Literal["self", "admin"],
    group_by: _UsageGroupBy,
    timezone: ZoneInfo,
    window_start: datetime | None,
    window_end: datetime,
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
) -> None:
    if scope == "admin" or row.source.requester_isolated:
        _initialize_row_tracking(state, row, comparison_allowed=True)
    for index, run in enumerate(row.runs):
        _collect_run_tree(
            state=state,
            row=row,
            run=run,
            is_top_level=True,
            parent_requester=None,
            parent_timestamp=None,
            structural_key=str(index),
            config=config,
            scope=scope,
            group_by=group_by,
            timezone=timezone,
            window_start=window_start,
            window_end=window_end,
            expected_agent=expected_agent,
            expected_requester=expected_requester,
            entity_filter=entity_filter,
            requester_filter=requester_filter,
        )


def _initialize_row_tracking(
    state: _CollectionState,
    row: UsageSessionRow,
    *,
    comparison_allowed: bool,
) -> None:
    row_key = (row.source.path_label, row.row_key)
    if row_key in state.row_history_totals:
        return
    state.scanned_sessions += 1
    state.row_history_totals[row_key] = TokenTotals()
    state.row_session_metrics[row_key] = _metrics_totals(row.session_metrics)
    state.row_comparison_allowed[row_key] = comparison_allowed
    state.row_history_complete[row_key] = True


def _collect_run_tree(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    run: UsageRunNode,
    is_top_level: bool,
    parent_requester: str | None,
    parent_timestamp: datetime | None,
    structural_key: str,
    config: Config,
    scope: Literal["self", "admin"],
    group_by: _UsageGroupBy,
    timezone: ZoneInfo,
    window_start: datetime | None,
    window_end: datetime,
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
) -> None:
    requester = config.authorization.resolve_alias(run.requester_id) if run.requester_id else parent_requester
    entity = _run_entity(row=row, run=run, is_top_level=is_top_level)
    timestamp = parent_timestamp if run.created_at is None else _run_timestamp(run.created_at)
    visible_to_scope = (
        scope == "admin"
        or row.source.requester_isolated
        or _matches_self_identity(
            entity=entity,
            requester=requester,
            expected_agent=expected_agent,
            expected_requester=expected_requester,
        )
    )
    if visible_to_scope:
        _initialize_row_tracking(
            state,
            row,
            comparison_allowed=scope == "admin" or row.source.requester_isolated,
        )
        if entity is None or timestamp is None:
            state.malformed_runs += entity is None
            state.missing_timestamp_runs += timestamp is None
            state.missing_requester_runs += requester is None
            state.skipped_runs += 1
            state.coverage_exclusions += 1
            _mark_row_history_incomplete(state, row)
        else:
            _collect_normalized_run(
                state=state,
                row=row,
                run=run,
                entity=entity,
                requester=requester,
                timestamp=timestamp,
                structural_key=structural_key,
                is_top_level=is_top_level,
                scope=scope,
                group_by=group_by,
                timezone=timezone,
                window_start=window_start,
                window_end=window_end,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
                entity_filter=entity_filter,
                requester_filter=requester_filter,
            )
    for index, child in enumerate(run.member_responses):
        _collect_run_tree(
            state=state,
            row=row,
            run=child,
            is_top_level=False,
            parent_requester=requester,
            parent_timestamp=timestamp,
            structural_key=f"{structural_key}.{index}",
            config=config,
            scope=scope,
            group_by=group_by,
            timezone=timezone,
            window_start=window_start,
            window_end=window_end,
            expected_agent=expected_agent,
            expected_requester=expected_requester,
            entity_filter=entity_filter,
            requester_filter=requester_filter,
        )


def _matches_self_identity(
    *,
    entity: _DirectRunEntity | None,
    requester: str | None,
    expected_agent: str | None,
    expected_requester: str | None,
) -> bool:
    return (
        requester is not None
        and requester == expected_requester
        and entity is not None
        and entity.kind == "agent"
        and entity.entity_id == expected_agent
    )


def _collect_normalized_run(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    run: UsageRunNode,
    entity: _DirectRunEntity,
    requester: str | None,
    timestamp: datetime,
    structural_key: str,
    is_top_level: bool,
    scope: Literal["self", "admin"],
    group_by: _UsageGroupBy,
    timezone: ZoneInfo,
    window_start: datetime | None,
    window_end: datetime,
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
) -> None:
    if not _admit_structural_run(state=state, row=row, run=run, structural_key=structural_key):
        return
    accepted = _accept_run(
        entity=entity,
        requester=requester,
        timestamp=timestamp,
        scope=scope,
        expected_agent=expected_agent,
        expected_requester=expected_requester,
        entity_filter=entity_filter,
        requester_filter=requester_filter,
        window_start=window_start,
        window_end=window_end,
    )
    if is_top_level and accepted:
        _count_top_level_turn(
            state=state,
            row=row,
            run=run,
            entity=entity,
            structural_key=structural_key,
        )
    contributions = _run_contributions(run)
    if contributions is None:
        state.malformed_runs += 1
        state.missing_requester_runs += requester is None
        state.skipped_runs += 1
        state.coverage_exclusions += 1
        _mark_row_history_incomplete(state, row)
        return
    records = tuple(
        _UsageMetricRecord(
            entity_id=entity.entity_id,
            requester_id=requester,
            created_at=timestamp,
            model_type=contribution.model_type,
            provider=contribution.provider,
            model_id=contribution.model_id,
            status=run.status,
            totals=contribution.totals,
            cost=contribution.cost,
        )
        for contribution in contributions
    )
    _record_row_history(state=state, row=row, run=run, entity=entity, records=records)
    if not _admit_stable_run(state=state, run=run, entity=entity):
        return
    if requester is None:
        state.missing_requester_runs += 1
    if not accepted:
        if requester is None and (scope == "self" or requester_filter is not None):
            state.coverage_exclusions += 1
            _mark_row_history_incomplete(state, row)
        state.skipped_runs += 1
        return
    if not records:
        state.skipped_runs += 1
        return
    _aggregate_records(state=state, row=row, records=records, group_by=group_by, timezone=timezone)


def _admit_structural_run(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    run: UsageRunNode,
    structural_key: str,
) -> bool:
    if run.run_id is not None:
        return True
    identity = (row.source.path_label, row.row_key, structural_key)
    if identity in state.structural_run_ids:
        return False
    state.structural_run_ids.add(identity)
    return True


def _record_row_history(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    run: UsageRunNode,
    entity: _DirectRunEntity,
    records: tuple[_UsageMetricRecord, ...],
) -> None:
    row_key = (row.source.path_label, row.row_key)
    if run.run_id is not None:
        history_key = (*row_key, entity.kind, entity.entity_id, run.run_id)
        if history_key in state.history_stable_run_ids:
            return
        state.history_stable_run_ids.add(history_key)
    state.row_history_totals[row_key] = _add_totals(
        state.row_history_totals[row_key],
        _records_totals(records),
    )


def _admit_stable_run(
    *,
    state: _CollectionState,
    run: UsageRunNode,
    entity: _DirectRunEntity,
) -> bool:
    if run.run_id is None:
        return True
    identity = (entity.kind, entity.entity_id, run.run_id)
    if identity in state.stable_run_ids:
        return False
    state.stable_run_ids.add(identity)
    return True


def _count_top_level_turn(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    run: UsageRunNode,
    entity: _DirectRunEntity,
    structural_key: str,
) -> None:
    identity = (
        (entity.kind, entity.entity_id, run.run_id)
        if run.run_id is not None
        else (row.source.path_label, row.row_key, structural_key)
    )
    if identity in state.top_level_turn_ids:
        return
    state.top_level_turn_ids.add(identity)
    state.turn_count += 1


def _mark_row_history_incomplete(state: _CollectionState, row: UsageSessionRow) -> None:
    state.row_history_complete[(row.source.path_label, row.row_key)] = False


def _aggregate_records(
    *,
    state: _CollectionState,
    row: UsageSessionRow,
    records: tuple[_UsageMetricRecord, ...],
    group_by: _UsageGroupBy,
    timezone: ZoneInfo,
) -> None:
    representative = records[0]
    state.retained_runs += 1
    state.included_sessions.add((row.source.path_label, row.row_key))
    state.observed_at.append(representative.created_at)
    state.status_counts[representative.status] += 1
    run_totals = TokenTotals()
    run_cost = Decimal(0)
    has_cost = False
    per_breakdown: dict[_BreakdownKey, _Aggregate] = {}
    for record in records:
        run_totals = _add_totals(run_totals, record.totals)
        if record.cost is not None:
            run_cost += record.cost
            has_cost = True
        key = _record_breakdown_key(group_by=group_by, record=record, timezone=timezone)
        per_breakdown.setdefault(key, _Aggregate()).add(totals=record.totals, cost=record.cost)
    for key, aggregate in per_breakdown.items():
        state.breakdowns.setdefault(key, _Aggregate()).add(
            totals=aggregate.totals,
            cost=aggregate.known_cost if aggregate.runs_with_cost else None,
        )
    state.aggregate.add(totals=run_totals, cost=run_cost if has_cost else None)


def _accept_run(
    *,
    entity: _DirectRunEntity | None,
    requester: str | None,
    timestamp: datetime,
    scope: Literal["self", "admin"],
    expected_agent: str | None,
    expected_requester: str | None,
    entity_filter: frozenset[str] | None,
    requester_filter: frozenset[str] | None,
    window_start: datetime | None,
    window_end: datetime,
) -> bool:
    if window_start is not None and timestamp < window_start:
        return False
    if timestamp >= window_end:
        return False
    if scope == "self":
        return _matches_self_identity(
            entity=entity,
            requester=requester,
            expected_agent=expected_agent,
            expected_requester=expected_requester,
        )
    if entity_filter is not None and (entity is None or entity.entity_id not in entity_filter):
        return False
    return requester_filter is None or requester in requester_filter


def _record_breakdown_key(
    *,
    group_by: _UsageGroupBy,
    record: _UsageMetricRecord,
    timezone: ZoneInfo,
) -> _BreakdownKey:
    if group_by == "model":
        return ("model", record.model_id, record.model_type, record.provider, record.model_id)
    if group_by == "entity":
        return ("entity", record.entity_id, None, None, None)
    if group_by == "requester":
        return ("requester", record.requester_id or "unknown", None, None, None)
    return ("day", record.created_at.astimezone(timezone).date().isoformat(), None, None, None)


def _metrics_totals(metrics: Mapping[str, object] | None) -> TokenTotals | None:
    if metrics is None:
        return None
    values: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        value = _token_value(metrics.get(field))
        if value is None and metrics.get(field) is not None:
            return None
        values[field] = value or 0
    return TokenTotals(**values)


def _records_totals(records: tuple[_UsageMetricRecord, ...]) -> TokenTotals:
    totals = TokenTotals()
    for record in records:
        totals = _add_totals(totals, record.totals)
    return totals


def _run_entity(  # noqa: PLR0911
    *,
    row: UsageSessionRow,
    run: UsageRunNode,
    is_top_level: bool,
) -> _DirectRunEntity | None:
    if is_top_level and row.source.scope in {"shared_agent", "private_agent"}:
        if row.source.source_agent_id in row.source.allowed_agent_ids:
            return _DirectRunEntity(kind="agent", entity_id=row.source.source_agent_id)
        return None
    if is_top_level:
        team_id = run.team_id or (row.entity_id if row.entity_kind == "team" else None)
        if team_id in row.source.allowed_team_ids:
            return _DirectRunEntity(kind="team", entity_id=team_id)
        return None
    if run.agent_id in row.source.allowed_agent_ids:
        return _DirectRunEntity(kind="agent", entity_id=run.agent_id)
    if run.team_id in row.source.allowed_team_ids:
        return _DirectRunEntity(kind="team", entity_id=run.team_id)
    return None


def _run_contributions(run: UsageRunNode) -> tuple[_MetricContribution, ...] | None:
    model_metrics = run.model_metrics
    if model_metrics:
        metered_model_metrics = tuple(metric for metric in model_metrics if _has_actual_metric(metric.metrics))
        contributions = tuple(_model_metric_contribution(metric) for metric in metered_model_metrics)
        if any(contribution is None for contribution in contributions):
            return None
        if contributions:
            return tuple(contribution for contribution in contributions if contribution is not None)
    if not _has_actual_metric(run.metrics):
        return ()
    contribution = _metric_contribution(
        model_type="model",
        provider=run.model_provider or "unknown",
        model_id=run.model_id or "unknown",
        metrics=run.metrics,
    )
    return None if contribution is None else (contribution,)


def _has_actual_metric(metrics: Mapping[str, object]) -> bool:
    return any(metrics.get(field) is not None for field in (*_TOKEN_FIELDS, "cost"))


def _model_metric_contribution(metric: UsageModelMetric) -> _MetricContribution | None:
    return _metric_contribution(
        model_type=metric.model_type,
        provider=metric.provider,
        model_id=metric.model_id,
        metrics=metric.metrics,
    )


def _metric_contribution(
    *,
    model_type: str,
    provider: str,
    model_id: str,
    metrics: Mapping[str, object],
) -> _MetricContribution | None:
    values: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        value = _token_value(metrics.get(field))
        if value is None and metrics.get(field) is not None:
            return None
        values[field] = value or 0
    cost = _cost_value(metrics.get("cost"))
    if cost is None and metrics.get("cost") is not None:
        return None
    return _MetricContribution(
        model_type=model_type or "unknown",
        provider=provider or "unknown",
        model_id=model_id or "unknown",
        totals=TokenTotals(**values),
        cost=cost,
    )


def _token_value(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_PERSISTED_INTEGER else None
    if isinstance(value, float):
        return (
            int(value) if math.isfinite(value) and 0 <= value <= _MAX_PERSISTED_INTEGER and value.is_integer() else None
        )
    if isinstance(value, str) and len(value) <= _MAX_PERSISTED_NUMERIC_TEXT_LENGTH and value.isdecimal():
        return int(value)
    return None


def _cost_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if (isinstance(value, int) and not 0 <= value <= _MAX_PERSISTED_INTEGER) or (
        isinstance(value, float) and (not math.isfinite(value) or not 0 <= value <= _MAX_PERSISTED_INTEGER)
    ):
        return None
    if isinstance(value, str) and len(value) > _MAX_PERSISTED_NUMERIC_TEXT_LENGTH:
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return (
        cost if cost.is_finite() and cost >= 0 and abs(cost.adjusted()) <= _MAX_PERSISTED_NUMERIC_TEXT_LENGTH else None
    )


def _report(
    *,
    state: _CollectionState,
    scope: Literal["self", "admin"],
    start: datetime | None,
    end: datetime,
    timezone_name: str,
    as_of: datetime,
) -> UsageReport:
    rows = tuple(
        UsageBreakdownRow(
            dimension=dimension,
            key=key,
            model_type=model_type if dimension == "model" else None,
            provider=provider if dimension == "model" else None,
            model_id=model_id if dimension == "model" else None,
            totals=aggregate.totals,
            cost=aggregate.cost_coverage(),
            run_count=aggregate.run_count,
        )
        for (dimension, key, model_type, provider, model_id), aggregate in state.breakdowns.items()
    )
    sorted_rows = tuple(
        sorted(
            rows,
            key=lambda row: (
                -row.totals.total_tokens,
                row.dimension,
                row.key,
                row.model_type or "",
                row.provider or "",
                row.model_id or "",
            ),
        ),
    )
    retained_rows = sorted_rows[:_MAX_BREAKDOWN_ROWS]
    coverage_status: Literal["complete_retained", "partial", "unknown"]
    partial_sources = len(state.partial_source_labels)
    compacted_sessions, comparison_unknown = _coverage_comparison(state)
    if partial_sources or compacted_sessions or state.coverage_exclusions:
        coverage_status = "partial"
    elif comparison_unknown or not state.scanned_sources:
        coverage_status = "unknown"
    else:
        coverage_status = "complete_retained"
    return UsageReport(
        scope=scope,
        start=_format_timestamp(start) if start is not None else None,
        end=_format_timestamp(end),
        timezone=timezone_name,
        as_of=_format_timestamp(as_of),
        totals=state.aggregate.totals,
        cost=state.aggregate.cost_coverage(),
        turn_count=state.turn_count,
        run_count=state.aggregate.run_count,
        session_count=len(state.included_sessions),
        first_observed_at=_format_timestamp(min(state.observed_at)) if state.observed_at else None,
        last_observed_at=_format_timestamp(max(state.observed_at)) if state.observed_at else None,
        status_counts=MappingProxyType(dict(sorted(state.status_counts.items()))),
        breakdown=retained_rows,
        breakdown_truncated=len(sorted_rows) > _MAX_BREAKDOWN_ROWS,
        breakdown_omitted=max(0, len(sorted_rows) - _MAX_BREAKDOWN_ROWS),
        coverage=UsageCoverage(
            status=coverage_status,
            scanned_sources=state.scanned_sources,
            partial_sources=partial_sources,
            scanned_sessions=state.scanned_sessions,
            retained_runs=state.retained_runs,
            skipped_runs=state.skipped_runs,
            malformed_runs=state.malformed_runs,
            missing_requester_runs=state.missing_requester_runs,
            missing_timestamp_runs=state.missing_timestamp_runs,
            compacted_sessions=compacted_sessions,
            note="Retained run usage only; session compaction can make retained history incomplete.",
        ),
    )


def _coverage_comparison(state: _CollectionState) -> tuple[int, bool]:
    compacted_sessions = 0
    unknown = not state.row_session_metrics
    for row_key, retained in state.row_history_totals.items():
        if not state.row_history_complete[row_key]:
            unknown = True
            continue
        if not state.row_comparison_allowed[row_key]:
            unknown = True
            continue
        cumulative = state.row_session_metrics[row_key]
        if cumulative is None:
            unknown = True
            continue
        if _totals_dominate(cumulative, retained):
            compacted_sessions += 1
        elif cumulative != retained:
            unknown = True
    return compacted_sessions, unknown


def _totals_dominate(left: TokenTotals, right: TokenTotals) -> bool:
    left_values = left.to_dict().values()
    right_values = right.to_dict().values()
    pairs = tuple(zip(left_values, right_values, strict=True))
    return all(left_value >= right_value for left_value, right_value in pairs) and any(
        left_value > right_value for left_value, right_value in pairs
    )


def _validate_entity_filter(entity_names: tuple[str, ...] | None, config: Config) -> frozenset[str] | None:
    if entity_names is None:
        return None
    known_entities = frozenset(config.agents) | frozenset(config.teams)
    unknown = sorted(set(entity_names) - known_entities)
    if unknown:
        msg = "Unknown entity filter"
        raise UsageStatsValidationError(msg)
    return frozenset(entity_names)


def _validate_group_by(group_by: object, *, allowed: frozenset[str]) -> None:
    if group_by not in allowed:
        msg = "Unsupported usage statistics group_by"
        raise UsageStatsValidationError(msg)


def _query_as_of(as_of: datetime | None) -> datetime:
    return _as_utc(as_of or datetime.now(UTC), error="as_of must include a timezone")


def _usage_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        msg = "Unknown timezone"
        raise UsageStatsValidationError(msg) from error


def _parse_boundary(value: str, *, timezone: ZoneInfo, is_end: bool) -> datetime:
    if _DATE_ONLY.fullmatch(value):
        try:
            local_date = date.fromisoformat(value)
            if is_end:
                local_date += timedelta(days=1)
        except ValueError as error:
            msg = "Invalid date"
            raise UsageStatsValidationError(msg) from error
        except OverflowError as error:
            msg = "Invalid date"
            raise UsageStatsValidationError(msg) from error
        boundary = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone)
        return _as_utc(boundary, error="Invalid date")
    if _TIMESTAMP_OFFSET.fullmatch(value) is None:
        msg = "Timestamp must include Z or an explicit offset"
        raise UsageStatsValidationError(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        msg = "Invalid timestamp"
        raise UsageStatsValidationError(msg) from error
    return _as_utc(parsed, error="Invalid timestamp")


def _run_timestamp(value: str | None) -> datetime | None:  # noqa: PLR0911
    if value is None:
        return None
    try:
        numeric = Decimal(value)
    except InvalidOperation:
        numeric = None
    if numeric is not None:
        if not numeric.is_finite():
            return None
        try:
            return datetime.fromtimestamp(float(numeric), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if _TIMESTAMP_OFFSET.fullmatch(value) is None:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value), error="timestamp")
    except (UsageStatsValidationError, ValueError):
        return None


def _as_utc(value: datetime, *, error: str) -> datetime:
    try:
        utc_offset = value.utcoffset()
    except (OverflowError, ValueError) as cause:
        raise UsageStatsValidationError(error) from cause
    if value.tzinfo is None or utc_offset is None:
        raise UsageStatsValidationError(error)
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as cause:
        raise UsageStatsValidationError(error) from cause


def _add_totals(left: TokenTotals, right: TokenTotals) -> TokenTotals:
    return TokenTotals(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        audio_input_tokens=left.audio_input_tokens + right.audio_input_tokens,
        audio_output_tokens=left.audio_output_tokens + right.audio_output_tokens,
        audio_total_tokens=left.audio_total_tokens + right.audio_total_tokens,
    )


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
