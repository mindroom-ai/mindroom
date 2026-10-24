"""Budget monitor: cached month-to-date spend and fallback model decisions."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.budgets import monitor as monitor_module
from mindroom.budgets import pricing as pricing_module
from mindroom.budgets.caps import BudgetCaps, UserCap, budget_caps_path
from mindroom.budgets.monitor import BudgetMonitor, _budget_limit_usd, _BudgetUserStatus, budget_model, budget_user_id
from mindroom.budgets.pricing import _PriceTable
from mindroom.budgets.spend import SpendSnapshot, _UnpricedModelUsage
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from tests.budget_helpers import set_month_spend

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

ALICE = "@alice:example.test"
ALICE_BRIDGE = "@telegram-alice:example.test"
BOB = "@bob:example.test"
OCTOBER = datetime(2026, 10, 9, 12, tzinfo=UTC)


def _config(**budgets: object) -> Config:
    return Config(
        models={
            "astra": ModelConfig(provider="openai", id="gpt-6-astra", pricing=ModelPricing(input=5, output=30)),
            "luna": ModelConfig(provider="openai", id="gpt-6-luna", pricing=ModelPricing(input=0.2, output=1.25)),
            "local": ModelConfig(provider="ollama", id="qwen3.8:27b"),
        },
        authorization=AuthorizationConfig(aliases={ALICE: [ALICE_BRIDGE]}),
        budgets={"fallback_model": "luna", "monthly_limit_usd": 10, **budgets},
    )


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _snapshot(spend: Mapping[str, float], *, start: date = date(2026, 10, 1), unavailable: int = 0) -> SpendSnapshot:
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return SpendSnapshot(
        period_start=start,
        period_end=end,
        generated_at=OCTOBER,
        spend_usd=dict(spend),
        unpriced_models=(_UnpricedModelUsage(provider="Ollama", model="qwen3.8:27b", total_tokens=5),),
        scanned_sources=2,
        unavailable_sources=unavailable,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = OCTOBER

    def __call__(self) -> datetime:
        return self.now


class _Scans:
    """Fake spend scans that record each call and can hold one open."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, spend: Mapping[str, float]) -> None:
        self.spend = dict(spend)
        self.calls = 0
        self.release = threading.Event()
        self.release.set()
        self.error: Exception | None = None
        self.unavailable = 0
        monkeypatch.setattr(
            pricing_module,
            "_price_table",
            lambda _config, _paths: _PriceTable(prices={}, complete=True),
        )
        monkeypatch.setattr(monitor_module, "collect_monthly_spend", self._collect)

    def _collect(self, _config: Config, _paths: RuntimePaths, now: datetime, _prices: object) -> SpendSnapshot:
        self.calls += 1
        self.release.wait(5)
        if self.error is not None:
            raise self.error
        start = now.date().replace(day=1)
        return _snapshot(self.spend, start=start, unavailable=self.unavailable)


def _monitor(tmp_path: Path, config: Config, clock: _Clock | None = None) -> tuple[BudgetMonitor, list[Config]]:
    current = [config]
    monitor = BudgetMonitor(
        runtime_paths=_paths(tmp_path),
        config_provider=lambda: current[0],
        clock=clock or _Clock(),
        min_scan_interval_seconds=0,
    )
    return monitor, current


def _decide(monitor: BudgetMonitor, requester_id: str | None, model_name: str) -> str:
    config = monitor.config_provider()
    assert config is not None
    return budget_model(config, monitor.runtime_paths, monitor, requester_id, model_name)


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "condition not reached"
    raise AssertionError(msg)


async def _quiet() -> None:
    """Give the monitor loop time to start any further scan it would wrongly run."""
    await asyncio.sleep(0.1)


async def _started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spend: Mapping[str, float],
    config: Config | None = None,
) -> tuple[BudgetMonitor, _Scans, list[Config]]:
    scans = _Scans(monkeypatch, spend)
    monitor, current = _monitor(tmp_path, config or _config())
    monitor.sync()
    await _until(lambda: monitor.status().snapshot is not None)
    return monitor, scans, current


@pytest.mark.asyncio
async def test_over_budget_requester_gets_fallback_for_priced_models_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0, BOB: 3.0})

    assert _decide(monitor, ALICE, "astra") == "luna"
    assert _decide(monitor, ALICE_BRIDGE, "astra") == "luna"
    assert _decide(monitor, ALICE, "local") == "local"
    assert _decide(monitor, BOB, "astra") == "astra"
    assert _decide(monitor, None, "astra") == "astra"
    await monitor.stop()


@pytest.mark.asyncio
async def test_fallback_model_stays_usable_past_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 500.0})

    assert _decide(monitor, ALICE, "luna") == "luna"
    await monitor.stop()


