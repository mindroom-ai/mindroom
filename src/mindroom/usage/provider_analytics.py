"""Pure 7-day usage attribution, normalized token mix, net cache savings, and rollup helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mindroom.budgets.pricing import cost_usd, model_prices
from mindroom.model_usage import provider_reports_cache_tokens_outside_input
from mindroom.usage.legacy_usage_storage import current_provider_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.llm.usage import TokenCounts
    from mindroom.usage.provider_accounts import AccountSpec
    from mindroom.usage.stats import UsageDailyBreakdownRow, UsageModelBreakdownRow

__all__ = [
    "AgentUsage7d",
    "DailyAccountUsage",
    "DailyUsageScan",
    "ModelUsage7d",
    "TokenMix",
    "UsageAttribution",
    "attribute_7d_usage",
    "opt_mix_dict",
    "strip_rich_analytics",
]

type _DailyBreakdown = tuple[UsageDailyBreakdownRow, ...]
type _DailyByEntity = dict[str, _DailyBreakdown]
type DailyUsageScan = tuple[_DailyBreakdown, _DailyByEntity]

_MAX_TOP_ITEMS = 12
_RICH_TOP_LEVEL_KEYS = ("top_agents_7d", "top_models_7d", "token_mix_7d", "cache_savings_usd_7d")


@dataclass(frozen=True, slots=True)
class TokenMix:
    """Mutually exclusive 4-part processed token breakdown (``input_tokens`` excludes cached tokens).

    Represents all processed tokens across the four buckets; on Anthropic-style providers where
    ``TokenCounts.total_tokens`` is ``input_tokens + output_tokens`` (excluding cache reads/writes),
    the sum of the four ``TokenMix`` buckets can exceed ``tokens_7d``.
    """

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


def opt_mix_dict(mix: TokenMix | None) -> dict[str, object]:
    """Return ``{"token_mix_7d": ...}`` when ``mix`` is non-None and non-zero, else ``{}``."""
    return {"token_mix_7d": mix.to_dict()} if mix is not None and not mix.is_zero() else {}


def _rollup_to_dict(item: ModelUsage7d | AgentUsage7d, head: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {
        **head,
        "tokens_7d": item.tokens_7d,
        "cost_usd_7d": item.cost_usd_7d,
        "run_count": item.run_count,
        **opt_mix_dict(item.token_mix_7d),
    }
    if item.daily_tokens_7d:
        out["daily_tokens_7d"] = list(item.daily_tokens_7d)
    return out


@dataclass(frozen=True, slots=True)
class DailyAccountUsage:
    """Per-account token and cost totals for one active UTC calendar day in the 7-day window."""

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
    """One model's 7-day token, cost, run count, processed token mix, and daily activity trend.

    ``daily_tokens_7d`` aligns 1:1 with ``ProviderUsageSnapshot.daily_7d`` in chronological day
    order (1..7 active UTC days, not always 7). ``token_mix_7d`` reports processed tokens across
    all four buckets, so its sum can exceed ``tokens_7d`` on Anthropic-style providers.
    """

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
    """One agent or team entity's 7-day token, cost, run count, processed token mix, and daily trend.

    ``daily_tokens_7d`` aligns 1:1 with ``ProviderUsageSnapshot.daily_7d`` in chronological day
    order (1..7 active UTC days, not always 7). ``token_mix_7d`` reports processed tokens across
    all four buckets, so its sum can exceed ``tokens_7d`` on Anthropic-style providers.
    """

    entity: str
    tokens_7d: int
    cost_usd_7d: float | None
    run_count: int
    token_mix_7d: TokenMix | None = None
    daily_tokens_7d: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Serialize to the wire contract agent rollup shape."""
        return _rollup_to_dict(self, {"entity": self.entity})


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


@dataclass(slots=True)
class _DayAccumulator:
    by_account: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cost_by_account: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    unattributed_tokens: int = 0
    unattributed_cost: float | None = None
    accounted_tokens: int = 0

    def to_daily_usage(self, day: str) -> DailyAccountUsage:
        return DailyAccountUsage(
            day=day,
            by_account=dict(sorted(self.by_account.items())),
            unattributed_tokens=self.unattributed_tokens,
            cost_by_account={k: round(v, 4) for k, v in sorted(self.cost_by_account.items())},
            unattributed_cost_usd=_round_opt(self.unattributed_cost),
        )


