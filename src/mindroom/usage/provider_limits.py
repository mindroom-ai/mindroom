"""Per-account provider usage limit aggregation, 7-day token/cost attribution, and caching."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from mindroom.budgets.pricing import cost_usd, model_prices
from mindroom.logging_config import get_logger
from mindroom.model_usage import provider_reports_cache_tokens_outside_input
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
from mindroom.usage.stats import collect_admin_daily_usage_with_entities

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
    "AgentUsage7d",
    "DailyAccountUsage",
    "ModelUsage7d",
    "ProviderLimitAccount",
    "ProviderUsageSnapshot",
    "TokenMix",
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
_MAX_TOP_ITEMS = 12
_RICH_TOP_LEVEL_KEYS = ("top_agents_7d", "top_models_7d", "token_mix_7d", "cache_savings_usd_7d")
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenMix:
    """Mutually exclusive 4-part token breakdown (``input_tokens`` excludes cached tokens)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def is_zero(self) -> bool:
        """Return whether all four token counters are zero."""
        return not (self.input_tokens or self.output_tokens or self.cache_read_tokens or self.cache_write_tokens)

    def add(self, other: TokenMix) -> TokenMix:
        """Return the counter-wise sum of two token mixes."""
        return TokenMix(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        """Serialize to the wire contract token mix shape."""
        return asdict(self)


def _opt_mix_dict(mix: TokenMix | None) -> dict[str, object]:
    return {"token_mix_7d": mix.to_dict()} if mix is not None and not mix.is_zero() else {}


def _rollup_to_dict(item: ModelUsage7d | AgentUsage7d, head: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {
        **head,
        "tokens_7d": item.tokens_7d,
        "cost_usd_7d": item.cost_usd_7d,
        "run_count": item.run_count,
        **_opt_mix_dict(item.token_mix_7d),
    }
    if item.daily_tokens_7d:
        out["daily_tokens_7d"] = list(item.daily_tokens_7d)
    return out


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
    token_mix_7d: TokenMix | None = None

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
            **_opt_mix_dict(self.token_mix_7d),
        }


@dataclass(frozen=True, slots=True)
class DailyAccountUsage:
    """Per-account token and cost totals for one UTC calendar day."""

    day: str
    by_account: dict[str, int]
    unattributed_tokens: int
    cost_by_account: dict[str, float] = field(default_factory=dict)
    unattributed_cost_usd: float | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract daily row shape."""
        out: dict[str, object] = {
            "day": self.day,
            "by_account": dict(self.by_account),
            "unattributed_tokens": self.unattributed_tokens,
        }
        if self.cost_by_account:
            out["cost_by_account"] = dict(self.cost_by_account)
        if self.unattributed_cost_usd is not None:
            out["unattributed_cost_usd"] = self.unattributed_cost_usd
        return out


@dataclass(frozen=True, slots=True)
class ModelUsage7d:
    """One model's 7-day token, cost, run count, and daily trend summary."""

    provider: str
    model: str
    account_id: str | None
    tokens_7d: int
    cost_usd_7d: float | None
    run_count: int
    token_mix_7d: TokenMix | None = None
    daily_tokens_7d: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract model rollup shape."""
        head: dict[str, object] = {"provider": self.provider, "model": self.model}
        if self.account_id is not None:
            head["account_id"] = self.account_id
        return _rollup_to_dict(self, head)


@dataclass(frozen=True, slots=True)
class AgentUsage7d:
    """One agent or team entity's 7-day token, cost, run count, and daily trend summary."""

    entity: str
    tokens_7d: int
    cost_usd_7d: float | None
    run_count: int
    token_mix_7d: TokenMix | None = None
    daily_tokens_7d: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract agent rollup shape."""
        return _rollup_to_dict(self, {"entity": self.entity})


@dataclass(frozen=True, slots=True)
class ProviderUsageSnapshot:
    """Complete snapshot of provider limits and 7-day usage attribution."""

    checked_at: str
    partial: bool
    accounts: tuple[ProviderLimitAccount, ...]
    daily_7d: tuple[DailyAccountUsage, ...]
    unattributed_tokens_7d: int
    unattributed_cost_usd_7d: float | None
    token_mix_7d: TokenMix | None = None
    cache_savings_usd_7d: float | None = None
    top_models_7d: tuple[ModelUsage7d, ...] = ()
    top_agents_7d: tuple[AgentUsage7d, ...] = ()

    def to_dict(self, *, request_id: str | None = None) -> dict[str, object]:
        """Serialize to the locked ``version: 1`` wire contract."""
        head: dict[str, object] = {"version": 1, **({"request_id": request_id} if request_id is not None else {})}
        out: dict[str, object] = {
            **head,
            "checked_at": self.checked_at,
            "partial": self.partial,
            "accounts": [account.to_dict() for account in self.accounts],
            "daily_7d": [row.to_dict() for row in self.daily_7d],
            "unattributed_tokens_7d": self.unattributed_tokens_7d,
            "unattributed_cost_usd_7d": self.unattributed_cost_usd_7d,
            **_opt_mix_dict(self.token_mix_7d),
        }
        if self.cache_savings_usd_7d is not None and self.cache_savings_usd_7d > 0:
            out["cache_savings_usd_7d"] = self.cache_savings_usd_7d
        if self.top_models_7d:
            out["top_models_7d"] = [model.to_dict() for model in self.top_models_7d]
        if self.top_agents_7d:
            out["top_agents_7d"] = [agent.to_dict() for agent in self.top_agents_7d]
        return out


@dataclass(slots=True)
class _RollupBucket:
    tokens: int = 0
    cost: float | None = None
    run_count: int = 0
    mix: TokenMix = field(default_factory=TokenMix)
    daily_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, day: str, tokens: int, cost: float | None, run_count: int, mix: TokenMix) -> None:
        self.tokens += tokens
        self.cost = _add_opt(self.cost, cost)
        self.run_count += run_count
        self.mix = self.mix.add(mix)
        self.daily_tokens[day] += tokens


@dataclass(frozen=True, slots=True)
class _UsageAttribution:
    tokens_by_account: dict[str, int] = field(default_factory=dict)
    cost_by_account: dict[str, float | None] = field(default_factory=dict)
    mix_by_account: dict[str, TokenMix] = field(default_factory=dict)
    daily_7d: tuple[DailyAccountUsage, ...] = ()
    unattributed_tokens_7d: int = 0
    unattributed_cost_usd_7d: float | None = None
    token_mix_7d: TokenMix | None = None
    cache_savings_usd_7d: float | None = None
    top_models_7d: tuple[ModelUsage7d, ...] = ()
    top_agents_7d: tuple[AgentUsage7d, ...] = ()


type _CacheKey = tuple[Path, Path, str]
type _DailyBreakdown = tuple[UsageDailyBreakdownRow, ...]
type _DailyByEntity = dict[str, _DailyBreakdown]
type _DailyUsageScan = tuple[_DailyBreakdown, _DailyByEntity]
type _InFlight[T] = WeakKeyDictionary[asyncio.AbstractEventLoop, dict[_CacheKey, asyncio.Task[T]]]

_CACHE: OrderedDict[_CacheKey, tuple[ProviderUsageSnapshot, float]] = OrderedDict()
_LAST_GOOD: OrderedDict[tuple[tuple[Path, Path], str, str], ProbeResult] = OrderedDict()
_IN_FLIGHT: _InFlight[ProviderUsageSnapshot] = WeakKeyDictionary()
_USAGE_REPORT_CACHE: OrderedDict[_CacheKey, tuple[_DailyUsageScan, float]] = OrderedDict()
_USAGE_REPORT_IN_FLIGHT: _InFlight[_DailyUsageScan] = WeakKeyDictionary()
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
    token_mix_7d: TokenMix | None = None,
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
        token_mix_7d=token_mix_7d,
    )


def _row_cost(totals: TokenCounts, priced: PricedModel | None) -> float | None:
    if priced is None or not any(
        (totals.input_tokens, totals.output_tokens, totals.cache_read_tokens, totals.cache_write_tokens),
    ):
        return None
    return cost_usd(totals, priced)


def _add_opt(base: float | None, delta: float | None) -> float | None:
    return base if delta is None else (base or 0.0) + delta


def _round_opt(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _non_zero_mix(mix: TokenMix) -> TokenMix | None:
    return None if mix.is_zero() else mix


def _token_mix_for_row(
    totals: TokenCounts,
    ukey: tuple[str, str],
    priced: PricedModel | None,
    configured_provider: str | None,
) -> TokenMix:
    input_includes_cache = (
        priced.input_includes_cache
        if priced is not None
        else not provider_reports_cache_tokens_outside_input(
            provider=ukey[0],
            configured_provider=configured_provider,
            model_id=ukey[1],
        )
    )
    uncached = (
        max(0, totals.input_tokens - totals.cache_read_tokens - totals.cache_write_tokens)
        if input_includes_cache
        else totals.input_tokens
    )
    return TokenMix(uncached, totals.output_tokens, totals.cache_read_tokens, totals.cache_write_tokens)


def _cache_savings(totals: TokenCounts, priced: PricedModel | None) -> float:
    if priced is None or totals.cache_read_tokens <= 0:
        return 0.0
    return totals.cache_read_tokens * max(0.0, priced.pricing.input - priced.pricing.cache_read_price) / 1_000_000


def _single_owner(owners_by_key: Mapping[tuple[str, str], set[str]], ukey: tuple[str, str]) -> str | None:
    owners = owners_by_key.get(ukey)
    return next(iter(owners)) if owners and len(owners) == 1 else None


def _build_top_models(
    model_buckets: Mapping[tuple[str, str], _RollupBucket],
    owners_by_key: Mapping[tuple[str, str], set[str]],
    day_order: tuple[str, ...],
) -> tuple[ModelUsage7d, ...]:
    models = [
        ModelUsage7d(
            provider,
            model,
            _single_owner(owners_by_key, (provider, model)),
            b.tokens,
            _round_opt(b.cost),
            b.run_count,
            _non_zero_mix(b.mix),
            tuple(b.daily_tokens.get(day, 0) for day in day_order),
        )
        for (provider, model), b in model_buckets.items()
    ]
    models.sort(key=lambda m: (m.cost_usd_7d is None, -(m.cost_usd_7d or 0.0), -m.tokens_7d, m.provider, m.model))
    return tuple(models[:_MAX_TOP_ITEMS])


def _build_top_agents(
    daily_by_entity: _DailyByEntity,
    valid_days: set[str],
    day_order: tuple[str, ...],
    prices: Mapping[tuple[str, str], PricedModel],
    configured_providers: Mapping[tuple[str, str], str],
) -> tuple[AgentUsage7d, ...]:
    agents: list[AgentUsage7d] = []
    for entity, rows in daily_by_entity.items():
        b, in_window = _RollupBucket(), False
        for row in rows:
            if row.date not in valid_days:
                continue
            in_window = True
            row_cost, row_mix = None, TokenMix()
            for mrow in row.model_breakdown:
                ukey = (current_provider_name(mrow.model_provider), mrow.model)
                priced = prices.get(ukey)
                row_cost = _add_opt(row_cost, _row_cost(mrow.totals, priced))
                row_mix = row_mix.add(_token_mix_for_row(mrow.totals, ukey, priced, configured_providers.get(ukey)))
            b.record(row.date, row.totals.total_tokens, row_cost, row.run_count, row_mix)
        if in_window:
            agents.append(
                AgentUsage7d(
                    entity,
                    b.tokens,
                    _round_opt(b.cost),
                    b.run_count,
                    _non_zero_mix(b.mix),
                    tuple(b.daily_tokens.get(day, 0) for day in day_order),
                ),
            )
    agents.sort(key=lambda a: (a.cost_usd_7d is None, -(a.cost_usd_7d or 0.0), -a.tokens_7d, a.entity))
    return tuple(agents[:_MAX_TOP_ITEMS])


def _attribute_7d_usage(
    accounts: list[AccountSpec],
    usage_scan: _DailyUsageScan,
    config: Config,
    now: datetime,
) -> _UsageAttribution:
    daily_breakdown, daily_by_entity = usage_scan
    valid_days = {(now.astimezone(UTC).date() - timedelta(days=offset)).isoformat() for offset in range(7)}
    owners_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    configured_providers: dict[tuple[str, str], str] = {}
    for account in accounts:
        for usage_key in account.usage_keys:
            owners_by_key[usage_key].add(account.id)
            configured_providers.setdefault(usage_key, account.provider)
    prices = model_prices(config)
    tokens_by_account: dict[str, int] = defaultdict(int)
    cost_by_account: dict[str, float | None] = dict.fromkeys((a.id for a in accounts), None)
    mix_by_account: dict[str, TokenMix] = defaultdict(TokenMix)
    model_buckets: dict[tuple[str, str], _RollupBucket] = defaultdict(_RollupBucket)
    total_mix, cache_savings, unattributed_tokens, unattributed_cost = TokenMix(), 0.0, 0, None
    daily_rows: list[DailyAccountUsage] = []
    for row in sorted(daily_breakdown, key=lambda item: item.date):
        if row.date not in valid_days:
            continue
        day_by_account: dict[str, int] = defaultdict(int)
        day_cost_by_account: dict[str, float] = defaultdict(float)
        day_unattributed, day_unattributed_cost, accounted = 0, None, 0
        for mrow in row.model_breakdown:
            tokens = mrow.totals.total_tokens
            accounted += tokens
            ukey = (current_provider_name(mrow.model_provider), mrow.model)
            priced = prices.get(ukey)
            row_cost = _row_cost(mrow.totals, priced)
            row_mix = _token_mix_for_row(mrow.totals, ukey, priced, configured_providers.get(ukey))
            total_mix = total_mix.add(row_mix)
            cache_savings += _cache_savings(mrow.totals, priced)
            model_buckets[ukey].record(row.date, tokens, row_cost, mrow.run_count, row_mix)
            if (owner := _single_owner(owners_by_key, ukey)) is not None:
                tokens_by_account[owner] += tokens
                if tokens > 0:
                    day_by_account[owner] += tokens
                cost_by_account[owner] = _add_opt(cost_by_account[owner], row_cost)
                if row_cost is not None:
                    day_cost_by_account[owner] += row_cost
                mix_by_account[owner] = mix_by_account[owner].add(row_mix)
            else:
                day_unattributed += tokens
                unattributed_tokens += tokens
                unattributed_cost = _add_opt(unattributed_cost, row_cost)
                day_unattributed_cost = _add_opt(day_unattributed_cost, row_cost)
        if remainder := max(0, row.totals.total_tokens - accounted):
            day_unattributed += remainder
            unattributed_tokens += remainder
        daily_rows.append(
            DailyAccountUsage(
                day=row.date,
                by_account=dict(sorted(day_by_account.items())),
                unattributed_tokens=day_unattributed,
                cost_by_account={k: round(v, 4) for k, v in sorted(day_cost_by_account.items())},
                unattributed_cost_usd=_round_opt(day_unattributed_cost),
            ),
        )
    day_order = tuple(row.day for row in daily_rows)
    rounded_savings = round(cache_savings, 4)
    return _UsageAttribution(
        tokens_by_account=dict(tokens_by_account),
        cost_by_account={k: _round_opt(v) for k, v in cost_by_account.items()},
        mix_by_account={k: mix for k, v in mix_by_account.items() if (mix := _non_zero_mix(v)) is not None},
        daily_7d=tuple(daily_rows),
        unattributed_tokens_7d=unattributed_tokens,
        unattributed_cost_usd_7d=_round_opt(unattributed_cost),
        token_mix_7d=_non_zero_mix(total_mix),
        cache_savings_usd_7d=rounded_savings if rounded_savings > 0.0 else None,
        top_models_7d=_build_top_models(model_buckets, owners_by_key, day_order),
        top_agents_7d=_build_top_agents(daily_by_entity, valid_days, day_order, prices, configured_providers),
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


def _get_cached_usage(usage_key: _CacheKey) -> _DailyUsageScan | None:
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
) -> asyncio.Task[_DailyUsageScan]:
    async def _run() -> _DailyUsageScan:
        try:
            usage_scan = await asyncio.to_thread(
                collect_admin_daily_usage_with_entities,
                config=config,
                runtime_paths=runtime_paths,
                since=since,
            )
        except Exception as exc:
            logger.warning("provider_usage_7d_scan_failed", error_type=type(exc).__name__)
            raise
        if epoch == _CACHE_EPOCH[0]:
            _put_bounded(_USAGE_REPORT_CACHE, usage_key, (usage_scan, _monotonic()), max_entries=_MAX_CACHE_ENTRIES)
            for key in [k for k, (snap, _) in _CACHE.items() if k[:2] == usage_key[:2] and snap.partial]:
                _CACHE.pop(key, None)
        return usage_scan

    return _single_flight(_USAGE_REPORT_IN_FLIGHT, usage_key, _run)


async def _resolve_daily_usage(
    usage_key: _CacheKey,
    usage_task: asyncio.Task[_DailyUsageScan] | None,
    fallback_usage: _DailyUsageScan | None,
) -> _DailyUsageScan | None:
    if (cached := _get_cached_usage(usage_key)) is not None:
        return cached
    if fallback_usage is not None or usage_task is None:
        return fallback_usage
    try:
        return await asyncio.wait_for(asyncio.shield(usage_task), timeout=USAGE_REPORT_WAIT_SECONDS)
    except TimeoutError:
        return _get_cached_usage(usage_key)
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
    cached_usage, cached_monotonic = _USAGE_REPORT_CACHE.get(usage_key, (None, None))
    usage_ttl = REFRESH_COOLDOWN_SECONDS if force_refresh else USAGE_REPORT_TTL_SECONDS
    usage_task = (
        _ensure_usage_refresh(config, runtime_paths, usage_key, since, epoch)
        if cached_monotonic is None or (_monotonic() - cached_monotonic) >= usage_ttl
        else None
    )
    probe_results = await asyncio.gather(*(_probe_account(spec, current_utc) for spec in specs))
    usage_scan = await _resolve_daily_usage(usage_key, usage_task, cached_usage)
    attribution = (
        _UsageAttribution() if usage_scan is None else _attribute_7d_usage(specs, usage_scan, config, current_utc)
    )
    record_cache = epoch == _CACHE_EPOCH[0]
    accounts = tuple(
        _build_account(
            spec,
            probe,
            runtime_key=runtime_key,
            now=current_utc,
            tokens_7d=attribution.tokens_by_account.get(spec.id, 0),
            cost_usd_7d=attribution.cost_by_account.get(spec.id),
            token_mix_7d=attribution.mix_by_account.get(spec.id),
            record_last_good=record_cache,
        )
        for spec, probe in zip(specs, probe_results, strict=True)
    )
    snapshot = ProviderUsageSnapshot(
        checked_at=format_utc_iso(current_utc),
        partial=usage_scan is None,
        accounts=accounts,
        daily_7d=attribution.daily_7d,
        unattributed_tokens_7d=attribution.unattributed_tokens_7d,
        unattributed_cost_usd_7d=attribution.unattributed_cost_usd_7d,
        token_mix_7d=attribution.token_mix_7d,
        cache_savings_usd_7d=attribution.cache_savings_usd_7d,
        top_models_7d=attribution.top_models_7d,
        top_agents_7d=attribution.top_agents_7d,
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


def _pop_keys(mapping: object, keys: tuple[str, ...]) -> bool:
    return isinstance(mapping, dict) and bool([mapping.pop(key) for key in keys if key in mapping])


def _strip_rich_analytics(payload: dict[str, object]) -> bool:
    stripped = _pop_keys(payload, _RICH_TOP_LEVEL_KEYS)
    for field_name, keys in (
        ("accounts", ("token_mix_7d",)),
        ("daily_7d", ("cost_by_account", "unattributed_cost_usd")),
    ):
        if isinstance(rows := payload.get(field_name), list):
            for row in rows:
                stripped |= _pop_keys(row, keys)
    return stripped


def fit_to_device_budget(payload: dict[str, object], max_bytes: int = 48_000) -> dict[str, object]:
    """Fit a serialized snapshot under ``max_bytes`` UTF-8 by shedding rich analytics, ``daily_7d``, then ``model_keys``."""
    result = copy.deepcopy(payload)
    if _encoded_size(result) <= max_bytes:
        return result
    if _strip_rich_analytics(result):
        result["partial"] = True
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