@pytest.mark.asyncio
async def test_reaching_the_cap_exactly_applies_the_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 10.0})

    assert _decide(monitor, ALICE, "astra") == "luna"
    await monitor.stop()


@pytest.mark.asyncio
async def test_stored_cap_beats_the_default_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0, BOB: 3.0})
    monitor.caps.set(ALICE, 100, updated_by="test", now=OCTOBER)
    monitor.caps.set(BOB, 1, updated_by="test", now=OCTOBER)

    assert _decide(monitor, ALICE, "astra") == "astra"
    assert _decide(monitor, ALICE_BRIDGE, "astra") == "astra"
    assert _decide(monitor, BOB, "astra") == "luna"
    await monitor.stop()


def test_budget_limit_uses_the_users_cap_then_the_default() -> None:
    cap = UserCap(limit_usd=100.0, updated_at=OCTOBER, updated_by="test")
    config = _config()

    assert config.budgets is not None
    assert _budget_limit_usd(config.budgets, cap) == 100.0
    assert _budget_limit_usd(config.budgets, None) == 10.0
    uncapped = _config(monthly_limit_usd=None).budgets
    assert uncapped is not None
    assert _budget_limit_usd(uncapped, None) is None


def test_monitor_loads_caps_stored_by_an_earlier_run(tmp_path: Path) -> None:
    BudgetCaps(budget_caps_path(_paths(tmp_path))).set(ALICE, 100, updated_by="test", now=OCTOBER)

    monitor, _current = _monitor(tmp_path, _config())

    assert monitor.caps.get(ALICE) == UserCap(limit_usd=100.0, updated_at=OCTOBER, updated_by="test")


def test_an_unpriced_alias_of_a_priced_model_is_swapped_too(tmp_path: Path) -> None:
    config = _config(monthly_limit_usd=0)
    config.models["astra_fast"] = ModelConfig(provider="openai", id="gpt-6-astra")

    assert budget_model(config, _paths(tmp_path), None, ALICE, "astra_fast") == "luna"


def test_no_snapshot_yet_means_zero_spend(tmp_path: Path) -> None:
    monitor, _current = _monitor(tmp_path, _config())

    assert monitor._spend_usd(ALICE) == 0.0
    assert _decide(monitor, ALICE, "astra") == "astra"


def test_without_a_monitor_only_zero_caps_apply(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert budget_model(_config(), paths, None, ALICE, "astra") == "astra"
    assert budget_model(_config(monthly_limit_usd=0), paths, None, ALICE, "astra") == "luna"


def test_disabled_budgets_ignore_spend(tmp_path: Path) -> None:
    config = _config().model_copy(update={"budgets": None})
    monitor, _current = _monitor(tmp_path, config)
    set_month_spend(monitor, {ALICE: 100.0})

    assert budget_model(config, monitor.runtime_paths, monitor, ALICE, "astra") == "astra"


@pytest.mark.asyncio
async def test_zero_cap_always_uses_the_fallback(tmp_path: Path) -> None:
    monitor, _current = _monitor(tmp_path, _config(monthly_limit_usd=0))

    assert _decide(monitor, ALICE, "astra") == "luna"


@pytest.mark.asyncio
async def test_previous_month_snapshot_counts_as_zero_after_rollover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    scans = _Scans(monkeypatch, {ALICE: 12.0})
    monitor, _current = _monitor(tmp_path, _config(), clock)
    monitor.sync()
    await _until(lambda: monitor.status().snapshot is not None)
    scans.release.clear()

    clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)

    assert monitor._spend_usd(ALICE) == 0.0
    assert _decide(monitor, ALICE, "astra") == "astra"
    scans.release.set()
    await monitor.stop()


@pytest.mark.asyncio
async def test_refreshes_are_debounced_with_one_trailing_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = _Scans(monkeypatch, {})
    scans.release.clear()
    monitor, _current = _monitor(tmp_path, _config())
    monitor.sync()
    await _until(lambda: scans.calls == 1)

    for _ in range(5):
        monitor.response_finished()
    scans.release.set()
    await _until(lambda: scans.calls == 2)
    await _quiet()

    assert scans.calls == 2
    await monitor.stop()


@pytest.mark.asyncio
async def test_scan_failure_keeps_previous_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0})
    scans.error = RuntimeError("database locked")

    monitor.response_finished()
    await _until(lambda: scans.calls == 2)
    await _quiet()

    assert monitor._spend_usd(ALICE) == 12.0
    await monitor.stop()


