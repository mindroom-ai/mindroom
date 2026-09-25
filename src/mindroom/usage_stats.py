"""Read-only aggregation of retained Agno token usage."""

from __future__ import annotations

from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Literal

from mindroom.legacy_usage_storage import legacy_request_model
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.usage_stats_storage import (
    TOKEN_FIELDS,
    UsageModelMetrics,
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
    discover_admin_usage_sources,
    discover_private_usage_sources,
    discover_self_usage_sources,
    iter_usage_storage_rows,
)
from mindroom.usage_storage import SYSTEM_USAGE_ENTITY

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.usage_storage import UsageKind

__all__ = [
    "TokenTotals",
    "UsageBreakdownRow",
    "UsageCoverage",
    "UsageCumulativeModelBreakdownRow",
    "UsageDailyBreakdownRow",
    "UsageModelBreakdownRow",
    "UsagePrivateAgentBreakdownRow",
    "UsageReport",
    "UsageRequestBreakdownRow",
    "UsageUserBreakdownRow",
    "UsageVoiceBreakdownRow",
    "collect_admin_usage",
    "collect_private_usage",
    "collect_self_usage",
]

type _Scope = Literal["self", "admin"]
type _ModelUsageEntry = tuple[UsageRunNode, TokenTotals, Mapping[tuple[str, str], TokenTotals]]

