"""Budget monitor: cached month-to-date spend and fallback model decisions."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from mindroom.budgets import monitor as monitor_module
from mindroom.budgets.caps import BudgetCaps, UserCap, budget_caps_path
from mindroom.budgets.monitor import BudgetMonitor, _budget_limit_usd, _BudgetUserStatus, budget_model, budget_user_id
from mindroom.budgets.pricing import model_prices, run_cost_usd
from mindroom.budgets.spend import SpendSnapshot, UnpricedModelUsage
from mindroom.budgets.spend_store import load_spend, spend_store_path
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_runtime_paths
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import ModelMetrics, RunMetrics, TokenCounts
from mindroom.providers.registry import build_model_provider
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.budget_helpers import set_month_spend
from tests.conftest import persist_session
from tests.identity_helpers import fixture_entity_matrix_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

ALICE = "@alice:example.test"
ALICE_BRIDGE = "@telegram-alice:example.test"
BOB = "@bob:example.test"
CAROL = "@carol:example.test"
BRIDGE_BOT = "@bridgebot:example.test"
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


def _snapshot(
    spend: Mapping[str, float],
    *,
    start: date = date(2026, 10, 1),
    unavailable: int = 0,
    generated_at: datetime = OCTOBER,
) -> SpendSnapshot:
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return SpendSnapshot(
        period_start=start,
        period_end=end,
        generated_at=generated_at,
        spend_usd=dict(spend),
        unpriced_models=(UnpricedModelUsage(provider="Ollama", model="qwen3.8:27b", total_tokens=5),),
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
        monkeypatch.setattr(monitor_module, "collect_monthly_spend", self._collect)

    def _collect(self, _config: Config, _paths: RuntimePaths, now: datetime, _prices: object) -> SpendSnapshot:
        self.calls += 1
        self.release.wait(5)
        if self.error is not None:
            raise self.error
        start = now.date().replace(day=1)
        return _snapshot(self.spend, start=start, unavailable=self.unavailable, generated_at=now)


def _monitor(
    tmp_path: Path,
    config: Config,
    clock: _Clock | None = None,
    **timing: float,
) -> tuple[BudgetMonitor, list[Config]]:
    current = [config]
    monitor = BudgetMonitor(
        runtime_paths=_paths(tmp_path),
        config_provider=lambda: current[0],
        clock=clock or _Clock(),
        min_scan_interval_seconds=0,
        **timing,
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
    await monitor.sync(current[0])
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
    await monitor.sync(_config())
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
    await monitor.sync(_config())
    await _until(lambda: scans.calls == 1)

    for _ in range(5):
        monitor.response_finished(ALICE, None, OCTOBER)
    scans.release.set()
    await _until(lambda: scans.calls == 2)
    await _quiet()

    assert scans.calls == 2
    await monitor.stop()


@pytest.mark.asyncio
async def test_scan_failure_keeps_previous_snapshot_and_reply_costs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0})
    scans.error = RuntimeError("database locked")
    monitor.response_finished(ALICE, 1.0, OCTOBER)

    monitor.response_finished(BOB, None, OCTOBER)
    await _until(lambda: scans.calls == 2)
    await _quiet()
    monitor.response_finished(ALICE, 0.5, OCTOBER)

    assert monitor._spend_usd(ALICE) == 13.5
    await monitor.stop()


@pytest.mark.asyncio
async def test_disabled_budgets_never_scan_or_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monitor, scans, current = await _started(tmp_path, monkeypatch, {ALICE: 12.0})
    current[0] = current[0].model_copy(update={"budgets": None})

    await monitor.sync(current[0])
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
    await monitor.sync(_config())
    await _until(lambda: scans.calls == 1)
    monitor.response_finished(ALICE, None, OCTOBER)
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
    await monitor.sync(_config())
    await _until(lambda: scans.calls == 1)
    monitor.response_finished(ALICE, None, OCTOBER)
    await _until(lambda: scans.calls == 2)

    # Five 0.1 s scan durations separate the two starts; allow slack for worker dispatch.
    assert started[1] - started[0] >= 0.4
    await monitor.stop()


@pytest.mark.asyncio
async def test_reconciliation_interval_schedules_scans_and_finished_replies_do_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voice calls and helpers add spend no reply reports, so only the slow reconciliation rescans for them."""
    scans = _Scans(monkeypatch, {ALICE: 1.0})
    replies_only, _current = _monitor(tmp_path, _config(), tick_seconds=0.01)
    await replies_only.sync(_config())
    await _until(lambda: scans.calls == 1)

    for _ in range(5):
        replies_only.response_finished(ALICE, 1.0, OCTOBER)
    await _quiet()

    assert scans.calls == 1
    assert replies_only._spend_usd(ALICE) == 6.0
    await replies_only.stop()

    reconciling, _current = _monitor(tmp_path, _config(), tick_seconds=0.01, reconcile_seconds=0.05)
    await reconciling.sync(_config())
    await _until(lambda: scans.calls >= 4)
    await reconciling.stop()


