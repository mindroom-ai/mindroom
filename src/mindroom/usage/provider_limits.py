"""Per-account provider usage limit aggregation, 7-day token/cost attribution, and caching."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from mindroom.budgets.pricing import cost_usd, model_prices
from mindroom.logging_config import get_logger
from mindroom.usage.legacy_usage_storage import current_provider_name
from mindroom.usage.limit_store import PassiveWindow, format_utc_iso, get_passive_windows
from mindroom.usage.provider_accounts import AccountSpec, discover_accounts
from mindroom.usage.provider_probes import (
    ProbeResult,
    UnavailableReason,
    probe_claude_code,
    probe_codex,
    probe_openrouter,
)
from mindroom.usage.stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping
    from pathlib import Path

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.usage import TokenCounts
    from mindroom.usage.stats import UsageDailyBreakdownRow

__all__ = [
    "CACHE_TTL_SECONDS",
    "REFRESH_COOLDOWN_SECONDS",
    "USAGE_REPORT_TTL_SECONDS",
    "USAGE_REPORT_WAIT_SECONDS",
    "DailyAccountUsage",
    "ProviderLimitAccount",
    "ProviderUsageSnapshot",
    "UnavailableReason",
    "clear_cache",
    "fit_to_device_budget",
    "get_provider_usage_snapshot",
]

CACHE_TTL_SECONDS = 60.0
REFRESH_COOLDOWN_SECONDS = 10.0
USAGE_REPORT_TTL_SECONDS = 300.0
USAGE_REPORT_WAIT_SECONDS = 2.5
_MAX_CACHE_ENTRIES = 16
_MAX_LAST_GOOD_ENTRIES = 64
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderLimitAccount:
    """One billing account's limit windows and 7-day token/cost summary."""

    id: str
    provider: str
    display_name: str
    account_label: str | None
    plan: str | None
    model_keys: tuple[str, ...]
    windows: tuple[PassiveWindow, ...]
    unavailable_reason: UnavailableReason | None
    checked_at: str | None
    stale: bool
    tokens_7d: int
    cost_usd_7d: float | None
    cost_estimated: bool

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract account shape."""
        out: dict[str, object] = {"id": self.id, "provider": self.provider, "display_name": self.display_name}
        for key in ("account_label", "plan", "unavailable_reason", "checked_at"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        return out | {
            "model_keys": list(self.model_keys),
            "windows": [window.to_dict() for window in self.windows],
            "stale": self.stale,
            "tokens_7d": self.tokens_7d,
            "cost_usd_7d": self.cost_usd_7d,
            "cost_estimated": self.cost_estimated,
        }


@dataclass(frozen=True, slots=True)
class DailyAccountUsage:
    """Per-account token totals for one UTC calendar day."""

    day: str
    by_account: dict[str, int]
    unattributed_tokens: int

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract daily row shape."""
        return {"day": self.day, "by_account": dict(self.by_account), "unattributed_tokens": self.unattributed_tokens}


@dataclass(frozen=True, slots=True)
class ProviderUsageSnapshot:
    """Complete snapshot of provider limits and 7-day usage attribution."""

    checked_at: str
    partial: bool
    accounts: tuple[ProviderLimitAccount, ...]
    daily_7d: tuple[DailyAccountUsage, ...]
    unattributed_tokens_7d: int
    unattributed_cost_usd_7d: float | None

    def to_dict(self, *, request_id: str | None = None) -> dict[str, object]:
        """Serialize to the locked ``version: 1`` wire contract."""
        head: dict[str, object] = {"version": 1, **({"request_id": request_id} if request_id is not None else {})}
        return {
            **head,
            "checked_at": self.checked_at,
            "partial": self.partial,
            "accounts": [account.to_dict() for account in self.accounts],
            "daily_7d": [row.to_dict() for row in self.daily_7d],
            "unattributed_tokens_7d": self.unattributed_tokens_7d,
            "unattributed_cost_usd_7d": self.unattributed_cost_usd_7d,
        }


type _CacheKey = tuple[Path, Path, str]
type _DailyBreakdown = tuple[UsageDailyBreakdownRow, ...]
type _InFlight[T] = WeakKeyDictionary[asyncio.AbstractEventLoop, dict[_CacheKey, asyncio.Task[T]]]

_CACHE: OrderedDict[_CacheKey, tuple[ProviderUsageSnapshot, float]] = OrderedDict()
_LAST_GOOD: OrderedDict[tuple[tuple[Path, Path], str, str], ProbeResult] = OrderedDict()
_IN_FLIGHT: _InFlight[ProviderUsageSnapshot] = WeakKeyDictionary()
_USAGE_REPORT_CACHE: OrderedDict[_CacheKey, tuple[_DailyBreakdown, float]] = OrderedDict()
_USAGE_REPORT_IN_FLIGHT: _InFlight[_DailyBreakdown] = WeakKeyDictionary()
_CACHE_EPOCH = [0]