_COVERAGE_NOTE = (
    "Shared self totals use requester-attributed retained agent runs. "
    "Private self and admin totals use Agno session aggregates, including team members. "
    "Compaction, memory auto-flush and embedded workflow helpers add tokens without increasing run_count. "
    "Usage snapshots survive compaction and regeneration. Deleted sessions are unavailable."
)
_MODEL_COVERAGE_NOTE = (
    "Model breakdown uses retained top-level runs, team members, and helper usage with usable token metrics. "
    "Only top-level runs increase run_count. "
    "Stored per-model details take precedence over a run's primary model. "
    "Runs with unusable model details are grouped as unknown. "
    "It does not necessarily sum to report totals, which may include history lost before usage migration "
    "and unavailable member detail."
)
_CUMULATIVE_MODEL_COVERAGE_NOTE = (
    "Cumulative model breakdown uses retained session model details plus independently recorded helper usage. "
    "Missing, unusable, or unreconciled attribution is grouped as unknown. "
    "Compacted history still present in retained sessions is included; deleted sessions and dates or requester "
    "attribution absent from session aggregates are unavailable."
)
_USER_COVERAGE_NOTE = (
    "User breakdown uses requester-attributed retained top-level runs, team members, and helper usage, "
    "grouped by canonical user identity. "
    "A null user_id means requester identity is unavailable. "
    "It does not necessarily sum to report totals, which may include history lost before usage migration "
    "and unavailable member detail. Deleted sessions are unavailable."
)
_DAILY_COVERAGE_NOTE = (
    "Daily breakdown uses retained top-level runs, team members, and helper usage with usable token metrics and timestamps, "
    "grouped by UTC request date when request details reconcile. Each top-level run counts once on its first request date. "
    "Missing or unreconciled request details fall back to run creation date and may shift usage across days. "
    "Runs without usable timestamps are excluded. "
    "Runs with unusable model details retain their totals under the unknown model. "
    "It does not necessarily sum to report totals, which may include history lost before usage migration "
    "and unavailable member detail. Deleted sessions are unavailable."
)
_PRIVATE_COVERAGE_NOTE = (
    "Private-agent totals use session aggregates, attributed to a validated private-instance owner "
    "or the session's recorded requester. Retained-run totals, models, and days use recorded run requesters "
    "with the private owner as fallback for ordinary runs. Summary requester attribution remains explicit. "
    "A null user_id means attribution is unavailable. "
    "Usage survives run cleanup; detail lost before migration cannot be reconstructed."
)
_REQUEST_COVERAGE_NOTE = (
    "Request breakdown contains stored provider requests with usable timestamps and token counters. "
    "Request counters must reconcile with both run totals and recorded per-model totals. "
    "Requests without stored model attribution may inherit it from a single known run model. "
    "Missing, unreadable, ambiguous, or mismatched details are excluded without changing aggregate totals. "
    "Stored team-member requests are included without adding top-level runs. "
    "Usage lost before request capture and deleted sessions cannot be reconstructed. "
    "Unavailable sources include retained session usage without matching request detail."
)
_VOICE_COVERAGE_NOTE = (
    "GPT-Live provider-reported session duration, billed separately from delegated agent tokens. "
    "Each row is one provider session; created_at is when that session was first observed. "
    "Duration is not split across UTC days. Unfinalized rows are the last reported running totals. "
    "Sources with missing caller attribution are marked unavailable. "
    "Earlier unrecorded calls, unreceived usage, and deleted sessions are unavailable."
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
    """One retained entity in an admin report."""

    key: str
    totals: TokenTotals
    session_count: int
    cumulative_model_breakdown: tuple[UsageCumulativeModelBreakdownRow, ...]
    retained_run_totals: TokenTotals
    run_count: int
    user_breakdown: tuple[UsageUserBreakdownRow, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the public breakdown row."""
        return {
            "dimension": "entity",
            "key": self.key,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
            "cumulative_model_breakdown": [row.to_dict() for row in self.cumulative_model_breakdown],
            "retained_run_totals": self.retained_run_totals.to_dict(),
            "run_count": self.run_count,
            "user_breakdown": [row.to_dict() for row in self.user_breakdown],
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
    """Retained usage for one provider and model."""

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
class UsageCumulativeModelBreakdownRow:
    """Session-aggregate usage for one provider and model."""

    model_provider: str
    model: str
    totals: TokenTotals
    session_count: int

    def to_dict(self) -> dict[str, object]:
        """Return the public cumulative model breakdown row."""
        return {
            "provider": self.model_provider,
            "model": self.model,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
        }


@dataclass(frozen=True, slots=True)
class UsageUserBreakdownRow:
    """Retained usage for one canonical requester and their models."""

    user_id: str | None
    totals: TokenTotals
    run_count: int
    model_breakdown: tuple[UsageModelBreakdownRow, ...]
    daily_breakdown: tuple[UsageDailyBreakdownRow, ...] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the administrator-only user breakdown."""
        payload: dict[str, object] = {
            "user_id": self.user_id,
            "totals": self.totals.to_dict(),
            "run_count": self.run_count,
            "model_breakdown": [row.to_dict() for row in self.model_breakdown],
        }
        if self.daily_breakdown is not None:
            payload["daily_breakdown"] = [row.to_dict() for row in self.daily_breakdown]
        return payload


@dataclass(frozen=True, slots=True)
class UsageDailyBreakdownRow:
    """Retained usage for one UTC calendar date."""

    date: str
    totals: TokenTotals
    run_count: int
    model_breakdown: tuple[UsageModelBreakdownRow, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the public daily breakdown row."""
        return {
            "date": self.date,
            "totals": self.totals.to_dict(),
            "run_count": self.run_count,
            "model_breakdown": [row.to_dict() for row in self.model_breakdown],
        }


@dataclass(frozen=True, slots=True)
class UsagePrivateAgentBreakdownRow:
    """Session totals and retained detail for one user's private agent."""

    agent_name: str
    user_id: str | None
    totals: TokenTotals
    session_count: int
    cumulative_model_breakdown: tuple[UsageCumulativeModelBreakdownRow, ...]
    retained_run_totals: TokenTotals
    run_count: int
    model_breakdown: tuple[UsageModelBreakdownRow, ...]
    daily_breakdown: tuple[UsageDailyBreakdownRow, ...] | None

    def to_dict(self, *, admin: bool) -> dict[str, object]:
        """Keep owner identities out of personal reports."""
        payload: dict[str, object] = {
            "agent_name": self.agent_name,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
            "cumulative_model_breakdown": [row.to_dict() for row in self.cumulative_model_breakdown],
            "retained_run_totals": self.retained_run_totals.to_dict(),
            "run_count": self.run_count,
            "model_breakdown": [row.to_dict() for row in self.model_breakdown],
        }
        if admin:
            payload["user_id"] = self.user_id
        if self.daily_breakdown is not None:
            payload["daily_breakdown"] = [row.to_dict() for row in self.daily_breakdown]
        return payload


@dataclass(frozen=True, slots=True)
class UsageRequestBreakdownRow:
    """One reconciled provider request, without conversation identifiers or content."""

    entity: str
    user_id: str | None
    model_provider: str
    model: str
    kind: UsageKind
    created_at: int | float
    totals: TokenTotals

    def to_dict(self) -> dict[str, object]:
        """Return the administrator-only request facts for external pricing."""
        return {
            "entity": self.entity,
            "user_id": self.user_id,
            "provider": self.model_provider,
            "model": self.model,
            "kind": self.kind,
            "created_at": self.created_at,
            "totals": self.totals.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UsageVoiceBreakdownRow:
    """One provider voice session, without conversation content or identifiers."""

    entity: str
    user_id: str | None
    model_provider: str
    model: str
    created_at: int | float
    duration_seconds: float
    finalized: bool

    def to_dict(self) -> dict[str, object]:
        """Return voice duration separately from token counters."""
        return {
            "entity": self.entity,
            "user_id": self.user_id,
            "provider": self.model_provider,
            "model": self.model,
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
            "finalized": self.finalized,
        }


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Content-free retained token usage, with optional request detail."""

    scope: _Scope
    totals: TokenTotals
    session_count: int
    breakdown: tuple[UsageBreakdownRow, ...]
    coverage: UsageCoverage
    model_breakdown: tuple[UsageModelBreakdownRow, ...]
    model_coverage: UsageCoverage
    cumulative_model_breakdown: tuple[UsageCumulativeModelBreakdownRow, ...]
    cumulative_model_coverage: UsageCoverage
    user_breakdown: tuple[UsageUserBreakdownRow, ...] = ()
    daily_breakdown: tuple[UsageDailyBreakdownRow, ...] = ()
    daily_coverage: UsageCoverage | None = None
    private_agent_breakdown: tuple[UsagePrivateAgentBreakdownRow, ...] = ()
    private_agent_coverage: UsageCoverage | None = None
    request_breakdown: tuple[UsageRequestBreakdownRow, ...] = ()
    request_coverage: UsageCoverage | None = None
    voice_breakdown: tuple[UsageVoiceBreakdownRow, ...] = ()
    voice_coverage: UsageCoverage | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the stable custom-tool payload fields."""
        payload: dict[str, object] = {
            "scope": self.scope,
            "totals": self.totals.to_dict(),
            "session_count": self.session_count,
            "breakdown": [row.to_dict() for row in self.breakdown],
            "coverage": self.coverage.to_dict(),
            "model_breakdown": [row.to_dict() for row in self.model_breakdown],
            "model_coverage": self.model_coverage.to_dict(),
        }
        if self.scope == "admin":
            payload["voice_breakdown"] = [row.to_dict() for row in self.voice_breakdown]
            if self.voice_coverage is not None:
                payload["voice_coverage"] = self.voice_coverage.to_dict()
            payload["user_breakdown"] = [row.to_dict() for row in self.user_breakdown]
            payload["user_coverage"] = replace(self.model_coverage, note=_USER_COVERAGE_NOTE).to_dict()
            if self.request_coverage is not None:
                payload["request_breakdown"] = [row.to_dict() for row in self.request_breakdown]
                payload["request_coverage"] = self.request_coverage.to_dict()
        if self.scope == "admin" or self.private_agent_coverage is not None:
            payload["cumulative_model_breakdown"] = [row.to_dict() for row in self.cumulative_model_breakdown]
            payload["cumulative_model_coverage"] = self.cumulative_model_coverage.to_dict()
        if self.daily_coverage is not None:
            payload["daily_breakdown"] = [row.to_dict() for row in self.daily_breakdown]
            payload["daily_coverage"] = self.daily_coverage.to_dict()
        if self.private_agent_coverage is not None:
            payload["private_agent_breakdown"] = [
                row.to_dict(admin=self.scope == "admin") for row in self.private_agent_breakdown
            ]
            payload["private_agent_coverage"] = self.private_agent_coverage.to_dict()
        return payload


@dataclass(slots=True)
class _Aggregate:
    totals: TokenTotals = TokenTotals()
    count: int = 0

    def add(self, totals: TokenTotals, *, count: int = 1) -> None:
        self.totals = self.totals.plus(totals)
        self.count += count


@dataclass(slots=True)
class _UsageAccumulator:
    total: TokenTotals = TokenTotals()
    sessions: set[tuple[str, str]] = dataclass_field(default_factory=set)
    buckets: dict[str, _Aggregate] = dataclass_field(default_factory=dict)
    cumulative_model_buckets: dict[tuple[str, str], _Aggregate] = dataclass_field(default_factory=dict)
    entity_cumulative_model_buckets: dict[str, dict[tuple[str, str], _Aggregate]] = dataclass_field(
        default_factory=dict,
    )
    seen_runs: set[tuple[str, str, str]] = dataclass_field(default_factory=set)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)
    cumulative_model_unavailable_sources: set[str] = dataclass_field(default_factory=set)

    def add_row(
        self,
        row: UsageSessionRow,
        *,
        scope: _Scope,
        expected_agent: str | None,
        expected_requester: str | None,
    ) -> None:
        uses_runs = scope == "self" and not row.source.requester_isolated
        if (uses_runs and (not row.runs_available or _has_unattributed_helper(row))) or (
            not uses_runs and not row.session_metrics_available
        ):
            self.unavailable_sources.add(row.source.path_label)
            if not uses_runs:
                self.cumulative_model_unavailable_sources.add(row.source.path_label)
                return
        try:
            if scope == "self":
                row_totals = _self_row_totals(
                    row,
                    expected_agent=expected_agent,
                    expected_requester=expected_requester,
                    seen_runs=self.seen_runs,
                )
                entity_id = None
            else:
                entity_id = _admin_entity_id(row)
                row_totals = _session_totals(row) if entity_id is not None else None
        except ValueError:
            self.unavailable_sources.add(row.source.path_label)
            if not uses_runs:
                self.cumulative_model_unavailable_sources.add(row.source.path_label)
            return
        if row_totals is None:
            return
        self.total = self.total.plus(row_totals)
        is_system = row.source.scope == "system"
        if not is_system:
            self.sessions.add((row.source.path_label, row.row_key))
        models: Mapping[tuple[str, str], TokenTotals] = {}
        if not uses_runs:
            models = self._add_cumulative_models(row, row_totals)
        if entity_id is not None:
            self.buckets.setdefault(entity_id, _Aggregate()).add(row_totals, count=0 if is_system else 1)
            _add_model_totals(
                self.entity_cumulative_model_buckets.setdefault(entity_id, {}),
                models,
                count=0 if is_system else 1,
            )

    def _add_cumulative_models(
        self,
        row: UsageSessionRow,
        row_totals: TokenTotals,
    ) -> Mapping[tuple[str, str], TokenTotals]:
        models = _session_model_totals(row, row_totals)
        if models is None:
            models = {("unknown", "unknown"): row_totals}
            self.cumulative_model_unavailable_sources.add(row.source.path_label)
        elif any("unknown" in key for key in models):
            self.cumulative_model_unavailable_sources.add(row.source.path_label)
        _add_model_totals(self.cumulative_model_buckets, models, count=0 if row.source.scope == "system" else 1)
        return models


@dataclass(slots=True)
class _DailyUsageAccumulator:
    buckets: dict[str, _Aggregate] = dataclass_field(default_factory=dict)
    model_buckets: dict[str, dict[tuple[str, str], _Aggregate]] = dataclass_field(default_factory=dict)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)

    def add_run(
        self,
        run: UsageRunNode,
        totals: TokenTotals,
        models: Mapping[tuple[str, str], TokenTotals],
        *,
        source_path: str,
    ) -> None:
        requests = _reconciled_request_totals(run, totals, models)
        if requests is not None:
            counted_models: set[tuple[str, str]] = set()
            for index, (created_at, model_key, request_totals) in enumerate(
                sorted(requests, key=lambda request: request[0]),
            ):
                date = datetime.fromtimestamp(created_at, tz=UTC).date().isoformat()
                self._add(
                    date,
                    request_totals,
                    {model_key: request_totals},
                    count=run.run_count if index == 0 else 0,
                    model_count=run.run_count if model_key not in counted_models else 0,
                )
                counted_models.add(model_key)
            return
        if run.created_at is None:
            self.unavailable_sources.add(source_path)
            return
        try:
            date = datetime.fromtimestamp(run.created_at, tz=UTC).date().isoformat()
        except (OverflowError, OSError, ValueError):
            self.unavailable_sources.add(source_path)
            return
        self._add(date, totals, models, count=run.run_count, model_count=run.run_count)

    def _add(
        self,
        date: str,
        totals: TokenTotals,
        models: Mapping[tuple[str, str], TokenTotals],
        *,
        count: int,
        model_count: int,
    ) -> None:
        self.buckets.setdefault(date, _Aggregate()).add(totals, count=count)
        _add_model_totals(self.model_buckets.setdefault(date, {}), models, count=model_count)

    def rows(self) -> tuple[UsageDailyBreakdownRow, ...]:
        """Build the same sorted daily rows for overall and per-user usage."""
        return tuple(
            UsageDailyBreakdownRow(
                date=date,
                totals=aggregate.totals,
                run_count=aggregate.count,
                model_breakdown=_model_breakdown(self.model_buckets[date]),
            )
            for date, aggregate in sorted(self.buckets.items())
        )


@dataclass(slots=True)
class _RequestUsageAccumulator:
    enabled: bool
    requests: list[UsageRequestBreakdownRow] = dataclass_field(default_factory=list)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)
    seen_sessions: set[tuple[str, str]] = dataclass_field(default_factory=set)

    def add_row(self, row: UsageSessionRow, entries: list[_ModelUsageEntry]) -> None:
        """Reuse admitted run/model entries and report historical gaps separately."""
        if not self.enabled:
            return
        entity = _admin_entity_id(row)
        session_key = (row.source.path_label, row.row_key)
        if entity is None or session_key in self.seen_sessions:
            return
        self.seen_sessions.add(session_key)
        retained = TokenTotals()
        for run, totals, models in entries:
            if run.kind == "run":
                retained = retained.plus(totals)
            requests = _reconciled_request_totals(run, totals, models)
            if requests is None:
                self.unavailable_sources.add(row.source.path_label)
            else:
                self.requests.extend(
                    UsageRequestBreakdownRow(
                        entity=entity,
                        user_id=run.requester_id,
                        model_provider=provider,
                        model=model,
                        kind=run.kind,
                        created_at=created_at,
                        totals=request_totals,
                    )
                    for created_at, (provider, model), request_totals in requests
                )
        try:
            session_totals = _metrics_totals(row.session_metrics) or TokenTotals()
        except ValueError:
            self.unavailable_sources.add(row.source.path_label)
            return
        if row.source.scope != "system" and (not row.session_metrics_available or retained != session_totals):
            self.unavailable_sources.add(row.source.path_label)

    def rows(self) -> tuple[UsageRequestBreakdownRow, ...]:
        """Return stable chronological request facts without exposing storage ordering."""
        return tuple(
            sorted(
                self.requests,
                key=lambda row: (
                    row.created_at,
                    row.entity,
                    row.user_id or "",
                    row.model_provider,
                    row.model,
                    row.kind,
                ),
            ),
        )


def _reconciled_request_totals(
    run: UsageRunNode,
    totals: TokenTotals,
    models: Mapping[tuple[str, str], TokenTotals],
) -> list[tuple[int | float, tuple[str, str], TokenTotals]] | None:
    """Require complete request counters to match the run and each known model."""
    if not run.requests or not models or any("unknown" in key for key in models):
        return None
    combined = TokenTotals()
    by_model: dict[tuple[str, str], TokenTotals] = {}
    requests: list[tuple[int | float, tuple[str, str], TokenTotals]] = []
    try:
        for request in run.requests:
            if request.model_provider is None and request.model is None:
                model_key = legacy_request_model(models)
            elif request.model_provider is not None and request.model is not None:
                model_key = (request.model_provider, request.model)
            else:
                return None
            if model_key is None:
                return None
            request_totals = _metrics_totals(request.metrics)
            if request_totals is None:
                return None
            datetime.fromtimestamp(request.created_at, tz=UTC)
            combined = combined.plus(request_totals)
            by_model[model_key] = by_model.get(model_key, TokenTotals()).plus(request_totals)
            requests.append((request.created_at, model_key, request_totals))
    except (OverflowError, OSError, ValueError):
        return None
    return requests if combined == totals and by_model == models else None


@dataclass(slots=True)
class _ModelUsageAccumulator:
    total: _Aggregate = dataclass_field(default_factory=_Aggregate)
    buckets: dict[tuple[str, str], _Aggregate] = dataclass_field(default_factory=dict)
    user_buckets: dict[str | None, dict[tuple[str, str], _Aggregate]] = dataclass_field(default_factory=dict)
    user_totals: dict[str | None, _Aggregate] = dataclass_field(default_factory=dict)
    seen_runs: set[tuple[str, str, str]] = dataclass_field(default_factory=set)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)
    daily_usage: _DailyUsageAccumulator | None = None
    user_daily_usage: dict[str | None, _DailyUsageAccumulator] = dataclass_field(default_factory=dict)

    def add_row(
        self,
        row: UsageSessionRow,
        *,
        scope: _Scope,
        expected_agent: str | None,
        expected_requester: str | None,
    ) -> list[_ModelUsageEntry]:
        if not row.runs_available or (
            scope == "self" and not row.source.requester_isolated and _has_unattributed_helper(row)
        ):
            self.unavailable_sources.add(row.source.path_label)
        try:
            entries = _model_entries_for_row(
                row,
                scope=scope,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
                unavailable_sources=self.unavailable_sources,
            )
        except ValueError:
            self.unavailable_sources.add(row.source.path_label)
            return []
        accepted: list[_ModelUsageEntry] = []
        for run, totals in entries:
            if run.run_id is not None:
                identity = (row.source.path_label, row.row_key, run.run_id)
                if identity in self.seen_runs:
                    continue
                self.seen_runs.add(identity)
            models = _run_model_totals(run, totals)
            if models is None:
                self.unavailable_sources.add(row.source.path_label)
                models = {("unknown", "unknown"): totals}
            self.add_run(run, totals, models, scope=scope, source_path=row.source.path_label)
            accepted.append((run, totals, models))
        return accepted

    def add_run(
        self,
        run: UsageRunNode,
        totals: TokenTotals,
        models: Mapping[tuple[str, str], TokenTotals],
        *,
        scope: _Scope,
        source_path: str,
    ) -> None:
        """Accumulate an already validated, deduplicated run for any report view."""
        self.total.add(totals, count=run.run_count)
        _add_model_totals(self.buckets, models, count=run.run_count)
        if scope == "admin":
            self.user_totals.setdefault(run.requester_id, _Aggregate()).add(totals, count=run.run_count)
            _add_model_totals(self.user_buckets.setdefault(run.requester_id, {}), models, count=run.run_count)
        if self.daily_usage is not None:
            self.daily_usage.add_run(run, totals, models, source_path=source_path)
            if scope == "admin":
                self.user_daily_usage.setdefault(run.requester_id, _DailyUsageAccumulator()).add_run(
                    run,
                    totals,
                    models,
                    source_path=source_path,
                )


