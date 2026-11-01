"""Monthly per-user spend derived from retained usage."""
# ruff: noqa: D103

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest

from mindroom.budgets.pricing import PricedModel, cost_usd, model_prices
from mindroom.budgets.spend import SpendSnapshot, UnpricedModelUsage, collect_monthly_spend, month_bounds
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics, TokenCounts
from mindroom.matrix_rtc.call_tools import record_call_token_usage
from mindroom.matrix_rtc.voice_agent import RealtimeCallUsage, _RealtimeResponseUsage
from mindroom.providers.registry import build_model_provider
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage.stats import collect_admin_usage
from mindroom.usage.stats_storage import (
    UsageModelMetrics,
    UsageRequestMetrics,
    UsageRunNode,
    UsageSessionRow,
    UsageStorageDiagnostic,
    UsageStorageSource,
)
from mindroom.usage.storage import SYSTEM_USAGE_ENTITY
from tests.conftest import persist_runs, persist_session, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

ALICE = "@alice:example.test"
BOB = "@bob:example.test"
ALICE_BRIDGE = "@bridge-alice:example.test"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def _config() -> Config:
    return Config(
        agents={"code": AgentConfig(display_name="Code")},
        teams={"engineering": TeamConfig(display_name="Engineering", role="Team", agents=["code"])},
        models={
            "astra": ModelConfig(
                provider="openai",
                id="gpt-6-astra",
                api_key="key",
                pricing=ModelPricing(input=5, output=30),
            ),
            "luna": ModelConfig(
                provider="openai",
                id="gpt-6-luna",
                api_key="key",
                pricing=ModelPricing(input=0.2, output=1.25),
            ),
            "local": ModelConfig(provider="ollama", id="qwen3.8:27b"),
        },
        authorization=AuthorizationConfig(aliases={ALICE: [ALICE_BRIDGE]}),
        budgets={"fallback_model": "luna", "monthly_limit_usd": 10},
    )


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _source(scope: str = "shared_agent") -> UsageStorageSource:
    agent = None if scope == "team" else "code"
    return UsageStorageSource(
        path=Path("/not-read.db"),
        path_label=f"{scope}.db",
        scope=scope,  # type: ignore[arg-type]
        source_agent_id=agent,
        allowed_agent_ids=frozenset({"code"}),
        requester_isolated=False,
    )


def _provider(config: Config, tmp_path: Path, model_name: str) -> str:
    return build_model_provider(config, _paths(tmp_path), model_name).identity.provider


def _run(
    run_id: str,
    *,
    provider: str,
    model: str,
    created_at: datetime,
    requester_id: str | None = ALICE,
    input_tokens: int = 0,
    output_tokens: int = 0,
    parent_run_id: str | None = None,
    kind: str = "run",
) -> UsageRunNode:
    return UsageRunNode(
        team_id=None,
        requester_id=requester_id,
        run_id=run_id,
        model_provider=provider,
        model=model,
        metrics=MappingProxyType(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        ),
        created_at=created_at.timestamp(),
        parent_run_id=parent_run_id,
        kind=kind,  # type: ignore[arg-type]
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[UsageStorageSource, tuple[UsageRunNode, ...]],
) -> list[float | None]:
    seen_since: list[float | None] = []
    monkeypatch.setattr("mindroom.usage.stats.discover_admin_usage_sources", lambda **_: tuple(rows))

    def iter_rows(
        source: UsageStorageSource,
        *,
        mode: str = "runs",
        since: float | None = None,
    ) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
        del mode
        seen_since.append(since)
        is_team = source.scope == "team"
        yield UsageSessionRow(
            source=source,
            entity_id="engineering" if is_team else "code",
            entity_kind="team" if is_team else "agent",
            row_key=f"{source.path_label}-session",
            runs=rows[source],
        )

    monkeypatch.setattr("mindroom.usage.stats.iter_usage_storage_rows", iter_rows)
    return seen_since


def _spend(config: Config, tmp_path: Path) -> object:
    paths = _paths(tmp_path)
    return collect_monthly_spend(config, paths, NOW, model_prices(config))


