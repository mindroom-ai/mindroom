"""Seed real budget monitors with month-to-date spend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.budgets.monitor import BudgetMonitor
from mindroom.budgets.spend import SpendSnapshot, month_bounds

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths


def set_month_spend(monitor: BudgetMonitor, spend_usd: Mapping[str, float]) -> None:
    """Make ``spend_usd`` per canonical requester the monitor's latest scan of the current month."""
    now = monitor.clock()
    period_start, period_end = month_bounds(now)
    monitor._snapshot = SpendSnapshot(
        period_start=period_start,
        period_end=period_end,
        generated_at=now,
        spend_usd=dict(spend_usd),
        unpriced_models=(),
        scanned_sources=0,
        unavailable_sources=0,
    )


def budget_monitor_with_spend(runtime_paths: RuntimePaths, spend_usd: Mapping[str, float]) -> BudgetMonitor:
    """Return an idle monitor whose latest scan reported ``spend_usd`` per canonical requester."""
    monitor = BudgetMonitor(runtime_paths=runtime_paths, config_provider=lambda: None)
    set_month_spend(monitor, spend_usd)
    return monitor