@dataclass(slots=True)
class _PrivateUsageAccumulator:
    include_daily: bool
    buckets: dict[tuple[str | None, str], tuple[_UsageAccumulator, _ModelUsageAccumulator]] = dataclass_field(
        default_factory=dict,
    )
    sources: set[str] = dataclass_field(default_factory=set)
    unavailable_sources: set[str] = dataclass_field(default_factory=set)

    def mark_unavailable(self, source: UsageStorageSource | UsageStorageDiagnostic) -> None:
        """Retain private discovery and read failures in private coverage."""
        if source.scope == "private_agent":
            self.unavailable_sources.add(source.path_label)

    def bucket(self, user_id: str | None, agent_name: str) -> tuple[_UsageAccumulator, _ModelUsageAccumulator]:
        """Reuse the report accumulators without rescanning a database."""
        key = (user_id, agent_name)
        if key not in self.buckets:
            self.buckets[key] = (
                _UsageAccumulator(),
                _ModelUsageAccumulator(daily_usage=_DailyUsageAccumulator() if self.include_daily else None),
            )
        return self.buckets[key]

    def add_row(
        self,
        row: UsageSessionRow,
        entries: list[_ModelUsageEntry],
        *,
        scope: _Scope,
        requester_id: str | None,
    ) -> None:
        """Preserve session ownership separately from retained run attribution."""
        if row.source.scope != "private_agent" or (agent_name := _admin_entity_id(row)) is None:
            return
        self.sources.add(row.source.path_label)
        owner = requester_id if scope == "self" else row.source.owner_id or row.requester_id
        if owner is None:
            self.unavailable_sources.add(row.source.path_label)
        usage, _ = self.bucket(owner, agent_name)
        usage.add_row(row, scope="admin", expected_agent=None, expected_requester=None)
        for run, totals, model_totals in entries:
            user_id = owner if scope == "self" else _run_requester(run, owner)
            _, models = self.bucket(user_id, agent_name)
            models.add_run(
                run,
                totals,
                model_totals,
                scope="admin",
                source_path=row.source.path_label,
            )

    def rows(self) -> tuple[UsagePrivateAgentBreakdownRow, ...]:
        """Build deterministic per-user, per-agent rows from shared accumulators."""
        rows = []
        for (user_id, agent_name), (usage, models) in sorted(
            self.buckets.items(),
            key=lambda item: (item[0][0] or "", item[0][1]),
        ):
            rows.append(
                UsagePrivateAgentBreakdownRow(
                    agent_name,
                    user_id,
                    usage.total,
                    len(usage.sessions),
                    _cumulative_model_breakdown(usage.cumulative_model_buckets),
                    models.total.totals,
                    models.total.count,
                    _model_breakdown(models.buckets),
                    models.daily_usage.rows() if models.daily_usage is not None else None,
                ),
            )
        return tuple(rows)

    def coverage(self) -> UsageCoverage:
        """Report unavailable attribution or metrics without guessing missing history."""
        unavailable = set(self.unavailable_sources)
        for usage, models in self.buckets.values():
            unavailable.update(usage.unavailable_sources | models.unavailable_sources)
            if models.daily_usage is not None:
                unavailable.update(models.daily_usage.unavailable_sources)
        return UsageCoverage(len(self.sources), len(unavailable), _PRIVATE_COVERAGE_NOTE)