def test_month_bounds_are_utc_calendar_months() -> None:
    assert month_bounds(NOW) == (date(2026, 10, 1), date(2026, 11, 1))
    assert month_bounds(datetime(2026, 12, 31, 23, tzinfo=UTC)) == (date(2026, 12, 1), date(2027, 1, 1))


def test_spend_prices_each_users_current_month_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    openai = _provider(config, tmp_path, "astra")
    seen_since = _wire(
        monkeypatch,
        {
            _source(): (
                _run(
                    "a1",
                    provider=openai,
                    model="gpt-6-astra",
                    created_at=datetime(2026, 10, 2, tzinfo=UTC),
                    input_tokens=1_000_000,
                ),
                _run(
                    "a0",
                    provider=openai,
                    model="gpt-6-astra",
                    created_at=datetime(2026, 9, 30, 23, tzinfo=UTC),
                    input_tokens=1_000_000,
                ),
                _run(
                    "b1",
                    provider=openai,
                    model="gpt-6-luna",
                    created_at=datetime(2026, 10, 3, tzinfo=UTC),
                    requester_id=BOB,
                    output_tokens=1_000_000,
                ),
            ),
        },
    )

    snapshot = _spend(config, tmp_path)

    assert snapshot.spend_usd == {ALICE: pytest.approx(5.0), BOB: pytest.approx(1.25)}
    assert (snapshot.period_start, snapshot.period_end) == (date(2026, 10, 1), date(2026, 11, 1))
    assert snapshot.unpriced_models == ()
    assert seen_since == [datetime(2026, 9, 30, tzinfo=UTC).timestamp()]


def test_spend_reports_unpriced_models_without_charging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    ollama = _provider(config, tmp_path, "local")
    _wire(
        monkeypatch,
        {
            _source(): (
                _run("l1", provider=ollama, model="qwen3.8:27b", created_at=NOW, input_tokens=700, output_tokens=300),
            ),
        },
    )

    snapshot = _spend(config, tmp_path)

    assert snapshot.spend_usd == {}
    assert snapshot.unpriced_models == (UnpricedModelUsage(provider=ollama, model="qwen3.8:27b", total_tokens=1000),)


def test_spend_ignores_unattributed_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    openai = _provider(config, tmp_path, "astra")
    _wire(
        monkeypatch,
        {
            _source(): (
                _run("x", provider=openai, model="gpt-6-astra", created_at=NOW, requester_id=None, input_tokens=10),
            ),
        },
    )

    assert _spend(config, tmp_path).spend_usd == {}


def test_spend_charges_team_members_and_helpers_to_the_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    openai = _provider(config, tmp_path, "astra")
    _wire(
        monkeypatch,
        {
            _source("team"): (
                _run("team-run", provider=openai, model="gpt-6-astra", created_at=NOW, output_tokens=100_000),
                _run(
                    "member-run",
                    provider=openai,
                    model="gpt-6-astra",
                    created_at=NOW,
                    output_tokens=100_000,
                    parent_run_id="team-run",
                ),
            ),
            _source(): (
                _run(
                    "summary",
                    provider=openai,
                    model="gpt-6-luna",
                    created_at=NOW,
                    input_tokens=1_000_000,
                    kind="compaction_summary",
                ),
            ),
        },
    )

    snapshot = _spend(config, tmp_path)

    assert snapshot.spend_usd == {ALICE: pytest.approx(3.0 + 3.0 + 0.2)}


