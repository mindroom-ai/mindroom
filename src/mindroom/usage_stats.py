"""Read-only aggregation of retained Agno token usage."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Literal

from mindroom.usage_stats_storage import (
    TOKEN_FIELDS,
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
    "TokenTotals",
    "UsageBreakdownRow",
    "UsageCoverage",
    "UsageModelBreakdownRow",
    "UsageReport",
    "collect_admin_usage",
    "collect_self_usage",
]

type _Scope = Literal["self", "admin"]

_COVERAGE_NOTE = (
    "Shared self totals use requester-attributed retained agent runs. "
    "Private self and admin totals use Agno session aggregates, including team members. "
    "Deleted sessions are unavailable."
)
_MODEL_COVERAGE_NOTE = (
    "Model breakdown uses retained top-level runs with usable token metrics. "
    "It does not necessarily sum to report totals, which may include compacted history "
    "and nested team-member usage."
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
        return TokenTotals(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            audio_input_tokens=self.audio_input_tokens + other.audio_input_tokens,
            audio_output_tokens=self.audio_output_tokens + other.audio_output_tokens,
            audio_total_tokens=self.audio_total_tokens + other.audio_total_tokens,
        )


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
    note: str = _COVERAGE_NOTE

    def to_dict(self) -> dict[str, object]:
        """Return coverage without implying billing completeness."""
        return {
            "scanned_sources": self.scanned_sources,
            "unavailable_sources": self.unavailable_sources,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class UsageModelBreakdownRow:
    """Retained top-level usage for one provider and model."""

    model_provider: str
    model: str
    totals: TokenTotals
    run_count: int

    def to_dict(self) -> dict[str, object]:
        """Return the public model breakdown row."""
        return {
            "provider": self.model_provider,
            "model": self.model,
            "totals": self.totals.to_dict(),
            "run_count": self.run_count,
        }


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Aggregate-only retained token usage."""

    scope: _Scope
    totals: TokenTotals
    session_count: int
    breakdown: tuple[UsageBreakdownRow, ...]
    coverage: UsageCoverage
    model_breakdown: tuple[UsageModelBreakdownRow, ...]
    model_coverage: UsageCoverage

    def to_dict(self) -> dict[str, object]:
        """Return the stable custom-tool payload fields."""
        return {
            "scope": self.scope,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
            "breakdown": [row.to_dict() for row in self.breakdown],
            "coverage": self.coverage.to_dict(),
            "model_breakdown": [row.to_dict() for row in self.model_breakdown],
            "model_coverage": self.model_coverage.to_dict(),
        }


@dataclass(slots=True)
class _Aggregate:
    totals: TokenTotals = TokenTotals()
    count: int = 0

    def add(self, totals: TokenTotals) -> None:
        self.totals = self.totals.plus(totals)
        self.count += 1


@dataclass(slots=True)
class _UsageAccumulator:
    total: TokenTotals = TokenTotals()
    sessions: set[tuple[str, str]] = dataclass_field(default_factory=set)
    buckets: dict[str, _Aggregate] = dataclass_field(default_factory=dict)
    seen_runs: set[tuple[str, str, str]] = dataclass_field(default_factory=set)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)

    def add_row(
        self,
        row: UsageSessionRow,
        *,
        config: Config,
        scope: _Scope,
        expected_agent: str | None,
        expected_requester: str | None,
    ) -> None:
        if not row.session_metrics_available:
            self.unavailable_sources.add(row.source.path_label)
            return
        try:
            if scope == "self":
                row_totals = _self_row_totals(
                    row,
                    config=config,
                    expected_agent=expected_agent,
                    expected_requester=expected_requester,
                    seen_runs=self.seen_runs,
                )
                entity_id = None
            else:
                entity_id = _admin_entity_id(row)
                row_totals = _metrics_totals(row.session_metrics) if entity_id is not None else None
        except ValueError:
            self.unavailable_sources.add(row.source.path_label)
            return
        if row_totals is None:
            return
        self.total = self.total.plus(row_totals)
        self.sessions.add((row.source.path_label, row.row_key))
        if entity_id is not None:
            self.buckets.setdefault(entity_id, _Aggregate()).add(row_totals)