def collect_private_usage(
    *,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    include_daily: bool = False,
) -> UsageReport:
    """Collect only the requester's own configured private agents across known aliases."""
    requester_id = resolve_human_requester_alias(requester_id, config, runtime_paths)
    return _collect_usage(
        sources=discover_private_usage_sources(requester_id=requester_id, config=config, runtime_paths=runtime_paths),
        config=config,
        runtime_paths=runtime_paths,
        scope="self",
        expected_agent=None,
        expected_requester=requester_id,
        include_daily=include_daily,
    )


def collect_self_usage(
    *,
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    include_daily: bool = False,
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
        runtime_paths=runtime_paths,
        scope="self",
        expected_agent=agent_name,
        expected_requester=resolve_human_requester_alias(requester_id, config, runtime_paths),
        include_daily=include_daily,
    )


def collect_admin_usage(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    include_daily: bool = False,
    include_requests: bool = False,
) -> UsageReport:
    """Collect retained session aggregates across configured agents and stored teams."""
    return _collect_usage(
        sources=discover_admin_usage_sources(config=config, runtime_paths=runtime_paths),
        config=config,
        runtime_paths=runtime_paths,
        scope="admin",
        expected_agent=None,
        expected_requester=None,
        include_daily=include_daily,
        include_requests=include_requests,
    )