@pytest.mark.asyncio
async def test_a_new_month_drops_last_months_spend_and_scans_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    scans = _Scans(monkeypatch, {ALICE: 9.0})
    monitor, _current = _monitor(tmp_path, _config(), clock, tick_seconds=0.01)
    await monitor.sync(_config())
    await _until(lambda: monitor.status().snapshot is not None)
    monitor.response_finished(ALICE, 2.0, OCTOBER)
    assert _decide(monitor, ALICE, "astra") == "luna"
    scans.release.clear()
    scans.spend = {ALICE: 0.5}

    clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)

    assert monitor._spend_usd(ALICE) == 0.0
    assert _decide(monitor, ALICE, "astra") == "astra"
    await _until(lambda: scans.calls == 2)
    scans.release.set()
    await _until(
        lambda: (snapshot := monitor.status().snapshot) is not None and snapshot.period_start == date(2026, 11, 1),
    )
    assert monitor._spend_usd(ALICE) == 0.5
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
async def test_a_reply_that_started_last_month_adds_nothing_and_requests_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan dates usage by request, so the part of a reply spent before the month began belongs to the month before."""
    clock = _Clock()
    clock.now = datetime(2026, 11, 1, 0, 30, tzinfo=UTC)
    scans = _Scans(monkeypatch, {ALICE: 1.0})
    monitor, _current = _monitor(tmp_path, _config(), clock)
    await monitor.sync(_config())
    await _until(lambda: monitor.status().snapshot is not None)

    monitor.response_finished(ALICE, 6.0, datetime(2026, 10, 31, 23, 50, tzinfo=UTC))

    assert monitor._spend_usd(ALICE) == 1.0
    await _until(lambda: scans.calls == 2)
    await monitor.stop()


@pytest.mark.asyncio
async def test_lower_spend_applies_even_while_some_stores_are_unread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents nobody has used yet have no store, so a price cut must still lower spend."""
    monitor, scans, current = await _started(tmp_path, monkeypatch, {ALICE: 12.0})
    scans.spend, scans.unavailable = {ALICE: 3.0}, 1
    current[0] = _config()
    current[0].models["astra"].pricing = ModelPricing(input=1, output=6)

    await monitor.sync(current[0])
    await _until(lambda: monitor._spend_usd(ALICE) == 3.0)
    await monitor.stop()


@pytest.mark.parametrize(("priced", "unpriced"), [("google", "gemini"), ("codex", "openai_codex")])
def test_provider_aliases_of_a_priced_model_are_swapped_too(tmp_path: Path, priced: str, unpriced: str) -> None:
    config = _config(monthly_limit_usd=0)
    config.models["priced"] = ModelConfig(provider=priced, id="shared-model", pricing=ModelPricing(input=1, output=2))
    config.models["alias"] = ModelConfig(provider=unpriced, id="shared-model")

    assert budget_model(config, _paths(tmp_path), None, ALICE, "alias") == "luna"


@pytest.mark.parametrize(
    ("priced", "alias"),
    [
        ({"provider": "codex", "id": "openai-codex/gpt-6.1-sol"}, {"provider": "codex", "id": "gpt-6.1-sol"}),
        ({"provider": "kimi", "id": "k3"}, {"provider": "kimi_code", "id": "kimi-code/k3"}),
        (
            {"provider": "openai", "id": "gpt-6-astra", "api": "responses"},
            {"provider": "openai", "id": "gpt-6-astra", "api": "chat_completions"},
        ),
    ],
)
def test_an_entry_recorded_as_a_priced_model_is_swapped_too(
    tmp_path: Path,
    priced: dict[str, Any],
    alias: dict[str, Any],
) -> None:
    """An entry that differs from a priced one only in ID prefix or API transport records the same model's usage."""
    config = _config(monthly_limit_usd=0)
    config.models["priced"] = ModelConfig(**priced, pricing=ModelPricing(input=1, output=2))
    config.models["alias"] = ModelConfig(**alias)

    assert budget_model(config, _paths(tmp_path), None, ALICE, "alias") == "luna"