def test_spend_prices_usage_recorded_through_session_storage(tmp_path: Path) -> None:
    """Usage written by real session storage must match the price table's model identities."""
    config = _config()
    paths = _paths(tmp_path)
    model = build_model_provider(config, paths, "astra").identity
    storage = create_agent_state_storage("code", paths.storage_root / "agents" / "code")
    metrics = RunMetrics(
        tokens=TokenCounts(input_tokens=1_000_000, output_tokens=100_000, total_tokens=1_100_000),
        details={
            "model": [
                ModelMetrics(
                    tokens=TokenCounts(input_tokens=1_000_000, output_tokens=100_000, total_tokens=1_100_000),
                    id=model.id,
                    provider=model.provider,
                ),
            ],
        },
    )
    persist_session(
        storage,
        ConversationSession(
            kind="agent",
            session_id="session-1",
            owner_id="code",
            runs=[
                RunRecord(
                    run_id="run-1",
                    user_id=ALICE,
                    model_provider=model.provider,
                    model=model.id,
                    created_at=int(datetime(2026, 10, 3, tzinfo=UTC).timestamp()),
                    metrics=metrics,
                ),
            ],
        ),
    )

    snapshot = collect_monthly_spend(config, paths, NOW, model_prices(config))

    assert snapshot.spend_usd == {ALICE: pytest.approx(5.0 + 3.0)}
    assert snapshot.unpriced_models == ()


def test_spend_charges_a_resumed_run_for_its_requests_this_month(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run paused last month and resumed this month is charged for its requests made this month."""
    config = _config()
    paths = _paths(tmp_path)
    model = build_model_provider(config, paths, "astra").identity
    started = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp())
    resumed = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp())

    def request(created_at: int) -> Message:
        return Message(
            role="assistant",
            created_at=created_at,
            provider_data={"mindroom_model": {"id": model.id, "provider": model.provider}},
            metrics=MessageMetrics(tokens=TokenCounts(input_tokens=1_000_000, total_tokens=1_000_000)),
        )

    def metrics(tokens: int) -> RunMetrics:
        model_metrics = ModelMetrics(
            tokens=TokenCounts(input_tokens=tokens, total_tokens=tokens),
            id=model.id,
            provider=model.provider,
        )
        return RunMetrics(
            tokens=TokenCounts(input_tokens=tokens, total_tokens=tokens),
            details={"model": [model_metrics]},
        )

    run = RunRecord(
        run_id="run-1",
        user_id=ALICE,
        model_provider=model.provider,
        model=model.id,
        created_at=started,
        status=RunStatus.paused,
        messages=[request(started)],
        metrics=metrics(1_000_000),
    )
    storage = create_agent_state_storage("code", paths.storage_root / "agents" / "code")
    with monkeypatch.context() as clock:
        clock.setattr("mindroom.session_storage.store.time", lambda: started)
        session = persist_session(
            storage,
            ConversationSession(kind="agent", session_id="session-1", owner_id="code", runs=[run]),
        )
        run.status = RunStatus.completed
        run.messages = [*run.messages, request(resumed)]
        run.metrics = metrics(2_000_000)
        clock.setattr("mindroom.session_storage.store.time", lambda: resumed)
        persist_runs(storage, session, run)

    snapshot = collect_monthly_spend(config, paths, NOW, model_prices(config))

    assert snapshot.spend_usd == {ALICE: pytest.approx(5.0)}


@pytest.mark.asyncio
async def test_realtime_call_tokens_are_charged_at_the_openai_entry_of_the_speech_model(tmp_path: Path) -> None:
    """A realtime call records its speech model's tokens under the ``openai`` route's name, so that entry prices them."""
    paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={"code": AgentConfig(display_name="Code")},
        models={
            "realtime": ModelConfig(provider="openai", id="gpt-realtime-2.1", pricing=ModelPricing(input=4, output=16)),
        },
    )
    caller = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=ALICE,
        room_id="!room:example.test",
        thread_id=None,
        resolved_thread_id=None,
        session_id="call",
    )
    started = int(NOW.timestamp())
    response = _RealtimeResponseUsage(
        created_at=started,
        tokens=TokenCounts(input_tokens=1_000_000, output_tokens=100_000),
    )
    call = RealtimeCallUsage(usage_id="call-1", model="gpt-realtime-2.1", created_at=started, responses=(response,))
    await record_call_token_usage(call, config=config, runtime_paths=paths, execution_identity=caller)

    snapshot = collect_monthly_spend(config, paths, NOW, model_prices(config))

    assert snapshot.spend_usd == {ALICE: pytest.approx(4.0 + 1.6)}
    assert snapshot.unpriced_models == ()


def _spend_from_admin_report(
    config: Config,
    paths: RuntimePaths,
    now: datetime,
    prices: Mapping[tuple[str, str], PricedModel],
) -> SpendSnapshot:
    """Price the full admin report's per-user daily model rows the way monthly spend does."""
    start, end = month_bounds(now)
    since = datetime.combine(start - timedelta(days=1), time(), UTC).timestamp()
    report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, since=since)
    spend: defaultdict[str, float] = defaultdict(float)
    unpriced: defaultdict[tuple[str, str], int] = defaultdict(int)
    for user in report.user_breakdown:
        for day in user.daily_breakdown or ():
            if not start <= date.fromisoformat(day.date) < end:
                continue
            for row in day.model_breakdown:
                priced = prices.get((row.model_provider, row.model))
                if priced is None:
                    unpriced[(row.model_provider, row.model)] += row.totals.total_tokens
                elif user.user_id is not None:
                    spend[user.user_id] += cost_usd(row.totals, priced)
    coverage = report.daily_coverage
    assert coverage is not None
    return SpendSnapshot(
        period_start=start,
        period_end=end,
        generated_at=now,
        spend_usd=dict(spend),
        unpriced_models=tuple(
            UnpricedModelUsage(provider=provider, model=model, total_tokens=tokens)
            for (provider, model), tokens in sorted(unpriced.items())
        ),
        scanned_sources=coverage.scanned_sources,
        unavailable_sources=coverage.unavailable_sources,
    )