@dataclass(frozen=True, slots=True)
class UsageAttribution:
    """Attributed 7-day totals, daily rows, processed token mix, net cache savings, and top rollups."""

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
    """Return net cache savings in USD (cache-read discount minus cache-write premium)."""
    if priced is None or not (totals.cache_read_tokens or totals.cache_write_tokens):
        return 0.0
    pricing = priced.pricing
    return (
        totals.cache_read_tokens * (pricing.input - pricing.cache_read_price)
        - totals.cache_write_tokens * (pricing.cache_write_price - pricing.input)
    ) / 1_000_000


def _evaluate_model_row(
    mrow: UsageModelBreakdownRow,
    prices: Mapping[tuple[str, str], PricedModel],
    configured_providers: Mapping[tuple[str, str], str],
) -> tuple[tuple[str, str], float | None, TokenMix, float]:
    """Normalize provider identity and compute ``(usage_key, cost_usd, token_mix, net_cache_savings)``."""
    ukey = (current_provider_name(mrow.model_provider), mrow.model)
    priced = prices.get(ukey)
    return (
        ukey,
        _row_cost(mrow.totals, priced),
        _token_mix_for_row(mrow.totals, ukey, priced, configured_providers.get(ukey)),
        _cache_savings(mrow.totals, priced),
    )


def _single_owner(owners_by_key: Mapping[tuple[str, str], set[str]], ukey: tuple[str, str]) -> str | None:
    owners = owners_by_key.get(ukey)
    return next(iter(owners)) if owners and len(owners) == 1 else None


def _account_lookups(accounts: list[AccountSpec]) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], str]]:
    owners_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    configured_providers: dict[tuple[str, str], str] = {}
    for account in accounts:
        for usage_key in account.usage_keys:
            owners_by_key[usage_key].add(account.id)
            configured_providers.setdefault(usage_key, account.provider)
    return owners_by_key, configured_providers


def _apply_daily_remainders(
    daily_breakdown: _DailyBreakdown,
    valid_days: set[str],
    days_by_date: dict[str, _DayAccumulator],
) -> int:
    extra_unattributed = 0
    for row in daily_breakdown:
        if row.date not in valid_days:
            continue
        day_acc = days_by_date.setdefault(row.date, _DayAccumulator())
        if remainder := max(0, row.totals.total_tokens - day_acc.accounted_tokens):
            day_acc.unattributed_tokens += remainder
            extra_unattributed += remainder
    return extra_unattributed


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
    agent_buckets: Mapping[str, _RollupBucket],
    day_order: tuple[str, ...],
) -> tuple[AgentUsage7d, ...]:
    agents = [
        AgentUsage7d(
            entity,
            b.tokens,
            _round_opt(b.cost),
            b.run_count,
            _non_zero_mix(b.mix),
            tuple(b.daily_tokens.get(day, 0) for day in day_order),
        )
        for entity, b in agent_buckets.items()
    ]
    agents.sort(key=lambda a: (a.cost_usd_7d is None, -(a.cost_usd_7d or 0.0), -a.tokens_7d, a.entity))
    return tuple(agents[:_MAX_TOP_ITEMS])