def _collect_usage(
    *,
    sources: Iterable[UsageStorageSource | UsageStorageDiagnostic],
    config: Config,
    runtime_paths: RuntimePaths,
    scope: _Scope,
    expected_agent: str | None,
    expected_requester: str | None,
    include_daily: bool,
    include_requests: bool = False,
) -> UsageReport:
    @cache
    def canonical_requester(requester_id: str) -> str:
        return resolve_human_requester_alias(requester_id, config, runtime_paths)

    usage = _UsageAccumulator()
    daily_usage = _DailyUsageAccumulator() if include_daily else None
    request_usage = _RequestUsageAccumulator(enabled=include_requests and scope == "admin")
    model_usage = _ModelUsageAccumulator(daily_usage=daily_usage)
    entity_models: dict[str, _ModelUsageAccumulator] = {}
    private_usage = _PrivateUsageAccumulator(include_daily)
    scanned_sources: set[str] = set()
    voice_rows: list[UsageVoiceBreakdownRow] = []
    voice_unavailable: set[str] = set()
    for discovered in sources:
        if isinstance(discovered, UsageStorageDiagnostic):
            voice_unavailable.add(discovered.path_label)
            usage.unavailable_sources.add(discovered.path_label)
            usage.cumulative_model_unavailable_sources.add(discovered.path_label)
            model_usage.unavailable_sources.add(discovered.path_label)
            private_usage.mark_unavailable(discovered)
            continue
        source = discovered
        scanned_sources.add(source.path_label)
        if source.scope == "private_agent":
            private_usage.sources.add(source.path_label)
        mode = "runs" if scope == "self" and not source.requester_isolated else "both"
        for item in iter_usage_storage_rows(source, mode=mode):
            if isinstance(item, UsageStorageDiagnostic):
                voice_unavailable.add(item.path_label)
                usage.unavailable_sources.add(item.path_label)
                usage.cumulative_model_unavailable_sources.add(item.path_label)
                model_usage.unavailable_sources.add(item.path_label)
                private_usage.mark_unavailable(source)
                continue
            owner = canonical_requester(item.source.owner_id) if item.source.owner_id is not None else None
            row = replace(
                item,
                source=replace(item.source, owner_id=owner),
                requester_id=canonical_requester(item.requester_id) if item.requester_id is not None else None,
                runs=tuple(
                    replace(
                        run,
                        requester_id=canonical_requester(run.requester_id)
                        if run.requester_id
                        else _run_requester(run, owner),
                    )
                    for run in item.runs
                ),
            )
            usage.add_row(
                row,
                scope=scope,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
            )
            entries = model_usage.add_row(
                row,
                scope=scope,
                expected_agent=expected_agent,
                expected_requester=expected_requester,
            )
            if scope == "admin":
                _add_entity_runs(entity_models, row, entries, include_daily=include_daily)
                voice_rows.extend(_voice_rows(row, voice_unavailable))
            request_usage.add_row(row, entries)
            if scope == "admin" or _self_source_allowed(source, expected_agent):
                private_usage.add_row(row, entries, scope=scope, requester_id=expected_requester)
            if source.scope == "private_agent" and source.path_label in model_usage.unavailable_sources:
                private_usage.unavailable_sources.add(source.path_label)

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
        breakdown=_entity_breakdown(usage, entity_models),
        coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(usage.unavailable_sources),
        ),
        model_breakdown=model_breakdown,
        model_coverage=model_coverage,
        cumulative_model_breakdown=_cumulative_model_breakdown(usage.cumulative_model_buckets),
        cumulative_model_coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(usage.cumulative_model_unavailable_sources),
            note=_CUMULATIVE_MODEL_COVERAGE_NOTE,
        ),
        user_breakdown=_user_breakdown(model_usage),
        daily_breakdown=daily_usage.rows() if daily_usage is not None else (),
        daily_coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(model_usage.unavailable_sources | daily_usage.unavailable_sources),
            note=_DAILY_COVERAGE_NOTE,
        )
        if daily_usage is not None
        else None,
        # Current-agent self reports keep their existing compact payload.
        private_agent_breakdown=private_usage.rows() if expected_agent is None else (),
        private_agent_coverage=private_usage.coverage() if expected_agent is None else None,
        request_breakdown=request_usage.rows(),
        voice_breakdown=tuple(sorted(voice_rows, key=lambda row: (row.created_at, row.entity, row.user_id or ""))),
        voice_coverage=UsageCoverage(len(scanned_sources), len(voice_unavailable), _VOICE_COVERAGE_NOTE),
        request_coverage=UsageCoverage(
            scanned_sources=len(scanned_sources),
            unavailable_sources=len(
                usage.unavailable_sources | model_usage.unavailable_sources | request_usage.unavailable_sources,
            ),
            note=_REQUEST_COVERAGE_NOTE,
        )
        if request_usage.enabled
        else None,
    )


