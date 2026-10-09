"""Month-to-date spend per requester, derived from retained usage."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from mindroom.budgets.pricing import PricedModel, cost_usd
from mindroom.usage_stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True, slots=True)
class _UnpricedModelUsage:
    """Month-to-date tokens of a model without configured prices."""

    provider: str
    model: str
    total_tokens: int


@dataclass(frozen=True, slots=True)
class SpendSnapshot:
    """Month-to-date USD spend per canonical requester at one scan."""

    period_start: date
    period_end: date
    generated_at: datetime
    spend_usd: Mapping[str, float]
    unpriced_models: tuple[_UnpricedModelUsage, ...]
    scanned_sources: int
    unavailable_sources: int


def month_bounds(now: datetime) -> tuple[date, date]:
    """Return the first day of the UTC month containing ``now`` and of the month after it."""
    today = now.astimezone(UTC).date()
    start = today.replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


def collect_monthly_spend(
    config: Config,
    runtime_paths: RuntimePaths,
    now: datetime,
    prices: Mapping[tuple[str, str], PricedModel],
) -> SpendSnapshot:
    """Price each requester's dated usage in the current UTC month."""
    start, end = month_bounds(now)
    # Runs that began the day before can still make requests dated in this month.
    since = datetime.combine(start - timedelta(days=1), time(), UTC).timestamp()
    report = collect_admin_usage(config=config, runtime_paths=runtime_paths, include_daily=True, since=since)
    spend: defaultdict[str, float] = defaultdict(float)
    unpriced: defaultdict[tuple[str, str], int] = defaultdict(int)
    for user in report.user_breakdown:
        for day in user.daily_breakdown or ():
            if not start <= date.fromisoformat(day.date) < end:
                continue
            for row in day.model_breakdown:
                priced = prices.get((row.model_provider, row.model))
                if priced is None:
                    unpriced[(row.model_provider, row.model)] += row.totals.total_tokens
                elif user.user_id is not None:
                    spend[user.user_id] += cost_usd(row.totals, priced)
    coverage = report.daily_coverage
    return SpendSnapshot(
        period_start=start,
        period_end=end,
        generated_at=now,
        spend_usd=dict(spend),
        unpriced_models=tuple(
            _UnpricedModelUsage(provider=provider, model=model, total_tokens=tokens)
            for (provider, model), tokens in sorted(unpriced.items())
        ),
        scanned_sources=coverage.scanned_sources if coverage is not None else 0,
        unavailable_sources=coverage.unavailable_sources if coverage is not None else 0,
    )
