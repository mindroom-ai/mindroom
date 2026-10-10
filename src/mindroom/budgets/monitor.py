"""Cached month-to-date spend per requester and the fallback model decision it drives."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from mindroom.budgets.pricing import price_table, provider_identity
from mindroom.budgets.spend import SpendSnapshot, collect_monthly_spend, month_bounds
from mindroom.logging_config import get_logger
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_MIN_SCAN_INTERVAL_SECONDS = 30.0
# Finished replies rescan sooner; the tick also catches voice calls, helpers, and a new month.
_TICK_SECONDS = 300.0


def _budget_limit_usd(config: Config, canonical_user_id: str) -> float | None:
    """Return a canonical requester's monthly cap in USD, or None when budgets are off or the user is uncapped."""
    budgets = config.budgets
    if budgets is None:
        return None
    for configured_user_id, limit in budgets.users.items():
        if config.authorization.resolve_alias(configured_user_id) == canonical_user_id:
            return limit
    return budgets.monthly_limit_usd


def _is_priced(config: Config, model_name: str) -> bool:
    """Return whether usage of this model adds spend, including through another alias of the same model."""
    model_config = config.models.get(model_name)
    if model_config is None:
        return False
    identity = (provider_identity(model_config.provider), model_config.id)
    return any(
        other.pricing is not None and (provider_identity(other.provider), other.id) == identity
        for other in config.models.values()
    )


def budget_model(
    config: Config,
    runtime_paths: RuntimePaths,
    monitor: BudgetMonitor | None,
    requester_id: str | None,
    model_name: str,
) -> str:
    """Return the model a reply for this requester should use under their budget.

    ``config`` is the snapshot ``model_name`` was resolved from, so the fallback names a model it defines.
    Without a monitor no spend is known yet, as before the first scan.
    """
    budgets = config.budgets
    if budgets is None or requester_id is None:
        return model_name
    # Unpriced models add no tracked spend, so swapping them could only raise cost.
    if model_name == budgets.fallback_model or not _is_priced(config, model_name):
        return model_name
    if not is_human_requester_id(requester_id, config, runtime_paths):
        return model_name
    canonical_requester_id = resolve_human_requester_alias(requester_id, config, runtime_paths)
    limit = _budget_limit_usd(config, canonical_requester_id)
    if limit is None:
        return model_name
    spend = monitor._spend_usd(canonical_requester_id) if monitor is not None else 0.0
    if spend < limit:
        return model_name
    logger.info(
        "budget_fallback_applied",
        requester_id=canonical_requester_id,
        model=model_name,
        fallback_model=budgets.fallback_model,
        spend_usd=round(spend, 4),
        limit_usd=limit,
    )
    return budgets.fallback_model


@dataclass(frozen=True, slots=True)
class _BudgetUserStatus:
    """One requester's month-to-date spend against their cap."""

    user_id: str
    spend_usd: float
    limit_usd: float | None
    over_budget: bool

    def to_dict(self) -> dict[str, object]:
        """Return the dashboard row."""
        return {
            "user_id": self.user_id,
            "spend_usd": self.spend_usd,
            "limit_usd": self.limit_usd,
            "over_budget": self.over_budget,
        }