def _voice_rows(row: UsageSessionRow, unavailable: set[str]) -> list[UsageVoiceBreakdownRow]:
    if not row.runs_available:
        unavailable.add(row.source.path_label)
    entity = _admin_entity_id(row)
    if entity is None:
        return []
    result = []
    for run in row.runs:
        if run.kind != "live_voice":
            continue
        if run.voice_seconds is None or run.created_at is None or run.model_provider is None or run.model is None:
            unavailable.add(row.source.path_label)
            continue
        if run.requester_id is None:
            unavailable.add(row.source.path_label)
        result.append(
            UsageVoiceBreakdownRow(
                entity,
                run.requester_id,
                run.model_provider,
                run.model,
                run.created_at,
                run.voice_seconds,
                run.voice_finalized,
            ),
        )
    return result


def _add_entity_runs(
    entities: dict[str, _ModelUsageAccumulator],
    row: UsageSessionRow,
    entries: list[_ModelUsageEntry],
    *,
    include_daily: bool,
) -> None:
    if not entries or (entity_id := _admin_entity_id(row)) is None:
        return
    if entity_id not in entities:
        entities[entity_id] = _ModelUsageAccumulator(
            daily_usage=_DailyUsageAccumulator() if include_daily else None,
        )
    for run, totals, models in entries:
        entities[entity_id].add_run(run, totals, models, scope="admin", source_path=row.source.path_label)


