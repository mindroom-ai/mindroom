"""Month-to-date spend per requester and the fallback model decision it drives.

A full scan of retained usage runs in a worker thread at start, after a config reload that changes what it counts, when
the UTC month changes, and on a slow reconciliation interval, which also picks up usage no reply reports, such as
helper calls, voice calls, and summaries.
Between scans each completed reply adds the cost it priced to its requester's running total, so it costs no scan.
A reply whose cost is unknown, such as a stopped or failed run or one resumed after an approval, requests a scan.
Spend is checked when a reply starts, so the reply that crosses a cap still finishes on its model, and the requester's
next reply uses the fallback model.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from mindroom.background_tasks import SupervisedLoop
from mindroom.budgets.caps import BudgetCaps, UserCap, budget_caps_path
from mindroom.budgets.pricing import model_prices
from mindroom.budgets.spend import SpendSnapshot, collect_monthly_spend, month_bounds
from mindroom.constants import resolve_session_state_root
from mindroom.logging_config import get_logger
from mindroom.providers.identity import model_identity
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.budgets import BudgetsConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_MIN_SCAN_INTERVAL_SECONDS = 30.0
# A scan is CPU-bound Python in a worker thread, so it holds the GIL the event loop needs.
# Spacing scan starts by ten scan durations keeps a slow scan from starving the loop.
_SCAN_SPACING_FACTOR = 10.0
# Replies add their own cost between scans; this rescan adds usage no reply reports and replaces their running totals.
_RECONCILE_SECONDS = 6 * 60 * 60.0
# How often the loop checks whether the month changed or reconciliation is due; a check that finds neither is free.
_TICK_SECONDS = 300.0


def _budget_limit_usd(budgets: BudgetsConfig, cap: UserCap | None) -> float | None:
    """Return the monthly cap in USD for a user with ``cap``, or None when the user is uncapped."""
    return cap.limit_usd if cap is not None else budgets.monthly_limit_usd


def budget_user_id(user_id: str, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the canonical person whose cap and spend ``user_id`` uses, or None for agents and bots."""
    if not is_human_requester_id(user_id, config, runtime_paths):
        return None
    # A bridge alias shares its canonical user's spend, so it shares one cap too.
    return resolve_human_requester_alias(user_id, config, runtime_paths)


def _is_priced(config: Config, model_name: str) -> bool:
    """Return whether usage of this model adds spend: whether it or another entry with its identity has prices."""
    model_config = config.models.get(model_name)
    if model_config is None:
        return False
    identity = model_identity(model_config)
    return (identity.provider, identity.id) in model_prices(config)