@dataclass(frozen=True, slots=True)
class _BudgetStatus:
    """Budget settings and month-to-date spend for the dashboard."""

    enabled: bool
    period_start: date | None = None
    period_end: date | None = None
    snapshot: SpendSnapshot | None = None
    default_limit_usd: float | None = None
    fallback_model: str | None = None
    users: tuple[_BudgetUserStatus, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return the dashboard payload."""
        if not self.enabled or self.period_start is None or self.period_end is None:
            return {"enabled": False}
        snapshot = self.snapshot
        return {
            "enabled": True,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "generated_at": snapshot.generated_at.isoformat() if snapshot is not None else None,
            "default_limit_usd": self.default_limit_usd,
            "fallback_model": self.fallback_model,
            "users": [user.to_dict() for user in self.users],
            "unpriced_models": [
                {"provider": row.provider, "model": row.model, "total_tokens": row.total_tokens}
                for row in (snapshot.unpriced_models if snapshot is not None else ())
            ],
            "coverage": {
                "scanned_sources": snapshot.scanned_sources if snapshot is not None else 0,
                "unavailable_sources": snapshot.unavailable_sources if snapshot is not None else 0,
            },
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BudgetMonitor:
    """Keep month-to-date spend current off the event loop and decide when a reply uses the fallback model."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    clock: Callable[[], datetime] = _utc_now
    min_scan_interval_seconds: float = _MIN_SCAN_INTERVAL_SECONDS
    tick_seconds: float = _TICK_SECONDS
    _snapshot: SpendSnapshot | None = field(default=None, init=False)
    # Priced models are instantiated to learn their recorded identity, so the table is kept per config object.
    _prices: tuple[Config, Mapping[tuple[str, str], PricedModel]] | None = field(default=None, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    def sync(self) -> None:
        """Start the refresh loop once and rescan for the active config."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="budget_monitor")
        self._request_scan()

    def response_finished(self) -> None:
        """Rescan soon, because a finished response may have added spend."""
        self._request_scan()

    async def stop(self) -> None:
        """Stop the refresh loop; the last snapshot stays readable."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _spend_usd(self, user_id: str) -> float:
        """Return a canonical requester's month-to-date spend from the latest scan of this month."""
        snapshot = self._current_snapshot()
        return 0.0 if snapshot is None else snapshot.spend_usd.get(user_id, 0.0)

    def status(self) -> _BudgetStatus:
        """Return budget settings with each spender's and configured user's month-to-date spend."""
        config = self.config_provider()
        if config is None or config.budgets is None:
            return _BudgetStatus(enabled=False)
        period_start, period_end = month_bounds(self.clock())
        snapshot = self._current_snapshot()
        spend = dict(snapshot.spend_usd) if snapshot is not None else {}
        user_ids = set(spend) | {config.authorization.resolve_alias(user_id) for user_id in config.budgets.users}
        users = []
        for user_id in user_ids:
            # Replies for agents and bridge bots are never swapped, so they have no cap to be over.
            limit = (
                _budget_limit_usd(config, user_id)
                if is_human_requester_id(user_id, config, self.runtime_paths)
                else None
            )
            user_spend = spend.get(user_id, 0.0)
            users.append(
                _BudgetUserStatus(
                    user_id=user_id,
                    spend_usd=user_spend,
                    limit_usd=limit,
                    over_budget=limit is not None and user_spend >= limit,
                ),
            )
        return _BudgetStatus(
            enabled=True,
            period_start=period_start,
            period_end=period_end,
            snapshot=snapshot,
            default_limit_usd=config.budgets.monthly_limit_usd,
            fallback_model=config.budgets.fallback_model,
            users=tuple(sorted(users, key=lambda user: (-user.spend_usd, user.user_id))),
        )

    def _current_snapshot(self) -> SpendSnapshot | None:
        """Return the latest snapshot unless it belongs to an earlier month."""
        snapshot = self._snapshot
        if snapshot is None or snapshot.period_start != month_bounds(self.clock())[0]:
            return None
        return snapshot

    def _request_scan(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        last_scan_started: float | None = None
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_seconds)
            config = self.config_provider()
            if config is None or config.budgets is None:
                self._wake.clear()
                continue
            if last_scan_started is not None:
                delay = last_scan_started + self.min_scan_interval_seconds - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            # Requests made while waiting are covered by this scan; ones made during it earn one trailing scan.
            self._wake.clear()
            last_scan_started = time.monotonic()
            await self._scan()

    async def _scan(self) -> None:
        config = self.config_provider()
        if config is None:
            return
        try:
            self._snapshot = await asyncio.to_thread(self._collect, config, self.clock())
        except Exception as error:
            # Usage storage is external state; keep enforcing the last good scan.
            logger.warning("budget_scan_failed", error=str(error))

    def _collect(self, config: Config, now: datetime) -> SpendSnapshot:
        if self._prices is None or self._prices[0] is not config:
            self._prices = (config, price_table(config, self.runtime_paths))
        return collect_monthly_spend(config, self.runtime_paths, now, self._prices[1])