def _entity_breakdown(
    usage: _UsageAccumulator,
    entity_models: Mapping[str, _ModelUsageAccumulator],
) -> tuple[UsageBreakdownRow, ...]:
    rows = []
    for entity_id in usage.buckets.keys() | entity_models.keys():
        aggregate = usage.buckets.get(entity_id, _Aggregate())
        models = entity_models.get(entity_id, _ModelUsageAccumulator())
        rows.append(
            UsageBreakdownRow(
                key=entity_id,
                totals=aggregate.totals,
                session_count=aggregate.count,
                cumulative_model_breakdown=_cumulative_model_breakdown(
                    usage.entity_cumulative_model_buckets.get(entity_id, {}),
                ),
                retained_run_totals=models.total.totals,
                run_count=models.total.count,
                user_breakdown=_user_breakdown(models),
            ),
        )
    return tuple(sorted(rows, key=lambda row: (-row.totals.total_tokens, row.key)))


def _user_breakdown(usage: _ModelUsageAccumulator) -> tuple[UsageUserBreakdownRow, ...]:
    rows: list[UsageUserBreakdownRow] = []
    for user_id, models in usage.user_buckets.items():
        aggregate = usage.user_totals[user_id]
        rows.append(
            UsageUserBreakdownRow(
                user_id,
                aggregate.totals,
                aggregate.count,
                _model_breakdown(models),
                usage.user_daily_usage[user_id].rows() if usage.daily_usage is not None else None,
            ),
        )
    return tuple(sorted(rows, key=lambda row: (-row.totals.total_tokens, row.user_id or "")))


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


def _cumulative_model_breakdown(
    buckets: Mapping[tuple[str, str], _Aggregate],
) -> tuple[UsageCumulativeModelBreakdownRow, ...]:
    return tuple(
        UsageCumulativeModelBreakdownRow(
            model_provider=key[0],
            model=key[1],
            totals=aggregate.totals,
            session_count=aggregate.count,
        )
        for key, aggregate in sorted(
            buckets.items(),
            key=lambda item: (-item[1].totals.total_tokens, item[0]),
        )
    )


def _add_model_totals(
    buckets: dict[tuple[str, str], _Aggregate],
    models: Mapping[tuple[str, str], TokenTotals],
    *,
    count: int = 1,
) -> None:
    for key, totals in models.items():
        buckets.setdefault(key, _Aggregate()).add(totals, count=count)