def test_checking_the_budget_quietly_logs_no_fallback(tmp_path: Path) -> None:
    config = _config(monthly_limit_usd=0)
    paths = _paths(tmp_path)

    with capture_logs() as logs:
        assert budget_model(config, paths, None, ALICE, "astra", log_fallback=False) == "luna"
        assert budget_model(config, paths, None, ALICE, "astra") == "luna"

    assert [entry["event"] for entry in logs if entry["event"] == "budget_fallback_applied"] == [
        "budget_fallback_applied",
    ]


@pytest.mark.asyncio
async def test_a_finished_replys_cost_can_apply_the_fallback_without_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 9.0})
    assert _decide(monitor, ALICE, "astra") == "astra"

    monitor.response_finished(ALICE, 1.5, OCTOBER)
    await _quiet()

    assert monitor._spend_usd(ALICE) == 10.5
    assert _decide(monitor, ALICE, "astra") == "luna"
    assert scans.calls == 1
    await monitor.stop()


@pytest.mark.asyncio
async def test_a_rescan_replaces_the_costs_recorded_before_it_started_and_keeps_later_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 5.0})
    monitor.response_finished(ALICE, 2.0, OCTOBER)
    scans.release.clear()
    scans.spend = {ALICE: 6.5}

    monitor.response_finished(BOB, None, OCTOBER)
    await _until(lambda: scans.calls == 2)
    monitor.response_finished(ALICE, 1.0, OCTOBER)

    # Until the scan completes, the last scan and both replies count.
    assert monitor._spend_usd(ALICE) == 8.0
    scans.release.set()
    await _until(lambda: monitor._spend_usd(ALICE) == 7.5)
    await monitor.stop()


@pytest.mark.asyncio
async def test_a_reply_counts_for_the_person_a_scan_charges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An alias counts for its canonical user, and a reply requested by an agent or a bot counts for nobody."""
    config = _config().model_copy(update={"bot_accounts": [BRIDGE_BOT]})
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {}, config)
    paths = monitor.runtime_paths
    router = fixture_entity_matrix_id(ROUTER_AGENT_NAME, config.get_domain(paths), paths).full_id
    assert budget_user_id(router, config, paths) is None

    monitor.response_finished(ALICE_BRIDGE, 2.0, OCTOBER)
    for requester_id in (BRIDGE_BOT, router, None):
        monitor.response_finished(requester_id, 4.0, OCTOBER)
        # Nobody is charged for such a reply, so not knowing its cost needs no scan.
        monitor.response_finished(requester_id, None, OCTOBER)
    await _quiet()

    assert scans.calls == 1
    assert monitor._spend_usd(ALICE) == 2.0
    assert monitor._spend_usd(BRIDGE_BOT) == 0.0
    assert monitor._spend_usd(router) == 0.0
    assert [(user.user_id, user.spend_usd) for user in monitor.status().users] == [(ALICE, 2.0)]
    await monitor.stop()


@pytest.mark.asyncio
async def test_a_completed_unpriced_reply_neither_adds_nor_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 3.0})

    monitor.response_finished(ALICE, 0.0, OCTOBER)
    monitor.response_finished(BOB, 0.0, OCTOBER)
    await _quiet()

    assert scans.calls == 1
    assert [(user.user_id, user.spend_usd) for user in monitor.status().users] == [(ALICE, 3.0)]
    await monitor.stop()


@pytest.mark.asyncio
async def test_a_stopped_reply_adds_nothing_and_requests_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped, failed, or resumed reply reports no exact cost, but its run's usage is stored, so a scan counts it."""
    monitor, scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 5.0})
    scans.release.clear()
    scans.spend = {ALICE: 7.0}

    monitor.response_finished(ALICE, None, OCTOBER)

    assert monitor._spend_usd(ALICE) == 5.0
    await _until(lambda: scans.calls == 2)
    scans.release.set()
    await _until(lambda: monitor._spend_usd(ALICE) == 7.0)
    await monitor.stop()