def attribute_7d_usage(
    accounts: list[AccountSpec],
    usage_scan: DailyUsageScan,
    config: Config,
    now: datetime,
) -> UsageAttribution:
    """Attribute 7-day UTC daily usage across accounts, daily costs, token mix, net cache savings, and top rollups."""
    daily_breakdown, daily_by_entity = usage_scan
    valid_days = {(now.astimezone(UTC).date() - timedelta(days=offset)).isoformat() for offset in range(7)}
    owners_by_key, configured_providers = _account_lookups(accounts)
    prices = model_prices(config)
    tokens_by_account: dict[str, int] = defaultdict(int)
    cost_by_account: dict[str, float | None] = dict.fromkeys((a.id for a in accounts), None)
    mix_by_account: dict[str, TokenMix] = defaultdict(TokenMix)
    model_buckets: dict[tuple[str, str], _RollupBucket] = defaultdict(_RollupBucket)
    agent_buckets: dict[str, _RollupBucket] = defaultdict(_RollupBucket)
    days_by_date: dict[str, _DayAccumulator] = {}
    total_mix, cache_savings, unattributed_tokens, unattributed_cost = TokenMix(), 0.0, 0, None

    # Evaluate each (entity, date, provider, model) row once when entity rows are present so non-additive
    # clamps (such as max(0, input - cache_read - cache_write)) agree identically across accounts, models, and agents.
    row_stream = (
        ((entity, row) for entity, rows in daily_by_entity.items() for row in rows)
        if daily_by_entity
        else ((None, row) for row in daily_breakdown)
    )
    for entity, row in row_stream:
        if row.date not in valid_days:
            continue
        day_acc = days_by_date.setdefault(row.date, _DayAccumulator())
        row_entity_cost, row_entity_mix = None, TokenMix()
        for mrow in row.model_breakdown:
            tokens = mrow.totals.total_tokens
            day_acc.accounted_tokens += tokens
            ukey, row_cost, row_mix, row_savings = _evaluate_model_row(mrow, prices, configured_providers)
            total_mix = total_mix.add(row_mix)
            cache_savings += row_savings
            row_entity_cost = _add_opt(row_entity_cost, row_cost)
            row_entity_mix = row_entity_mix.add(row_mix)
            model_buckets[ukey].record(row.date, tokens, row_cost, mrow.run_count, row_mix)
            if (owner := _single_owner(owners_by_key, ukey)) is not None:
                tokens_by_account[owner] += tokens
                if tokens > 0:
                    day_acc.by_account[owner] += tokens
                cost_by_account[owner] = _add_opt(cost_by_account[owner], row_cost)
                if row_cost is not None:
                    day_acc.cost_by_account[owner] += row_cost
                mix_by_account[owner] = mix_by_account[owner].add(row_mix)
            else:
                day_acc.unattributed_tokens += tokens
                unattributed_tokens += tokens
                unattributed_cost = _add_opt(unattributed_cost, row_cost)
                day_acc.unattributed_cost = _add_opt(day_acc.unattributed_cost, row_cost)
        if entity is not None:
            agent_buckets[entity].record(
                row.date,
                row.totals.total_tokens,
                row_entity_cost,
                row.run_count,
                row_entity_mix,
            )

    unattributed_tokens += _apply_daily_remainders(daily_breakdown, valid_days, days_by_date)
    daily_rows = tuple(days_by_date[day].to_daily_usage(day) for day in sorted(days_by_date))
    day_order = tuple(row.day for row in daily_rows)
    rounded_savings = round(cache_savings, 4)
    return UsageAttribution(
        tokens_by_account=dict(tokens_by_account),
        cost_by_account={k: _round_opt(v) for k, v in cost_by_account.items()},
        mix_by_account={k: mix for k, v in mix_by_account.items() if (mix := _non_zero_mix(v)) is not None},
        daily_7d=daily_rows,
        unattributed_tokens_7d=unattributed_tokens,
        unattributed_cost_usd_7d=_round_opt(unattributed_cost),
        token_mix_7d=_non_zero_mix(total_mix),
        cache_savings_usd_7d=rounded_savings if rounded_savings > 0.0 else None,
        top_models_7d=_build_top_models(model_buckets, owners_by_key, day_order),
        top_agents_7d=_build_top_agents(agent_buckets, day_order),
    )


def _pop_keys(mapping: object, keys: tuple[str, ...]) -> bool:
    return isinstance(mapping, dict) and bool([mapping.pop(key) for key in keys if key in mapping])


def strip_rich_analytics(payload: dict[str, object]) -> bool:
    """Strip optional additive rich-analytics keys from a serialized snapshot in place."""
    stripped = _pop_keys(payload, _RICH_TOP_LEVEL_KEYS)
    for field_name, keys in (
        ("accounts", ("token_mix_7d",)),
        ("daily_7d", ("cost_by_account", "unattributed_cost_usd")),
    ):
        if isinstance(rows := payload.get(field_name), list):
            for row in rows:
                stripped |= _pop_keys(row, keys)
    return stripped