def _counters(input_tokens: int) -> dict[str, int]:
    output_tokens = input_tokens // 4
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}


def _requested_run(
    run_id: str,
    *requests: tuple[datetime, tuple[str, str], int],
    requester_id: str | None = ALICE,
    parent_run_id: str | None = None,
    kind: str = "run",
) -> UsageRunNode:
    """Return a run whose requests, each a time, a (provider, model) and input tokens, add up to its model totals."""
    by_model: dict[tuple[str, str], dict[str, int]] = {}
    for _, model, input_tokens in requests:
        counters = by_model.setdefault(model, {})
        for name, value in _counters(input_tokens).items():
            counters[name] = counters.get(name, 0) + value
    provider, model = requests[0][1]
    return UsageRunNode(
        team_id=None,
        requester_id=requester_id,
        run_id=run_id,
        model_provider=provider,
        model=model,
        metrics=MappingProxyType(
            {name: sum(counters[name] for counters in by_model.values()) for name in _counters(0)},
        ),
        parent_run_id=parent_run_id,
        created_at=requests[0][0].timestamp(),
        model_metrics=tuple(
            UsageModelMetrics(model_provider, model_id, MappingProxyType(counters))
            for (model_provider, model_id), counters in by_model.items()
        ),
        requests=tuple(
            UsageRequestMetrics(
                created_at.timestamp(),
                MappingProxyType(_counters(input_tokens)),
                model_provider=model_provider,
                model=model_id,
            )
            for created_at, (model_provider, model_id), input_tokens in requests
        ),
        kind=kind,  # type: ignore[arg-type]
    )


def _usage_row(
    source: UsageStorageSource,
    *runs: UsageRunNode,
    entity_id: str = "code",
    row_key: str = "session",
    runs_available: bool = True,
) -> UsageSessionRow:
    return UsageSessionRow(
        source=source,
        entity_id=entity_id,
        entity_kind="team" if source.scope == "team" else "agent",
        row_key=row_key,
        runs=runs,
        runs_available=runs_available,
    )