@pytest.mark.asyncio
async def test_replies_before_the_first_scan_count_nothing_until_it_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first scan covers replies that finished before it started, so they are not added on top of it."""
    scans = _Scans(monkeypatch, {ALICE: 3.0})
    monitor, _current = _monitor(tmp_path, _config())
    monitor.response_finished(ALICE, 20.0, OCTOBER)

    assert monitor._spend_usd(ALICE) == 0.0
    assert _decide(monitor, ALICE, "astra") == "astra"
    await monitor.sync(_config())
    await _until(lambda: scans.calls == 1 and monitor.status().snapshot is not None)
    assert monitor._spend_usd(ALICE) == 3.0
    await monitor.stop()


@pytest.mark.asyncio
async def test_status_shows_the_latest_scan_plus_finished_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, _scans, _current = await _started(tmp_path, monkeypatch, {ALICE: 12.0, BOB: 3.0})

    monitor.response_finished(BOB, 8.0, OCTOBER)
    monitor.response_finished(CAROL, 1.5, OCTOBER)
    status = monitor.status()

    assert [(user.user_id, user.spend_usd, user.over_budget) for user in status.users] == [
        (ALICE, 12.0, True),
        (BOB, 11.0, True),
        (CAROL, 1.5, False),
    ]
    assert status.to_dict()["generated_at"] == OCTOBER.isoformat()
    await monitor.stop()


@pytest.mark.asyncio
async def test_a_rescan_keeps_the_total_a_finished_reply_added(tmp_path: Path) -> None:
    """A reply's own cost and the scan of its stored run charge the same person the same amount."""
    config = _config()
    config.agents["code"] = AgentConfig(display_name="Code")
    paths = _paths(tmp_path)
    monitor = BudgetMonitor(
        runtime_paths=paths,
        config_provider=lambda: config,
        clock=_Clock(),
        min_scan_interval_seconds=0,
    )
    await monitor.sync(config)
    await _until(lambda: monitor.status().snapshot is not None)
    model = build_model_provider(config, paths, "astra").identity
    tokens = TokenCounts(input_tokens=1_000_000, output_tokens=100_000, total_tokens=1_100_000)
    metrics = RunMetrics(
        tokens=tokens,
        details={"model": [ModelMetrics(tokens=tokens, id=model.id, provider=model.provider)]},
    )
    persist_session(
        create_agent_state_storage("code", paths.storage_root / "agents" / "code"),
        ConversationSession(
            kind="agent",
            session_id="session-1",
            owner_id="code",
            runs=[
                RunRecord(
                    run_id="run-1",
                    user_id=ALICE_BRIDGE,
                    model_provider=model.provider,
                    model=model.id,
                    created_at=int(datetime(2026, 10, 3, tzinfo=UTC).timestamp()),
                    metrics=metrics,
                ),
            ],
        ),
    )
    cost = run_cost_usd(metrics, model_provider=model.provider, model_id=model.id, prices=model_prices(config))
    monitor.response_finished(ALICE_BRIDGE, cost, OCTOBER)
    assert monitor._spend_usd(ALICE) == pytest.approx(8.0)
    first_scan = monitor.status().snapshot

    monitor.response_finished(ALICE, None, OCTOBER)
    await _until(lambda: monitor.status().snapshot is not first_scan)

    assert monitor._spend_usd(ALICE) == pytest.approx(8.0)
    await monitor.stop()


async def _restarted(
    tmp_path: Path,
    config: Config,
    clock: _Clock,
    **timing: float,
) -> tuple[BudgetMonitor, list[Config]]:
    """Return a fresh monitor over the storage an earlier, stopped monitor used, synced to ``config``."""
    monitor, current = _monitor(tmp_path, config, clock, **timing)
    await monitor.sync(config)
    return monitor, current


@pytest.mark.asyncio
async def test_a_restart_within_the_reconciliation_interval_resumes_the_stored_spend_without_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    scans = _Scans(monkeypatch, {ALICE: 9.0, BOB: 2.0})
    scans.unavailable = 1
    before, _current = _monitor(tmp_path, _config(), clock)
    await before.sync(_config())
    await _until(lambda: before.status().snapshot is not None)
    before.response_finished(ALICE, 1.5, OCTOBER)
    before.response_finished(CAROL, 0.25, OCTOBER)
    status = before.status()
    spend = {user_id: before._spend_usd(user_id) for user_id in (ALICE, BOB, CAROL)}
    await before.stop()

    clock.now = OCTOBER + timedelta(hours=5)
    restarted, _current = _monitor(tmp_path, _config(), clock, tick_seconds=0.01, reconcile_seconds=5 * 3600 + 1)

    # The stored spend applies before the first sync, so caps hold from the first reply after a restart.
    assert {user_id: restarted._spend_usd(user_id) for user_id in (ALICE, BOB, CAROL)} == spend
    assert _decide(restarted, ALICE, "astra") == "luna"
    assert restarted.status() == status
    await restarted.sync(_config())
    await _quiet()
    assert scans.calls == 1
    # The next reconciliation is due an interval after the stored scan started, not after the restart.
    await _until(lambda: scans.calls == 2)
    await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stale", "last_month", "repriced", "requested", "budgets_were_off"])