@pytest.mark.asyncio
async def test_disabled_budgets_never_scan_or_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, scans, current = await _started(tmp_path, monkeypatch, {ALICE: 12.0})
    current[0] = current[0].model_copy(update={"budgets": None})

    monitor.sync()
    await _quiet()

    assert scans.calls == 1
    assert _decide(monitor, ALICE, "astra") == "astra"
    assert monitor.status().to_dict() == {"enabled": False}
    await monitor.stop()


@pytest.mark.asyncio
async def test_status_lists_spenders_and_users_with_stored_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0, BOB: 3.0})
    carol_cap = monitor.caps.set("@carol:example.test", 50, updated_by="budget-service", now=OCTOBER)

    status = monitor.status()

    assert status.users == (
        _BudgetUserStatus(user_id=ALICE, spend_usd=12.0, limit_usd=10.0, over_budget=True, cap=None),
        _BudgetUserStatus(user_id=BOB, spend_usd=3.0, limit_usd=10.0, over_budget=False, cap=None),
        _BudgetUserStatus(
            user_id="@carol:example.test",
            spend_usd=0.0,
            limit_usd=50.0,
            over_budget=False,
            cap=carol_cap,
        ),
    )
    payload = status.to_dict()
    assert payload["enabled"] is True
    assert payload["period_start"] == "2026-10-01"
    assert payload["period_end"] == "2026-11-01"
    assert payload["generated_at"] == "2026-10-09T12:00:00+00:00"
    assert payload["default_limit_usd"] == 10.0
    assert payload["fallback_model"] == "luna"
    assert payload["users"][0] == {
        "user_id": ALICE,
        "spend_usd": 12.0,
        "limit_usd": 10.0,
        "over_budget": True,
        "override_usd": None,
        "updated_at": None,
        "updated_by": None,
    }
    assert payload["users"][2] == {
        "user_id": "@carol:example.test",
        "spend_usd": 0.0,
        "limit_usd": 50.0,
        "over_budget": False,
        "override_usd": 50.0,
        "updated_at": "2026-10-09T12:00:00+00:00",
        "updated_by": "budget-service",
    }
    assert payload["unpriced_models"] == [{"provider": "Ollama", "model": "qwen3.8:27b", "total_tokens": 5}]
    assert payload["coverage"] == {"scanned_sources": 2, "unavailable_sources": 0}
    await monitor.stop()


def test_status_hides_a_stored_cap_whose_id_became_an_alias(tmp_path: Path) -> None:
    """A cap stored under an ID that is now a bridge alias no longer applies, so it is not listed."""
    monitor, _current = _monitor(tmp_path, _config())
    monitor.caps.set(ALICE_BRIDGE, 0, updated_by="test", now=OCTOBER)
    monitor.caps.set(ALICE, 100, updated_by="test", now=OCTOBER)

    assert [(user.user_id, user.limit_usd) for user in monitor.status().users] == [(ALICE, 100.0)]


def test_status_before_first_scan_reports_current_month_without_spend(tmp_path: Path) -> None:
    monitor, _current = _monitor(tmp_path, _config())

    payload = monitor.status().to_dict()

    assert payload["generated_at"] is None
    assert payload["period_start"] == "2026-10-01"
    assert payload["users"] == []


def test_budget_user_id_names_the_canonical_person_or_none_for_bots(tmp_path: Path) -> None:
    config = _config().model_copy(update={"bot_accounts": ["@bridgebot:example.test"]})
    paths = _paths(tmp_path)

    assert budget_user_id(ALICE, config, paths) == ALICE
    assert budget_user_id(ALICE_BRIDGE, config, paths) == ALICE
    assert budget_user_id("@bridgebot:example.test", config, paths) is None


def test_bridge_bot_requesters_are_never_budgeted(tmp_path: Path) -> None:
    config = _config(monthly_limit_usd=0).model_copy(update={"bot_accounts": ["@bridgebot:example.test"]})

    assert budget_model(config, _paths(tmp_path), None, "@bridgebot:example.test", "astra") == "astra"