def _run_model_totals(run: UsageRunNode, totals: TokenTotals) -> dict[tuple[str, str], TokenTotals] | None:
    """Use detailed attribution only when it accounts for the run's token counters."""
    if run.model_metrics is None:
        return None
    if not run.model_metrics:
        return {(run.model_provider or "unknown", run.model or "unknown"): totals}
    return _detailed_model_totals(run.model_metrics, totals)


def _session_totals(row: UsageSessionRow) -> TokenTotals | None:
    """Independent helpers are not part of Agno's session counters; add them exactly once."""
    totals = _metrics_totals(row.session_metrics)
    for run in row.runs:
        if run.kind != "run" and (helper_totals := _metrics_totals(run.metrics)) is not None:
            totals = (totals or TokenTotals()).plus(helper_totals)
    return totals


def _session_model_totals(
    row: UsageSessionRow,
    totals: TokenTotals,
) -> dict[tuple[str, str], TokenTotals] | None:
    """Reconcile session model detail, then add independently recorded helper usage."""
    base_totals = _metrics_totals(row.session_metrics)
    models = (
        _detailed_model_totals(row.session_model_metrics, base_totals, require_usable_entries=True)
        if row.session_model_metrics and base_totals is not None
        else None
    )
    if models is None:
        models = {("unknown", "unknown"): base_totals} if base_totals is not None else {}
    for run in row.runs:
        if run.kind == "run" or (helper_totals := _metrics_totals(run.metrics)) is None:
            continue
        helper_models = _run_model_totals(run, helper_totals) or {("unknown", "unknown"): helper_totals}
        for key, value in helper_models.items():
            models[key] = models.get(key, TokenTotals()).plus(value)
    combined = TokenTotals()
    for value in models.values():
        combined = combined.plus(value)
    return models if models and combined == totals else None


def _detailed_model_totals(
    model_metrics: tuple[UsageModelMetrics, ...],
    totals: TokenTotals,
    *,
    require_usable_entries: bool = False,
) -> dict[tuple[str, str], TokenTotals] | None:
    models: dict[tuple[str, str], TokenTotals] = {}
    combined = TokenTotals()
    try:
        for entry in model_metrics:
            model_totals = _metrics_totals(entry.metrics)
            if model_totals is None and require_usable_entries:
                return None
            if model_totals is None:
                continue
            if require_usable_entries and (entry.model_provider is None or entry.model is None):
                return None
            key = (entry.model_provider or "unknown", entry.model or "unknown")
            models[key] = models.get(key, TokenTotals()).plus(model_totals)
            combined = combined.plus(model_totals)
    except ValueError:
        return None
    return models if models and combined == totals else None


def _run_requester(run: UsageRunNode, owner: str | None) -> str | None:
    """Keep helper attribution explicit while retaining ordinary private-run fallback."""
    return run.requester_id or (owner if run.kind == "run" else None)


def _has_unattributed_helper(row: UsageSessionRow) -> bool:
    return any(run.kind != "run" and run.requester_id is None for run in row.runs)


def _model_entries_for_row(
    row: UsageSessionRow,
    *,
    scope: _Scope,
    expected_agent: str | None,
    expected_requester: str | None,
    unavailable_sources: set[str],
) -> list[tuple[UsageRunNode, TokenTotals]]:
    if scope == "admin" and _admin_entity_id(row) is None:
        return []
    if scope == "self" and not _self_source_allowed(row.source, expected_agent):
        return []

    entries: list[tuple[UsageRunNode, TokenTotals]] = []
    for run in row.runs:
        if scope == "self" and not row.source.requester_isolated:
            if run.requester_id is None and run.kind == "run":
                raise ValueError
            if run.requester_id != expected_requester:
                continue
        try:
            totals = _metrics_totals(run.metrics)
        except ValueError:
            if scope == "self" and not row.source.requester_isolated:
                raise
            unavailable_sources.add(row.source.path_label)
            continue
        if totals is not None:
            entries.append((run, totals))
    return entries


def _self_row_totals(
    row: UsageSessionRow,
    *,
    expected_agent: str | None,
    expected_requester: str | None,
    seen_runs: set[tuple[str, str, str]],
) -> TokenTotals | None:
    if not _self_source_allowed(row.source, expected_agent):
        return None
    if row.source.requester_isolated:
        return _session_totals(row)
    total = TokenTotals()
    accepted = False
    for run in row.runs:
        if run.requester_id is None and run.kind == "run":
            raise ValueError
        if run.requester_id != expected_requester:
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


def _self_source_allowed(source: UsageStorageSource, expected_agent: str | None) -> bool:
    if source.source_agent_id not in source.allowed_agent_ids:
        return False
    if expected_agent is None:
        return source.scope == "private_agent" and source.requester_isolated
    return source.source_agent_id == expected_agent


def _admin_entity_id(row: UsageSessionRow) -> str | None:
    if row.source.scope == "system":
        return SYSTEM_USAGE_ENTITY if row.entity_id == SYSTEM_USAGE_ENTITY else None
    if row.source.scope in {"shared_agent", "private_agent"}:
        entity_id = row.source.source_agent_id
        return entity_id if entity_id in row.source.allowed_agent_ids else None
    return row.entity_id if row.entity_kind == "team" else None


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