async def test_a_restart_scans_when_the_stored_scan_cannot_stand_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    clock = _Clock()
    scans = _Scans(monkeypatch, {ALICE: 9.0})
    before, current = _monitor(tmp_path, _config(), clock)
    await before.sync(_config())
    await _until(lambda: before.status().snapshot is not None)
    config = _config()
    if reason == "stale":
        clock.now = OCTOBER + timedelta(hours=7)
    elif reason == "last_month":
        clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    elif reason == "repriced":
        config.models["astra"].pricing = ModelPricing(input=1, output=6)
    elif reason == "requested":
        # The scan the request starts fails, so the request is still unserved when the runtime stops.
        scans.error = RuntimeError("database locked")
        before.response_finished(ALICE, None, OCTOBER)
        await _until(lambda: scans.calls == 2)
        await _quiet()
        scans.error = None
    else:
        current[0] = _config().model_copy(update={"budgets": None})
        await before.sync(current[0])
    calls = scans.calls
    await before.stop()

    restarted, _current = await _restarted(tmp_path, config, clock)

    await _until(lambda: scans.calls == calls + 1)
    await restarted.stop()


@pytest.mark.asyncio
async def test_a_config_reload_rescans_only_when_budgets_aliases_or_prices_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor, scans, current = await _started(tmp_path, monkeypatch, {ALICE: 9.0})

    unrelated = _config()
    unrelated.agents["code"] = AgentConfig(display_name="Code")
    for config in (_config(), unrelated):
        await monitor.sync(config)
    await _quiet()
    assert scans.calls == 1

    repriced = _config()
    repriced.models["luna"].pricing = ModelPricing(input=0.1, output=1)
    realiased = repriced.model_copy(update={"authorization": AuthorizationConfig(aliases={BOB: [ALICE_BRIDGE]})})
    recapped = realiased.model_copy(update={"budgets": _config(monthly_limit_usd=20).budgets})
    for calls, config in enumerate((repriced, realiased, recapped), start=2):
        current[0] = config
        await monitor.sync(config)
        await _until(lambda calls=calls: scans.calls == calls)

    # Replies add nothing while budgets are off, so turning them back on recounts.
    current[0] = recapped.model_copy(update={"budgets": None})
    await monitor.sync(current[0])
    await _quiet()
    current[0] = recapped
    await monitor.sync(recapped)
    await _until(lambda: scans.calls == 5)
    await _quiet()

    assert scans.calls == 5
    await monitor.stop()


@pytest.mark.asyncio
async def test_a_completed_scan_drops_the_stored_reply_costs_it_covers_and_keeps_later_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    scans = _Scans(monkeypatch, {ALICE: 5.0})
    before, _current = _monitor(tmp_path, _config(), clock)
    await before.sync(_config())
    await _until(lambda: before.status().snapshot is not None)
    clock.now += timedelta(minutes=1)
    before.response_finished(ALICE, 2.0, OCTOBER)
    scans.release.clear()
    scans.spend = {ALICE: 6.5}
    clock.now += timedelta(minutes=1)
    before.response_finished(BOB, None, OCTOBER)
    await _until(lambda: scans.calls == 2)
    clock.now += timedelta(minutes=1)
    before.response_finished(ALICE, 1.0, OCTOBER)
    scans.release.set()
    await _until(lambda: before._spend_usd(ALICE) == 7.5)
    await before.stop()

    stored = load_spend(spend_store_path(_paths(tmp_path)))
    assert stored.reply_costs == {ALICE: 1.0}
    assert stored.scan_requested is False
    restarted, _current = await _restarted(tmp_path, _config(), clock)
    await _quiet()

    assert restarted._spend_usd(ALICE) == 7.5
    assert scans.calls == 2
    await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["missing", "corrupt", "other_schema"])
async def test_an_unusable_spend_store_falls_back_to_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
) -> None:
    path = spend_store_path(_paths(tmp_path))
    path.parent.mkdir(parents=True)
    if store == "corrupt":
        path.write_bytes(b"not a database" * 100)
    elif store == "other_schema":
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 7")
    scans = _Scans(monkeypatch, {ALICE: 12.0})

    with capture_logs() as logs:
        monitor, _current = _monitor(tmp_path, _config())
    await monitor.sync(_config())
    await _until(lambda: monitor._spend_usd(ALICE) == 12.0)

    assert scans.calls == 1
    assert ("budget_spend_store_unreadable" in [entry["event"] for entry in logs]) is (store != "missing")
    stored = load_spend(path)
    assert stored.snapshot is not None
    assert stored.snapshot.spend_usd == {ALICE: 12.0}
    await monitor.stop()