def test_spend_matches_pricing_the_full_admin_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Collecting only per-requester daily usage prices exactly what the admin report's daily rows hold."""
    config = _config()
    paths = _paths(tmp_path)
    astra = (_provider(config, tmp_path, "astra"), "gpt-6-astra")
    luna = (_provider(config, tmp_path, "luna"), "gpt-6-luna")
    local = (_provider(config, tmp_path, "local"), "qwen3.8:27b")

    def at(day: int, hour: int = 12, minute: int = 0, month: int = 10) -> datetime:
        return datetime(2026, month, day, hour, minute, tzinfo=UTC)

    shared = _source()
    team = _source("team")
    private = replace(
        _source(),
        path_label="private_agent.db",
        scope="private_agent",
        requester_isolated=True,
        owner_id=ALICE_BRIDGE,
    )
    system = replace(_source(), path_label="system.db", scope="system", source_agent_id=None)
    damaged = replace(_source(), path_label="damaged.db")
    missing = UsageStorageDiagnostic("missing.db", "partial", "source discovery unavailable", "shared_agent")
    conversation = _usage_row(
        shared,
        _requested_run("first", (at(2, 9), astra, 1_000), (at(2, 9, 5), astra, 2_000)),
        _requested_run("bridged", (at(3), luna, 5_000), requester_id=ALICE_BRIDGE),
        _requested_run("straddling", (at(30, 23, 50, month=9), astra, 3_000), (at(1, 0, 10), luna, 4_000)),
        _requested_run("last-month", (at(30, 10, month=9), astra, 50_000)),
        _requested_run("summary", (at(4), luna, 700), requester_id=BOB, kind="compaction_summary"),
        _requested_run("local", (at(5), local, 900), requester_id=BOB),
        _requested_run("unattributed", (at(5), astra, 800), (at(5, 13), local, 600), requester_id=None),
        replace(_run("undated", provider=astra[0], model=astra[1], created_at=NOW, input_tokens=40), created_at=None),
        row_key="conversation",
    )
    rows: dict[str, tuple[UsageSessionRow | UsageStorageDiagnostic, ...]] = {
        shared.path_label: (
            conversation,
            # A session read twice counts its runs once.
            conversation,
        ),
        team.path_label: (
            _usage_row(
                team,
                _requested_run("leader", (at(7), astra, 1_500)),
                _requested_run(
                    "member",
                    (at(7, 12, 1), luna, 2_500),
                    requester_id=ALICE_BRIDGE,
                    parent_run_id="leader",
                ),
                _requested_run("other-member", (at(7, 12, 2), astra, 3_500), requester_id=BOB, parent_run_id="leader"),
                entity_id="engineering",
            ),
            _usage_row(
                team,
                replace(_requested_run("unusable-models", (at(6), astra, 600), requester_id=BOB), model_metrics=None),
                entity_id="engineering",
                row_key="unusable-models",
            ),
        ),
        private.path_label: (
            _usage_row(
                private,
                _requested_run("owner-run", (at(8), astra, 1_200), requester_id=None),
                _requested_run("guest-run", (at(8), astra, 400), requester_id=BOB),
                _requested_run("helper", (at(8), luna, 300), requester_id=None, kind="compaction_summary"),
            ),
        ),
        system.path_label: (
            _usage_row(
                system,
                _requested_run("routing", (at(9), luna, 1_100), requester_id=BOB, kind="routing"),
                entity_id=SYSTEM_USAGE_ENTITY,
            ),
        ),
        damaged.path_label: (
            _usage_row(damaged, _requested_run("readable", (at(2), astra, 10_000), requester_id=BOB)),
            UsageStorageDiagnostic("damaged.db", "partial", "malformed retained session"),
            _usage_row(damaged, _requested_run("partly-read", (at(3), luna, 20_000)), runs_available=False),
        ),
    }
    monkeypatch.setattr(
        "mindroom.usage.stats.discover_admin_usage_sources",
        lambda **_: (damaged, missing, private, shared, system, team),
    )

    def iter_rows(
        source: UsageStorageSource,
        *,
        since: float | None = None,
    ) -> Iterator[UsageSessionRow | UsageStorageDiagnostic]:
        del since
        yield from rows[source.path_label]

    monkeypatch.setattr("mindroom.usage.stats.iter_usage_storage_rows", iter_rows)
    prices = model_prices(config)

    snapshot = collect_monthly_spend(config, paths, NOW, prices)

    assert snapshot == _spend_from_admin_report(config, paths, NOW, prices)
    assert set(snapshot.spend_usd) == {ALICE, BOB}
    assert {(model.provider, model.model) for model in snapshot.unpriced_models} == {local, ("unknown", "unknown")}
    assert (snapshot.scanned_sources, snapshot.unavailable_sources) == (5, 4)