def _scan_fingerprint(config: Config, runtime_paths: RuntimePaths) -> str:
    """Return a digest of whether budgets are on and of the settings that change what a scan counts.

    Those are prices, the session stores a scan reads, and the agents, teams, bot accounts, internal user, and aliases
    that decide whose spend a run is. Caps and the fallback model apply when spend is read, so they are left out.
    """
    prices = [
        [provider, model_id, priced.pricing.model_dump(mode="json"), priced.input_includes_cache]
        for (provider, model_id), priced in sorted(model_prices(config).items())
    ]
    payload = [
        # Replies add nothing while budgets are off, so turning them back on must recount.
        config.budgets is not None,
        prices,
        str(resolve_session_state_root(runtime_paths.storage_root, runtime_paths).expanduser().resolve()),
        sorted((name, agent.private is not None) for name, agent in config.agents.items()),
        sorted(config.teams),
        sorted(config.bot_accounts),
        config.mindroom_user.username if config.mindroom_user is not None else None,
        {canonical: sorted(aliases) for canonical, aliases in config.authorization.aliases.items()},
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def budget_model(
    config: Config,
    runtime_paths: RuntimePaths,
    monitor: BudgetMonitor | None,
    requester_id: str | None,
    model_name: str,
    *,
    log_fallback: bool = True,
) -> str:
    """Return the model a reply for this requester should use under their budget.

    ``config`` is the snapshot ``model_name`` was resolved from, so the fallback names a model it defines.
    Without a monitor neither spend nor per-user caps are known, so the default cap applies against zero spend.
    Callers that only check, without choosing a reply model, pass ``log_fallback=False``.
    """
    budgets = config.budgets
    if budgets is None or requester_id is None:
        return model_name
    # Unpriced models add no tracked spend, so swapping them could only raise cost.
    if model_name == budgets.fallback_model or not _is_priced(config, model_name):
        return model_name
    canonical_requester_id = budget_user_id(requester_id, config, runtime_paths)
    if canonical_requester_id is None:
        return model_name
    limit = _budget_limit_usd(budgets, monitor.caps.get(canonical_requester_id) if monitor is not None else None)
    if limit is None:
        return model_name
    spend = monitor._spend_usd(canonical_requester_id) if monitor is not None else 0.0
    if spend < limit:
        return model_name
    if log_fallback:
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
    cap: UserCap | None

    def to_dict(self) -> dict[str, object]:
        """Return the dashboard row."""
        cap = self.cap
        return {
            "user_id": self.user_id,
            "spend_usd": self.spend_usd,
            "limit_usd": self.limit_usd,
            "over_budget": self.over_budget,
            "override_usd": cap.limit_usd if cap is not None else None,
            "updated_at": cap.updated_at.isoformat() if cap is not None else None,
            "updated_by": cap.updated_by if cap is not None else None,
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

    name = "budgets"
    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    clock: Callable[[], datetime] = _utc_now
    min_scan_interval_seconds: float = _MIN_SCAN_INTERVAL_SECONDS
    scan_spacing_factor: float = _SCAN_SPACING_FACTOR
    reconcile_seconds: float = _RECONCILE_SECONDS
    tick_seconds: float = _TICK_SECONDS
    caps: BudgetCaps = field(init=False)
    _snapshot: SpendSnapshot | None = field(default=None, init=False)
    # The scan fingerprint of the config the latest sync applied.
    _synced_fingerprint: str | None = field(default=None, init=False)
    # Costs of finished replies per canonical requester that the snapshot does not cover: those recorded before the
    # running or latest scan started, which that scan covers once it completes, and those recorded since.
    _costs_before_scan: dict[str, float] = field(default_factory=dict, init=False)
    _costs_since_scan: dict[str, float] = field(default_factory=dict, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _loop: SupervisedLoop = field(init=False)

    def __post_init__(self) -> None:
        """Load the per-user caps a previous run stored."""
        self.caps = BudgetCaps(budget_caps_path(self.runtime_paths))
        self._loop = SupervisedLoop("budget_monitor", self._run)

    async def sync(self, config: Config) -> None:
        """Start the refresh loop once; rescan on the first sync and when ``config`` changes what a scan counts."""
        fingerprint = _scan_fingerprint(config, self.runtime_paths)
        previous, self._synced_fingerprint = self._synced_fingerprint, fingerprint
        self._loop.start()
        if fingerprint != previous:
            self._request_scan()

    def response_finished(self, requester_id: str | None, cost_usd: float | None, started_at: datetime | None) -> None:
        """Add a finished reply's cost to its requester's spend until a scan covers the reply, or request a scan.

        ``requester_id`` is the requester the reply ran for, which its stored run names, so the cost counts for the
        person a scan charges: an alias's canonical user, and nobody for an agent or a bot. A private agent keeps one
        instance per requester, so that requester is also the owner a scan charges for a run that names none, and a
        team reply's cost covers its members' runs, which name the same requester.
        ``cost_usd`` is the exact cost of the reply's stored usage, zero when it used no priced model, or None when it
        is unknown; then a scan counts the usage instead. ``started_at`` is when the reply's turn was requested, None
        when its cost cannot be dated; a scan dates usage by request, so a reply that started in an earlier month has
        an unknown cost for this one.
        """
        if requester_id is None:
            return
        config = self.config_provider()
        if config is None or config.budgets is None:
            return
        user_id = budget_user_id(requester_id, config, self.runtime_paths)
        if user_id is None:
            return
        if started_at is None or month_bounds(started_at)[0] != month_bounds(self.clock())[0]:
            cost_usd = None
        if cost_usd is None:
            self._request_scan()
        elif cost_usd:
            self._costs_since_scan[user_id] = self._costs_since_scan.get(user_id, 0.0) + cost_usd

    async def stop(self) -> None:
        """Stop the refresh loop; the last snapshot stays readable."""
        await self._loop.stop()

    def _spend_usd(self, user_id: str) -> float:
        """Return a canonical requester's month-to-date spend, or zero until a scan of this month completed."""
        snapshot = self._current_snapshot()
        return 0.0 if snapshot is None else self._spend_with_replies(snapshot, user_id)

    def _spend_with_replies(self, snapshot: SpendSnapshot, user_id: str) -> float:
        """Return ``user_id``'s spend in ``snapshot`` plus the costs replies reported since the scan behind it started.

        That scan dropped every cost recorded before it started, so the costs left fall in its month. A reply that
        finished while it ran may also be in ``snapshot`` until the next scan.
        """
        return (
            snapshot.spend_usd.get(user_id, 0.0)
            + self._costs_before_scan.get(user_id, 0.0)
            + self._costs_since_scan.get(user_id, 0.0)
        )

    def status(self) -> _BudgetStatus:
        """Return budget settings with month-to-date spend for each spender and each user with a cap."""
        config = self.config_provider()
        if config is None or config.budgets is None:
            return _BudgetStatus(enabled=False)
        budgets = config.budgets
        period_start, period_end = month_bounds(self.clock())
        snapshot = self._current_snapshot()
        spenders = (
            set(snapshot.spend_usd) | set(self._costs_before_scan) | set(self._costs_since_scan)
            if snapshot is not None
            else set()
        )
        caps = self.caps.entries()
        users = []
        for user_id in spenders | set(caps):
            # Agents and bridge bots are never budgeted, and a cap stored under an ID that is now a
            # bridge alias no longer applies, because replies resolve that ID to its canonical user.
            if budget_user_id(user_id, config, self.runtime_paths) != user_id:
                continue
            cap = caps.get(user_id)
            limit = _budget_limit_usd(budgets, cap)
            user_spend = self._spend_with_replies(snapshot, user_id) if snapshot is not None else 0.0
            users.append(
                _BudgetUserStatus(
                    user_id=user_id,
                    spend_usd=user_spend,
                    limit_usd=limit,
                    over_budget=limit is not None and user_spend >= limit,
                    cap=cap,
                ),
            )
        return _BudgetStatus(
            enabled=True,
            period_start=period_start,
            period_end=period_end,
            snapshot=snapshot,
            default_limit_usd=budgets.monthly_limit_usd,
            fallback_model=budgets.fallback_model,
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
        last_scan_seconds = 0.0
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_seconds)
            config = self.config_provider()
            if config is None or config.budgets is None:
                self._wake.clear()
                continue
            if not self._wake.is_set() and not self._scan_due(last_scan_started):
                continue
            if last_scan_started is not None:
                interval = max(self.min_scan_interval_seconds, self.scan_spacing_factor * last_scan_seconds)
                delay = last_scan_started + interval - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            # Requests made while waiting are covered by this scan; ones made during it earn one trailing scan.
            self._wake.clear()
            last_scan_started = time.monotonic()
            await self._scan()
            last_scan_seconds = time.monotonic() - last_scan_started

    def _scan_due(self, last_scan_started: float | None) -> bool:
        """Return whether no scan ran yet, none of this month completed, or reconciliation is due."""
        return (
            last_scan_started is None
            or self._current_snapshot() is None
            or time.monotonic() - last_scan_started >= self.reconcile_seconds
        )

    async def _scan(self) -> None:
        config = self.config_provider()
        if config is None:
            return
        # The scan covers the replies recorded until now. A reply that finishes while it runs stays counted until the
        # next scan, though the scan may already have read its usage; that counts it twice for a while, which only
        # brings the fallback forward by about one reply.
        for user_id, cost_usd in self._costs_since_scan.items():
            self._costs_before_scan[user_id] = self._costs_before_scan.get(user_id, 0.0) + cost_usd
        self._costs_since_scan = {}
        try:
            snapshot = await asyncio.to_thread(self._collect, config, self.clock())
        except Exception:
            # Usage storage is external state; keep enforcing the last good scan and the replies since it started.
            logger.warning("budget_scan_failed", exc_info=True)
            return
        self._snapshot = snapshot
        self._costs_before_scan = {}

    def _collect(self, config: Config, now: datetime) -> SpendSnapshot:
        return collect_monthly_spend(config, self.runtime_paths, now, model_prices(config))
