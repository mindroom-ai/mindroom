"""Budget monitor: cached month-to-date spend and fallback model decisions."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.budgets import monitor as monitor_module
from mindroom.budgets.monitor import BudgetMonitor, _budget_limit_usd, _BudgetUserStatus, budget_model
from mindroom.budgets.spend import SpendSnapshot, _UnpricedModelUsage
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import RuntimePaths, resolve_runtime_paths

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


def _snapshot(spend: Mapping[str, float], *, start: date = date(2026, 10, 1)) -> SpendSnapshot:
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return SpendSnapshot(
        period_start=start,
        period_end=end,
        generated_at=OCTOBER,
        spend_usd=dict(spend),
        unpriced_models=(_UnpricedModelUsage(provider="Ollama", model="qwen3.8:27b", total_tokens=5),),
        scanned_sources=2,
        unavailable_sources=0,
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
        monkeypatch.setattr(monitor_module, "price_table", lambda _config, _paths: {})
        monkeypatch.setattr(monitor_module, "collect_monthly_spend", self._collect)

    def _collect(self, _config: Config, _paths: RuntimePaths, now: datetime, _prices: object) -> SpendSnapshot:
        self.calls += 1
        self.release.wait(5)
        if self.error is not None:
            raise self.error
        start = now.date().replace(day=1)
        return _snapshot(self.spend, start=start)


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
async def test_per_user_override_beats_the_default_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(users={ALICE_BRIDGE: 100.0, BOB: 1.0})
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0, BOB: 3.0}, config)

    assert _decide(monitor, ALICE, "astra") == "astra"
    assert _decide(monitor, BOB, "astra") == "luna"
    await monitor.stop()


def test_budget_limit_resolves_aliases_and_defaults(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config = _config(users={ALICE_BRIDGE: 100.0})

    assert _budget_limit_usd(config, ALICE, paths) == 100.0
    assert _budget_limit_usd(config, BOB, paths) == 10.0
    assert _budget_limit_usd(_config(monthly_limit_usd=None), BOB, paths) is None
    assert _budget_limit_usd(Config(), BOB, paths) is None


def test_no_snapshot_yet_means_zero_spend(tmp_path: Path) -> None:
    monitor, _current = _monitor(tmp_path, _config())

    assert monitor._spend_usd(ALICE) == 0.0
    assert _decide(monitor, ALICE, "astra") == "astra"


def test_without_a_monitor_only_zero_caps_apply(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert budget_model(_config(), paths, None, ALICE, "astra") == "astra"
    assert budget_model(_config(monthly_limit_usd=0), paths, None, ALICE, "astra") == "luna"


def test_disabled_budgets_never_consult_the_monitor(tmp_path: Path) -> None:
    class _Untouchable:
        def _spend_usd(self, _user_id: str) -> float:
            raise AssertionError

    config = _config().model_copy(update={"budgets": None})

    assert budget_model(config, _paths(tmp_path), _Untouchable(), ALICE, "astra") == "astra"  # type: ignore[arg-type]


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
async def test_status_lists_spenders_and_configured_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(users={"@carol:example.test": 50.0})
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0, BOB: 3.0}, config)

    status = monitor.status()

    assert status.users == (
        _BudgetUserStatus(user_id=ALICE, spend_usd=12.0, limit_usd=10.0, over_budget=True),
        _BudgetUserStatus(user_id=BOB, spend_usd=3.0, limit_usd=10.0, over_budget=False),
        _BudgetUserStatus(user_id="@carol:example.test", spend_usd=0.0, limit_usd=50.0, over_budget=False),
    )
    payload = status.to_dict()
    assert payload["enabled"] is True
    assert payload["period_start"] == "2026-10-01"
    assert payload["period_end"] == "2026-11-01"
    assert payload["generated_at"] == "2026-10-09T12:00:00+00:00"
    assert payload["default_limit_usd"] == 10.0
    assert payload["fallback_model"] == "luna"
    assert payload["users"][0] == {"user_id": ALICE, "spend_usd": 12.0, "limit_usd": 10.0, "over_budget": True}
    assert payload["unpriced_models"] == [{"provider": "Ollama", "model": "qwen3.8:27b", "total_tokens": 5}]
    assert payload["coverage"] == {"scanned_sources": 2, "unavailable_sources": 0}
    await monitor.stop()


def test_status_before_first_scan_reports_current_month_without_spend(tmp_path: Path) -> None:
    monitor, _current = _monitor(tmp_path, _config())

    payload = monitor.status().to_dict()

    assert payload["generated_at"] is None
    assert payload["period_start"] == "2026-10-01"
    assert payload["users"] == []
