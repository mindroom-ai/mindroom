"""Read-only aggregation of retained Agno token usage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.usage_stats_storage import (
    UsageReadBudget,
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
    "TokenTotals",
    "UsageBreakdownRow",
    "UsageCoverage",
    "UsageReport",
    "collect_admin_usage",
    "collect_self_usage",
]

type _Scope = Literal["self", "admin"]

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
_BREAKDOWN_LIMIT = 200
_MAX_ROWS_PER_REQUEST = 250
_MAX_RUNS_PER_REQUEST = 25_000
_MAX_BYTES_PER_REQUEST = 64_000_000
_COVERAGE_NOTE = (
    "Self totals use requester-attributed retained agent runs. "
    "Admin totals use Agno session aggregates, including team members. "
    "Deleted sessions are unavailable."
)


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
        """Add another aggregate's counters."""
        return TokenTotals(**{field: getattr(self, field) + getattr(other, field) for field in _TOKEN_FIELDS})


@dataclass(frozen=True, slots=True)
class UsageBreakdownRow:
    """One configured entity in an admin report."""

    key: str
    totals: TokenTotals
    session_count: int

    def to_dict(self) -> dict[str, object]:
        """Return the public breakdown row."""
        return {
            "dimension": "entity",
            "key": self.key,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
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
    totals: TokenTotals
    session_count: int
    breakdown: tuple[UsageBreakdownRow, ...]
    breakdown_truncated: bool
    coverage: UsageCoverage

    def to_dict(self) -> dict[str, object]:
        """Return the stable custom-tool payload fields."""
        return {
            "scope": self.scope,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
            "breakdown": [row.to_dict() for row in self.breakdown],
            "breakdown_truncated": self.breakdown_truncated,
            "coverage": self.coverage.to_dict(),
        }


@dataclass(slots=True)
class _Aggregate:
    totals: TokenTotals = TokenTotals()
    session_count: int = 0

    def add(self, totals: TokenTotals) -> None:
        self.totals = self.totals.plus(totals)
        self.session_count += 1


def collect_self_usage(
    *,
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> UsageReport:
    """Collect all retained direct usage for this requester and agent."""
    return _collect_usage(
        sources=discover_self_usage_sources(
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        ),
        config=config,
        scope="self",
        expected_agent=agent_name,
        expected_requester=config.authorization.resolve_alias(requester_id),
    )


def collect_admin_usage(*, config: Config, runtime_paths: RuntimePaths) -> UsageReport:
    """Collect all retained session aggregates across configured entities."""
    return _collect_usage(
        sources=discover_admin_usage_sources(config=config, runtime_paths=runtime_paths),
        config=config,
        scope="admin",
        expected_agent=None,
        expected_requester=None,
    )


def _collect_usage(
    *,
    sources: Iterable[UsageStorageSource | UsageStorageDiagnostic],
    config: Config,
    scope: _Scope,
    expected_agent: str | None,
    expected_requester: str | None,
) -> UsageReport:
    total = TokenTotals()
    sessions: set[tuple[str, str]] = set()
    buckets: dict[str, _Aggregate] = {}
    seen_runs: set[tuple[str, str, str]] = set()
    scanned_sources: set[str] = set()
    unavailable_sources: set[str] = set()
    truncated = False
    budget = UsageReadBudget(
        row_limit=_MAX_ROWS_PER_REQUEST,
        byte_limit=_MAX_BYTES_PER_REQUEST,
        run_limit=_MAX_RUNS_PER_REQUEST,
    )

    for discovered in sources:
        if isinstance(discovered, UsageStorageDiagnostic):
            unavailable_sources.add(discovered.path_label)
            truncated = truncated or discovered.status == "resource_limit"
            continue
        source = discovered
        scanned_sources.add(source.path_label)
        mode = "runs" if scope == "self" else "session_metrics"
        for item in iter_usage_storage_rows(source, mode=mode, budget=budget):
            if isinstance(item, UsageStorageDiagnostic):
                unavailable_sources.add(item.path_label)
                truncated = truncated or item.status == "resource_limit"
                continue
            try:
                if scope == "self":
                    row_totals = _self_row_totals(
                        item,
                        config=config,
                        expected_agent=expected_agent,
                        expected_requester=expected_requester,
                        seen_runs=seen_runs,
                    )
                    entity_id = None
                else:
                    entity_id = _admin_entity_id(item)
                    row_totals = _metrics_totals(item.session_metrics) if entity_id is not None else None
            except ValueError:
                unavailable_sources.add(source.path_label)
                continue
            if row_totals is None:
                continue
            total = total.plus(row_totals)
            sessions.add((source.path_label, item.row_key))
            if entity_id is not None:
                buckets.setdefault(entity_id, _Aggregate()).add(row_totals)
        if budget.exhausted:
            break

    breakdown = tuple(
        UsageBreakdownRow(key=entity_id, totals=aggregate.totals, session_count=aggregate.session_count)
        for entity_id, aggregate in sorted(
            buckets.items(),
            key=lambda item: (-item[1].totals.total_tokens, item[0]),
        )[:_BREAKDOWN_LIMIT]
    )
    return UsageReport(
        scope=scope,
        totals=total,
        session_count=len(sessions),
        breakdown=breakdown,
        breakdown_truncated=len(buckets) > _BREAKDOWN_LIMIT,
        coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(unavailable_sources),
            truncated=truncated,
        ),
    )


def _self_row_totals(
    row: UsageSessionRow,
    *,
    config: Config,
    expected_agent: str | None,
    expected_requester: str | None,
    seen_runs: set[tuple[str, str, str]],
) -> TokenTotals | None:
    if row.source.source_agent_id != expected_agent or expected_agent not in row.source.allowed_agent_ids:
        return None
    total = TokenTotals()
    accepted = False
    for run in row.runs:
        requester_id = config.authorization.resolve_alias(run.requester_id) if run.requester_id else None
        if not row.source.requester_isolated:
            if requester_id is None:
                raise ValueError
            if requester_id != expected_requester:
                continue
        run_totals = _metrics_totals(run.metrics)
        if run_totals is None:
            continue
        if run.run_id is not None:
            identity = (row.source.path_label, row.row_key, run.run_id)
            if identity in seen_runs:
                continue
            seen_runs.add(identity)
        total = total.plus(run_totals)
        accepted = True
    return total if accepted else None


def _admin_entity_id(row: UsageSessionRow) -> str | None:
    if row.source.scope in {"shared_agent", "private_agent"}:
        entity_id = row.source.source_agent_id
        return entity_id if entity_id in row.source.allowed_agent_ids else None
    return row.entity_id if row.entity_kind == "team" and row.entity_id in row.source.allowed_team_ids else None


def _metrics_totals(metrics: Mapping[str, object]) -> TokenTotals | None:
    if not any(metrics.get(field) is not None for field in _TOKEN_FIELDS):
        return None
    values: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        value = _token_value(metrics.get(field))
        if value is None:
            raise ValueError
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
