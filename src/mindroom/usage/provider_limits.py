"""Per-account provider usage limit aggregation, 7-day token/cost attribution, and caching."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import time
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from mindroom.budgets.pricing import cost_usd, model_prices
from mindroom.config.schema_hints import ENV_PLACEHOLDER_PATTERN
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.model_defaults import ANTHROPIC_BASE_URL_DEFAULT, OPENAI_BASE_URL_DEFAULT, OPENROUTER_BASE_URL_DEFAULT
from mindroom.providers.credentials import (
    model_api_key,
    model_with_dashboard_key,
    own_api_key,
    resolve_api_key_reference,
    uses_alternative_auth,
)
from mindroom.providers.identity import model_identity
from mindroom.providers.names import ROUTE_PROVIDERS, provider_route
from mindroom.usage.legacy_usage_storage import current_provider_name
from mindroom.usage.limit_store import PassiveWindow, format_utc_iso, get_passive_windows, normalize_codex_scope_key
from mindroom.usage.provider_probes import (
    ProbeResult,
    UnavailableReason,
    probe_claude_code,
    probe_codex,
    probe_openrouter,
)
from mindroom.usage.stats import collect_admin_usage

if TYPE_CHECKING:
    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.usage import TokenCounts
    from mindroom.usage.stats import UsageReport

__all__ = [
    "CACHE_TTL_SECONDS",
    "REFRESH_COOLDOWN_SECONDS",
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
_DISPLAY_OVERRIDES = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "bedrock_claude": "AWS Bedrock",
    "vertexai_claude": "Vertex AI",
    "llama_cpp": "llama.cpp",
    "zai": "Z.AI",
}


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
        optional = {
            k: v
            for k, v in (
                ("account_label", self.account_label),
                ("plan", self.plan),
                ("unavailable_reason", self.unavailable_reason),
                ("checked_at", self.checked_at),
            )
            if v is not None
        }
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            **optional,
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


@dataclass(frozen=True, slots=True)
class _AccountSpec:
    id: str
    provider: str
    display_name: str
    model_keys: tuple[str, ...]
    usage_keys: frozenset[tuple[str, str]]
    probe_kind: Literal["codex", "claude_code", "openrouter", "payg"]
    scope_key: str = ""
    target: str | None = None


@dataclass(slots=True)
class _CacheEntry:
    snapshot: ProviderUsageSnapshot
    fetched_monotonic: float


_CACHE: dict[tuple[Path, Path, str], _CacheEntry] = {}
_LAST_GOOD: dict[tuple[tuple[Path, Path, str], str], ProbeResult] = {}
_LOOP_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[Path, Path, str], asyncio.Lock]] = (
    WeakKeyDictionary()
)


def _monotonic() -> float:
    return time.monotonic()


def clear_cache() -> None:
    """Clear cached snapshots and last-good probe readings (intended for tests)."""
    _CACHE.clear()
    _LAST_GOOD.clear()


def _is_official_openrouter_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {"openrouter.ai", "www.openrouter.ai"}


def _effective_base_url(route: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    extra = model_config.extra_kwargs or {}
    client_params = extra.get("client_params") if isinstance(extra.get("client_params"), Mapping) else {}
    if isinstance(authored := (client_params.get("base_url") or extra.get("base_url")), str) and authored.strip():
        return authored.strip()
    if route == "openrouter":
        return OPENROUTER_BASE_URL_DEFAULT
    env_defaults = {
        "openai": ("OPENAI_BASE_URL", OPENAI_BASE_URL_DEFAULT),
        "anthropic": ("ANTHROPIC_BASE_URL", ANTHROPIC_BASE_URL_DEFAULT),
    }
    if route in env_defaults:
        return get_secret_from_env(env_defaults[route][0], runtime_paths=runtime_paths) or env_defaults[route][1]
    return None


def _resolve_openrouter_key(model_key: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    candidate = model_config
    with suppress(OSError, ValueError):
        candidate = model_with_dashboard_key(model_key, candidate, runtime_paths)
    candidate = resolve_api_key_reference(candidate, runtime_paths)
    if provider_route(candidate.provider) == "anthropic":
        key = own_api_key(candidate)
        if not key and isinstance(token := (candidate.extra_kwargs or {}).get("auth_token"), str) and token.strip():
            match = ENV_PLACEHOLDER_PATTERN.fullmatch(token.strip())
            key = get_secret_from_env(match.group(1), runtime_paths=runtime_paths) if match else token.strip()
        if not key and not uses_alternative_auth(candidate, runtime_paths):
            key = get_api_key_for_provider("anthropic", runtime_paths=runtime_paths)
    else:
        key = model_api_key(candidate, runtime_paths)
    return (
        key.strip()
        if isinstance(key, str) and key.strip() and not ENV_PLACEHOLDER_PATTERN.fullmatch(key.strip())
        else None
    )


def _resolve_cli_scope(cli_path: str | None) -> tuple[str | None, str]:
    effective = cli_path.strip() if isinstance(cli_path, str) and cli_path.strip() else shutil.which("claude")
    if not effective:
        return None, "default"
    if resolved := shutil.which(effective):
        absolute = str(Path(resolved).absolute())
        return absolute, absolute
    return effective, str(Path(effective).expanduser().resolve(strict=False))


def _display_name(route: str) -> str:
    return _DISPLAY_OVERRIDES.get(route, ROUTE_PROVIDERS.get(route, route.replace("_", " ").title()))


def _discover_accounts(config: Config, runtime_paths: RuntimePaths) -> list[_AccountSpec]:
    env_codex = get_secret_from_env("CODEX_HOME", runtime_paths=runtime_paths)
    defaults = {"claude_code": _resolve_cli_scope(None)[1], "codex": normalize_codex_scope_key(env_codex)}
    grouped: dict[str, dict[str, tuple[str | None, list[str], set[tuple[str, str]]]]] = {
        k: {} for k in ("claude_code", "codex", "openrouter", "payg")
    }
    for model_key in sorted(config.models):
        model_config = config.models[model_key]
        route, identity, extra = (
            provider_route(model_config.provider),
            model_identity(model_config),
            model_config.extra_kwargs or {},
        )
        if route == "codex":
            raw_home = extra.get("codex_home")
            target = raw_home.strip() if isinstance(raw_home, str) and raw_home.strip() else env_codex
            bucket, key = "codex", normalize_codex_scope_key(target)
        elif route == "claude_code":
            target, key = _resolve_cli_scope(extra.get("cli_path") if isinstance(extra.get("cli_path"), str) else None)
            bucket = "claude_code"
        elif route in {"openrouter", "openai", "anthropic"} and _is_official_openrouter_url(
            _effective_base_url(route, model_config, runtime_paths),
        ):
            target = _resolve_openrouter_key(model_key, model_config, runtime_paths)
            bucket, key = "openrouter", hashlib.sha256(target.encode()).hexdigest() if target else "__missing__"
        else:
            bucket, key, target = "payg", (f"{route}:custom" if route == "openrouter" else f"{route}:default"), route
        _, keys, ukeys = grouped[bucket].setdefault(key, (target, [], set()))
        keys.append(model_key)
        ukeys.add((identity.provider, identity.id))

    accounts: list[_AccountSpec] = []
    for kind in ("claude_code", "codex"):
        items = sorted(grouped[kind].items(), key=lambda item: (item[0] != defaults[kind], item[1][1][0]))
        accounts.extend(
            _AccountSpec(
                id=f"{kind}:default" if i == 0 else f"{kind}:{i + 1}",
                provider=kind,
                display_name=_display_name(kind),
                model_keys=tuple(keys),
                usage_keys=frozenset(ukeys),
                probe_kind=kind,
                scope_key=scope,
                target=target,
            )
            for i, (scope, (target, keys, ukeys)) in enumerate(items)
        )
    accounts.extend(
        _AccountSpec(
            id=f"openrouter:{i}",
            provider="openrouter",
            display_name=_display_name("openrouter"),
            model_keys=tuple(keys),
            usage_keys=frozenset(ukeys),
            probe_kind="openrouter",
            target=api_key,
        )
        for i, (_, (api_key, keys, ukeys)) in enumerate(
            sorted(grouped["openrouter"].items(), key=lambda item: item[1][1][0]),
            start=1,
        )
    )
    accounts.extend(
        _AccountSpec(
            id=aid,
            provider=route or "unknown",
            display_name=_display_name(route or "unknown"),
            model_keys=tuple(keys),
            usage_keys=frozenset(ukeys),
            probe_kind="payg",
        )
        for aid, (route, keys, ukeys) in sorted(grouped["payg"].items())
    )
    return accounts


def _build_account(
    spec: _AccountSpec,
    probe: ProbeResult,
    *,
    cache_key: tuple[Path, Path, str],
    now: datetime,
    tokens_7d: int,
    cost_usd_7d: float | None,
) -> ProviderLimitAccount:
    windows, plan, label, reason, checked, stale = (), None, None, probe.unavailable_reason, format_utc_iso(now), False
    if probe.succeeded:
        _LAST_GOOD[(cache_key, spec.id)] = probe
        windows, plan, label, reason, checked = (
            probe.windows,
            probe.plan,
            probe.account_label,
            None,
            format_utc_iso(probe.observed_at or now),
        )
    elif probe.unavailable_reason in {"unsupported", "not_signed_in"}:
        if (
            spec.probe_kind == "codex"
            and probe.unavailable_reason == "not_signed_in"
            and (passive := get_passive_windows("codex", spec.scope_key))
        ):
            windows, plan, label, reason, checked, stale = (
                passive.windows,
                passive.plan,
                passive.plan,
                None,
                format_utc_iso(passive.observed_at),
                True,
            )
    else:
        candidates = [last] if (last := _LAST_GOOD.get((cache_key, spec.id))) is not None else []
        if spec.probe_kind in {"codex", "claude_code"} and (
            passive := get_passive_windows(spec.probe_kind, spec.scope_key)
        ):
            candidates.append(
                ProbeResult(
                    windows=passive.windows,
                    plan=passive.plan,
                    account_label=passive.plan,
                    observed_at=passive.observed_at,
                    succeeded=True,
                ),
            )
        if candidates:
            best = max(candidates, key=lambda item: item.observed_at or datetime.min.replace(tzinfo=UTC))
            windows, plan, label, reason, checked, stale = (
                best.windows,
                best.plan,
                best.account_label,
                None,
                format_utc_iso(best.observed_at or now),
                True,
            )
        else:
            reason = probe.unavailable_reason or "probe_failed"
    return ProviderLimitAccount(
        id=spec.id,
        provider=spec.provider,
        display_name=spec.display_name,
        account_label=label,
        plan=plan,
        model_keys=spec.model_keys,
        windows=windows,
        unavailable_reason=reason,
        checked_at=checked,
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
    accounts: list[_AccountSpec],
    report: UsageReport,
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
    for row in sorted(report.daily_breakdown, key=lambda item: item.date):
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


async def _probe_account(account: _AccountSpec, now: datetime) -> ProbeResult:
    if account.probe_kind == "codex":
        return await probe_codex(account.target, now)
    if account.probe_kind == "claude_code":
        return await probe_claude_code(account.target, now)
    if account.probe_kind == "openrouter":
        return await probe_openrouter(account.target, now)
    return ProbeResult(plan="Pay-as-you-go", account_label="Pay-as-you-go", observed_at=now, succeeded=True)


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
    if (cached := _CACHE.get(cache_key)) is not None and (_monotonic() - cached.fetched_monotonic) < min_age:
        return cached.snapshot
    lock = _LOOP_LOCKS.setdefault(asyncio.get_running_loop(), {}).setdefault(cache_key, asyncio.Lock())
    async with lock:
        if (cached := _CACHE.get(cache_key)) is not None and (_monotonic() - cached.fetched_monotonic) < min_age:
            return cached.snapshot
        current_utc = (datetime.now(UTC) if now is None else now).astimezone(UTC)
        specs = _discover_accounts(config, runtime_paths)
        probe_results, report = await asyncio.gather(
            asyncio.gather(*(_probe_account(spec, current_utc) for spec in specs)),
            asyncio.to_thread(collect_admin_usage, config=config, runtime_paths=runtime_paths, include_daily=True),
        )
        tokens_7d, costs_7d, daily_7d, unattr_tokens, unattr_cost = _attribute_7d_usage(
            specs,
            report,
            config,
            current_utc,
        )
        accounts = tuple(
            _build_account(
                spec,
                probe,
                cache_key=cache_key,
                now=current_utc,
                tokens_7d=tokens_7d.get(spec.id, 0),
                cost_usd_7d=costs_7d.get(spec.id),
            )
            for spec, probe in zip(specs, probe_results, strict=True)
        )
        snapshot = ProviderUsageSnapshot(
            checked_at=format_utc_iso(current_utc),
            partial=False,
            accounts=accounts,
            daily_7d=daily_7d,
            unattributed_tokens_7d=unattr_tokens,
            unattributed_cost_usd_7d=unattr_cost,
        )
        _CACHE[cache_key] = _CacheEntry(snapshot=snapshot, fetched_monotonic=_monotonic())
        return snapshot


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