@pytest.mark.asyncio
async def test_rescans_wait_for_the_minimum_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = _Scans(monkeypatch, {})
    started: list[float] = []
    collect = monitor_module.collect_monthly_spend

    def timed(*args: object) -> SpendSnapshot:
        started.append(time.monotonic())
        return collect(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(monitor_module, "collect_monthly_spend", timed)
    current = [_config()]
    monitor = BudgetMonitor(
        runtime_paths=_paths(tmp_path),
        config_provider=lambda: current[0],
        clock=_Clock(),
        min_scan_interval_seconds=0.3,
    )
    monitor.sync()
    await _until(lambda: scans.calls == 1)
    monitor.response_finished()
    await _until(lambda: scans.calls == 2)

    # Scans start in a worker thread after a dispatch delay that varies, so allow some slack below the interval.
    assert started[1] - started[0] >= 0.2
    await monitor.stop()


@pytest.mark.asyncio
async def test_slow_scans_are_spaced_by_their_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan holds the GIL the event loop needs, so a slow scan must not restart right after it ends."""
    scans = _Scans(monkeypatch, {})
    started: list[float] = []
    collect = monitor_module.collect_monthly_spend

    def slow(*args: object) -> SpendSnapshot:
        started.append(time.monotonic())
        time.sleep(0.1)
        return collect(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(monitor_module, "collect_monthly_spend", slow)
    monitor = BudgetMonitor(
        runtime_paths=_paths(tmp_path),
        config_provider=_config,
        clock=_Clock(),
        min_scan_interval_seconds=0,
        scan_spacing_factor=5,
    )
    monitor.sync()
    await _until(lambda: scans.calls == 1)
    monitor.response_finished()
    await _until(lambda: scans.calls == 2)

    # Five 0.1 s scan durations separate the two starts; allow slack for worker dispatch.
    assert started[1] - started[0] >= 0.4
    await monitor.stop()


@pytest.mark.asyncio
async def test_idle_tick_rescans_usage_no_reply_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice calls and helpers add spend without a finished reply, so the tick rescans on its own."""
    scans = _Scans(monkeypatch, {ALICE: 12.0})
    clock = _Clock()
    monitor = BudgetMonitor(
        runtime_paths=_paths(tmp_path),
        config_provider=_config,
        clock=clock,
        min_scan_interval_seconds=0,
        tick_seconds=0.05,
    )
    monitor.sync()
    await _until(lambda: scans.calls >= 2)

    clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    await _until(
        lambda: (snapshot := monitor.status().snapshot) is not None and snapshot.period_start == date(2026, 11, 1),
    )
    await monitor.stop()


@pytest.mark.asyncio
async def test_status_lists_only_people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replies for bridge bots are never swapped, so the budget list leaves them out."""
    config = _config().model_copy(update={"bot_accounts": ["@bridgebot:example.test"]})
    monitor, _scans, _current = await _started(
        tmp_path,
        monkeypatch,
        {"@bridgebot:example.test": 50.0, BOB: 3.0},
        config,
    )

    assert [user.user_id for user in monitor.status().users] == [BOB]
    await monitor.stop()


@pytest.mark.asyncio
async def test_lower_spend_applies_even_while_some_stores_are_unread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents nobody has used yet have no store, so a price cut must still lower spend."""
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0})
    scans.spend, scans.unavailable = {ALICE: 3.0}, 1

    monitor.response_finished()
    await _until(lambda: monitor._spend_usd(ALICE) == 3.0)
    await monitor.stop()


@pytest.mark.parametrize(("priced", "unpriced"), [("google", "gemini"), ("codex", "openai_codex")])
def test_provider_aliases_of_a_priced_model_are_swapped_too(tmp_path: Path, priced: str, unpriced: str) -> None:
    config = _config(monthly_limit_usd=0)
    config.models["priced"] = ModelConfig(provider=priced, id="shared-model", pricing=ModelPricing(input=1, output=2))
    config.models["alias"] = ModelConfig(provider=unpriced, id="shared-model")

    assert budget_model(config, _paths(tmp_path), None, ALICE, "alias") == "luna"


@pytest.mark.asyncio
async def test_an_incomplete_price_table_is_rebuilt_on_the_next_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A priced model that failed to build once must not stay unpriced until the next reload."""
    scans = _Scans(monkeypatch, {})
    builds: list[bool] = []

    def table(_config: Config, _paths: RuntimePaths) -> _PriceTable:
        complete = len(builds) > 0
        builds.append(complete)
        return _PriceTable(prices={}, complete=complete)

    monkeypatch.setattr(pricing_module, "_price_table", table)
    monitor, _current = _monitor(tmp_path, _config())
    monitor.sync()
    await _until(lambda: scans.calls == 1)
    for expected_scans in (2, 3):
        monitor.response_finished()
        await _until(lambda expected=expected_scans: scans.calls == expected)

    assert builds == [False, True]
    await monitor.stop()


def test_checking_the_budget_quietly_logs_no_fallback(tmp_path: Path) -> None:
    config = _config(monthly_limit_usd=0)
    paths = _paths(tmp_path)

    with capture_logs() as logs:
        assert budget_model(config, paths, None, ALICE, "astra", log_fallback=False) == "luna"
        assert budget_model(config, paths, None, ALICE, "astra") == "luna"

    assert [entry["event"] for entry in logs if entry["event"] == "budget_fallback_applied"] == [
        "budget_fallback_applied",
    ]