@dataclass(slots=True)
class _ModelUsageAccumulator:
    buckets: dict[tuple[str, str], _Aggregate] = dataclass_field(default_factory=dict)
    seen_runs: set[tuple[str, str, str]] = dataclass_field(default_factory=set)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)

    def add_row(
        self,
        row: UsageSessionRow,
        *,
        config: Config,
        scope: _Scope,
        expected_agent: str | None,
        expected_requester: str | None,
    ) -> None:
        if not row.runs_available:
            self.unavailable_sources.add(row.source.path_label)
            return
        try:
            entries = _model_entries_for_row(
                row,
                config=config,
                scope=scope,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
            )
        except ValueError:
            self.unavailable_sources.add(row.source.path_label)
            return
        _add_model_entries(
            entries,
            source_path=row.source.path_label,
            row_key=row.row_key,
            buckets=self.buckets,
            seen_runs=self.seen_runs,
        )


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
    usage = _UsageAccumulator()
    model_usage = _ModelUsageAccumulator()
    scanned_sources: set[str] = set()
    for discovered in sources:
        if isinstance(discovered, UsageStorageDiagnostic):
            usage.unavailable_sources.add(discovered.path_label)
            model_usage.unavailable_sources.add(discovered.path_label)
            continue
        source = discovered
        scanned_sources.add(source.path_label)
        mode = "runs" if scope == "self" and not source.requester_isolated else "both"
        for item in iter_usage_storage_rows(source, mode=mode):
            if isinstance(item, UsageStorageDiagnostic):
                usage.unavailable_sources.add(item.path_label)
                model_usage.unavailable_sources.add(item.path_label)
                continue
            usage.add_row(
                item,
                config=config,
                scope=scope,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
            )
            model_usage.add_row(
                item,
                config=config,
                scope=scope,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
            )

    breakdown = tuple(
        UsageBreakdownRow(key=entity_id, totals=aggregate.totals, session_count=aggregate.count)
        for entity_id, aggregate in sorted(
            usage.buckets.items(),
            key=lambda item: (-item[1].totals.total_tokens, item[0]),
        )
    )
    model_breakdown = _model_breakdown(model_usage.buckets)
    model_coverage = UsageCoverage(
        scanned_sources=len(scanned_sources),
        unavailable_sources=len(model_usage.unavailable_sources),
        note=_MODEL_COVERAGE_NOTE,
    )
    return UsageReport(
        scope=scope,
        totals=usage.total,
        session_count=len(usage.sessions),
        breakdown=breakdown,
        coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(usage.unavailable_sources),
        ),
        model_breakdown=model_breakdown,
        model_coverage=model_coverage,
    )


def _model_breakdown(
    buckets: Mapping[tuple[str, str], _Aggregate],
) -> tuple[UsageModelBreakdownRow, ...]:
    return tuple(
        UsageModelBreakdownRow(
            model_provider=key[0],
            model=key[1],
            totals=aggregate.totals,
            run_count=aggregate.count,
        )
        for key, aggregate in sorted(
            buckets.items(),
            key=lambda item: (-item[1].totals.total_tokens, item[0]),
        )
    )


def _add_model_entries(
    entries: Iterable[tuple[UsageRunNode, TokenTotals]],
    *,
    source_path: str,
    row_key: str,
    buckets: dict[tuple[str, str], _Aggregate],
    seen_runs: set[tuple[str, str, str]],
) -> None:
    row_seen_runs: set[tuple[str, str, str]] = set()
    accepted_entries: list[tuple[tuple[str, str], TokenTotals]] = []
    for run, totals in entries:
        if run.run_id is not None:
            identity = (source_path, row_key, run.run_id)
            if identity in seen_runs or identity in row_seen_runs:
                continue
            row_seen_runs.add(identity)
        key = (run.model_provider or "unknown", run.model or "unknown")
        accepted_entries.append((key, totals))
    seen_runs.update(row_seen_runs)
    for key, totals in accepted_entries:
        buckets.setdefault(key, _Aggregate()).add(totals)


def _model_entries_for_row(
    row: UsageSessionRow,
    *,
    config: Config,
    scope: _Scope,
    expected_agent: str | None,
    expected_requester: str | None,
) -> list[tuple[UsageRunNode, TokenTotals]]:
    if scope == "admin":
        if _admin_entity_id(row) is None:
            return []
    elif row.source.source_agent_id != expected_agent or expected_agent not in row.source.allowed_agent_ids:
        return []

    entries: list[tuple[UsageRunNode, TokenTotals]] = []
    for run in row.runs:
        if scope == "self" and not row.source.requester_isolated:
            requester_id = config.authorization.resolve_alias(run.requester_id) if run.requester_id else None
            if requester_id is None:
                raise ValueError
            if requester_id != expected_requester:
                continue
        totals = _metrics_totals(run.metrics)
        if totals is not None:
            entries.append((run, totals))
    return entries


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
    if row.source.requester_isolated:
        return _metrics_totals(row.session_metrics)
    total = TokenTotals()
    accepted = False
    for run in row.runs:
        requester_id = config.authorization.resolve_alias(run.requester_id) if run.requester_id else None
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
    if not any(metrics.get(field) is not None for field in TOKEN_FIELDS):
        return None
    values: dict[str, int] = {}
    for field in TOKEN_FIELDS:
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