def _put_bounded[K, V](store: OrderedDict[K, V], key: K, value: V, *, max_entries: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > max_entries:
        store.popitem(last=False)


def _monotonic() -> float:
    return time.monotonic()


def clear_cache() -> None:
    """Clear cached snapshots, 7-day usage reports, and last-good probe readings (intended for tests)."""
    _CACHE_EPOCH[0] += 1
    _CACHE.clear()
    _LAST_GOOD.clear()
    _IN_FLIGHT.clear()
    _USAGE_REPORT_CACHE.clear()
    _USAGE_REPORT_IN_FLIGHT.clear()


def _passive_as_probe(spec: AccountSpec) -> ProbeResult | None:
    if spec.probe_kind not in {"codex", "claude_code"}:
        return None
    if (passive := get_passive_windows(spec.probe_kind, spec.scope_key)) is None:
        return None
    return ProbeResult(windows=passive.windows, observed_at=passive.observed_at, succeeded=True)


def _build_account(
    spec: AccountSpec,
    probe: ProbeResult,
    *,
    runtime_key: tuple[Path, Path],
    now: datetime,
    tokens_7d: int,
    cost_usd_7d: float | None,
    record_last_good: bool = True,
) -> ProviderLimitAccount:
    last_good_key = (runtime_key, spec.id, spec.scope_key)
    chosen: ProbeResult | None = None
    stale = False
    if probe.succeeded:
        if record_last_good:
            _put_bounded(_LAST_GOOD, last_good_key, probe, max_entries=_MAX_LAST_GOOD_ENTRIES)
        chosen = probe
    elif probe.unavailable_reason in {"unsupported", "not_signed_in"}:
        if spec.probe_kind == "codex" and probe.unavailable_reason == "not_signed_in":
            chosen = _passive_as_probe(spec)
            stale = chosen is not None
    else:
        candidates = [c for c in (_LAST_GOOD.get(last_good_key), _passive_as_probe(spec)) if c is not None]
        if candidates:
            chosen = max(candidates, key=lambda item: item.observed_at or datetime.min.replace(tzinfo=UTC))
            stale = True
    return ProviderLimitAccount(
        id=spec.id,
        provider=spec.provider,
        display_name=spec.display_name,
        account_label=chosen.account_label if chosen else None,
        plan=chosen.plan if chosen else None,
        model_keys=spec.model_keys,
        windows=chosen.windows if chosen else (),
        unavailable_reason=None if chosen else (probe.unavailable_reason or "probe_failed"),
        checked_at=format_utc_iso((chosen.observed_at if chosen else None) or now),
        stale=stale,
        tokens_7d=tokens_7d,
        cost_usd_7d=cost_usd_7d,
        cost_estimated=cost_usd_7d is not None,
    )


def _row_cost(totals: TokenCounts, priced: PricedModel | None) -> float | None:
    if priced is None or not any(
        (totals.input_tokens, totals.output_tokens, totals.cache_read_tokens, totals.cache_write_tokens),
    ):
        return None
    return cost_usd(totals, priced)


def _add_opt(base: float | None, delta: float | None) -> float | None:
    return base if delta is None else (base or 0.0) + delta


def _attribute_7d_usage(
    accounts: list[AccountSpec],
    daily_breakdown: _DailyBreakdown,
    config: Config,
    now: datetime,
) -> tuple[dict[str, int], dict[str, float | None], tuple[DailyAccountUsage, ...], int, float | None]:
    valid_days = {(now.astimezone(UTC).date() - timedelta(days=offset)).isoformat() for offset in range(7)}
    owners_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for account in accounts:
        for usage_key in account.usage_keys:
            owners_by_key[usage_key].add(account.id)
    prices = model_prices(config)
    tokens_by_account: dict[str, int] = defaultdict(int)
    cost_by_account: dict[str, float | None] = dict.fromkeys((a.id for a in accounts), None)
    unattributed_tokens, unattributed_cost = 0, None
    daily_rows: list[DailyAccountUsage] = []
    for row in sorted(daily_breakdown, key=lambda item: item.date):
        if row.date not in valid_days:
            continue
        day_by_account: dict[str, int] = defaultdict(int)
        day_unattributed, accounted = 0, 0
        for mrow in row.model_breakdown:
            tokens = mrow.totals.total_tokens
            accounted += tokens
            ukey = (current_provider_name(mrow.model_provider), mrow.model)
            row_cost = _row_cost(mrow.totals, prices.get(ukey))
            owners = owners_by_key.get(ukey, set())
            if len(owners) == 1:
                owner = next(iter(owners))
                tokens_by_account[owner] += tokens
                if tokens > 0:
                    day_by_account[owner] += tokens
                cost_by_account[owner] = _add_opt(cost_by_account[owner], row_cost)
            else:
                day_unattributed += tokens
                unattributed_tokens += tokens
                unattributed_cost = _add_opt(unattributed_cost, row_cost)
        if remainder := max(0, row.totals.total_tokens - accounted):
            day_unattributed += remainder
            unattributed_tokens += remainder
        daily_rows.append(
            DailyAccountUsage(
                day=row.date,
                by_account=dict(sorted(day_by_account.items())),
                unattributed_tokens=day_unattributed,
            ),
        )
    return (
        dict(tokens_by_account),
        {k: (None if v is None else round(v, 4)) for k, v in cost_by_account.items()},
        tuple(daily_rows),
        unattributed_tokens,
        None if unattributed_cost is None else round(unattributed_cost, 4),
    )


def _config_signature(config: Config) -> str:
    digest = hashlib.sha256()
    for name in sorted(config.models):
        digest.update(f"{name}\x00{config.models[name].model_dump_json()}\n".encode())
    return digest.hexdigest()


def _usage_sources_signature(config: Config) -> str:
    digest = hashlib.sha256()
    for section, entries in (("agents", config.agents), ("teams", config.teams)):
        for name in sorted(entries):
            digest.update(f"{section}\x00{name}\x00{entries[name].model_dump_json()}\n".encode())
    return digest.hexdigest()


async def _probe_account(account: AccountSpec, now: datetime) -> ProbeResult:
    if account.probe_kind == "codex":
        return await probe_codex(account.target, now)
    if account.probe_kind == "claude_code":
        return await probe_claude_code(account.target, now)
    if account.probe_kind == "openrouter":
        return await probe_openrouter(account.target, now)
    return ProbeResult(plan="Pay-as-you-go", account_label="Pay-as-you-go", observed_at=now, succeeded=True)


def _on_inflight_done[K, T](inflight: dict[K, asyncio.Task[T]], key: K, task: asyncio.Task[T]) -> None:
    if inflight.get(key) is task:
        inflight.pop(key, None)
    if not task.cancelled():
        # Retrieve any exception so timed-out callers do not leave an unretrieved background task exception.
        task.exception()


def _single_flight[T](
    registry: _InFlight[T],
    key: _CacheKey,
    factory: Callable[[], Coroutine[object, object, T]],
) -> asyncio.Task[T]:
    loop = asyncio.get_running_loop()
    inflight = registry.setdefault(loop, {})
    task = inflight.get(key)
    if task is None or task.done():
        task = loop.create_task(factory())
        inflight[key] = task
        task.add_done_callback(lambda done: _on_inflight_done(inflight, key, done))
    return task


def _get_cached_daily(usage_key: _CacheKey) -> _DailyBreakdown | None:
    if (cached := _USAGE_REPORT_CACHE.get(usage_key)) is None:
        return None
    _USAGE_REPORT_CACHE.move_to_end(usage_key)
    return cached[0]


def _ensure_usage_refresh(
    config: Config,
    runtime_paths: RuntimePaths,
    usage_key: _CacheKey,
    since: float,
    epoch: int,
) -> asyncio.Task[_DailyBreakdown]:
    async def _run() -> _DailyBreakdown:
        try:
            report = await asyncio.to_thread(
                collect_admin_usage,
                config=config,
                runtime_paths=runtime_paths,
                include_daily=True,
                since=since,
            )
        except Exception as exc:
            logger.warning("provider_usage_7d_scan_failed", error_type=type(exc).__name__)
            raise
        daily = report.daily_breakdown
        if epoch == _CACHE_EPOCH[0]:
            _put_bounded(_USAGE_REPORT_CACHE, usage_key, (daily, _monotonic()), max_entries=_MAX_CACHE_ENTRIES)
            for key in [k for k, (snap, _) in _CACHE.items() if k[:2] == usage_key[:2] and snap.partial]:
                _CACHE.pop(key, None)
        return daily

    return _single_flight(_USAGE_REPORT_IN_FLIGHT, usage_key, _run)


async def _resolve_daily_breakdown(
    usage_key: _CacheKey,
    usage_task: asyncio.Task[_DailyBreakdown] | None,
    fallback_daily: _DailyBreakdown | None,
) -> _DailyBreakdown | None:
    if (cached := _get_cached_daily(usage_key)) is not None:
        return cached
    if fallback_daily is not None or usage_task is None:
        return fallback_daily
    try:
        return await asyncio.wait_for(asyncio.shield(usage_task), timeout=USAGE_REPORT_WAIT_SECONDS)
    except TimeoutError:
        return _get_cached_daily(usage_key)
    except Exception:
        return None


async def _compute_snapshot(
    config: Config,
    runtime_paths: RuntimePaths,
    cache_key: _CacheKey,
    force_refresh: bool,
    now: datetime | None,
    epoch: int,
) -> ProviderUsageSnapshot:
    current_utc = (datetime.now(UTC) if now is None else now).astimezone(UTC)
    # Include one extra UTC day before the 7-day window so sessions started the prior day with runs in range are read.
    since = datetime.combine(current_utc.date() - timedelta(days=7), dt_time(), UTC).timestamp()
    specs = await asyncio.to_thread(discover_accounts, config, runtime_paths)
    runtime_key = (cache_key[0], cache_key[1])
    usage_key: _CacheKey = (*runtime_key, _usage_sources_signature(config))
    cached_daily, cached_monotonic = _USAGE_REPORT_CACHE.get(usage_key, (None, None))
    usage_ttl = REFRESH_COOLDOWN_SECONDS if force_refresh else USAGE_REPORT_TTL_SECONDS
    usage_task = (
        _ensure_usage_refresh(config, runtime_paths, usage_key, since, epoch)
        if cached_monotonic is None or (_monotonic() - cached_monotonic) >= usage_ttl
        else None
    )
    probe_results = await asyncio.gather(*(_probe_account(spec, current_utc) for spec in specs))
    daily_breakdown = await _resolve_daily_breakdown(usage_key, usage_task, cached_daily)
    tokens_7d, costs_7d, daily_7d, unattr_tokens, unattr_cost = (
        ({}, {}, (), 0, None)
        if daily_breakdown is None
        else _attribute_7d_usage(specs, daily_breakdown, config, current_utc)
    )
    record_cache = epoch == _CACHE_EPOCH[0]
    accounts = tuple(
        _build_account(
            spec,
            probe,
            runtime_key=runtime_key,
            now=current_utc,
            tokens_7d=tokens_7d.get(spec.id, 0),
            cost_usd_7d=costs_7d.get(spec.id),
            record_last_good=record_cache,
        )
        for spec, probe in zip(specs, probe_results, strict=True)
    )
    snapshot = ProviderUsageSnapshot(
        checked_at=format_utc_iso(current_utc),
        partial=daily_breakdown is None,
        accounts=accounts,
        daily_7d=daily_7d,
        unattributed_tokens_7d=unattr_tokens,
        unattributed_cost_usd_7d=unattr_cost,
    )
    if record_cache:
        _put_bounded(_CACHE, cache_key, (snapshot, _monotonic()), max_entries=_MAX_CACHE_ENTRIES)
    return snapshot


async def get_provider_usage_snapshot(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> ProviderUsageSnapshot:
    """Return cached or freshly probed per-account limits and 7-day usage."""
    cache_key = (runtime_paths.config_path, runtime_paths.storage_root, _config_signature(config))
    min_age = REFRESH_COOLDOWN_SECONDS if force_refresh else CACHE_TTL_SECONDS
    if (cached := _CACHE.get(cache_key)) is not None and (_monotonic() - cached[1]) < min_age:
        _CACHE.move_to_end(cache_key)
        return cached[0]
    epoch = _CACHE_EPOCH[0]
    task = _single_flight(
        _IN_FLIGHT,
        cache_key,
        lambda: _compute_snapshot(config, runtime_paths, cache_key, force_refresh, now, epoch),
    )
    return await asyncio.shield(task)


def _encoded_size(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload).encode("utf-8"))


def fit_to_device_budget(payload: dict[str, object], max_bytes: int = 48_000) -> dict[str, object]:
    """Fit a serialized snapshot under ``max_bytes`` UTF-8 by dropping ``daily_7d``, then trimming ``model_keys``."""
    result = copy.deepcopy(payload)
    if _encoded_size(result) <= max_bytes:
        return result
    result["daily_7d"] = []
    result["partial"] = True
    if _encoded_size(result) <= max_bytes:
        return result
    accounts = result.get("accounts")
    if isinstance(accounts, list):
        while _encoded_size(result) > max_bytes:
            longest = max(
                (
                    acct
                    for acct in accounts
                    if isinstance(acct, dict) and isinstance(acct.get("model_keys"), list) and acct["model_keys"]
                ),
                key=lambda acct: len(acct["model_keys"]),
                default=None,
            )
            if longest is None:
                break
            longest["model_keys"].pop()
        while _encoded_size(result) > max_bytes and accounts:
            accounts.pop()
    return result
