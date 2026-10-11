"""Monthly per-user spend derived from retained usage."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest
from agno.metrics import ModelMetrics, RunMetrics
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage
from mindroom.budgets.pricing import price_table
from mindroom.budgets.spend import _UnpricedModelUsage, collect_monthly_spend, month_bounds
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.usage_stats_storage import UsageRunNode, UsageSessionRow, UsageStorageDiagnostic, UsageStorageSource
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import Iterator

ALICE = "@alice:example.test"
BOB = "@bob:example.test"
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
        expected_session_table="sessions",
        source_agent_id=agent,
        allowed_agent_ids=frozenset({"code"}),
        requester_isolated=False,
    )


def _provider(config: Config, tmp_path: Path, model_name: str) -> str:
    return get_model_instance(config, _paths(tmp_path), model_name).get_provider()


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
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", lambda **_: tuple(rows))

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

    monkeypatch.setattr("mindroom.usage_stats.iter_usage_storage_rows", iter_rows)
    return seen_since


def _spend(config: Config, tmp_path: Path) -> object:
    paths = _paths(tmp_path)
    return collect_monthly_spend(config, paths, NOW, price_table(config, paths).prices)


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
    assert snapshot.unpriced_models == (_UnpricedModelUsage(provider=ollama, model="qwen3.8:27b", total_tokens=1000),)


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


def test_spend_prices_usage_recorded_through_agno_storage(tmp_path: Path) -> None:
    """Usage written by real session storage must match the price table's model identities."""
    config = _config()
    paths = _paths(tmp_path)
    model = get_model_instance(config, paths, "astra")
    storage = create_state_storage(
        "code",
        paths.storage_root / "agents" / "code",
        subdir="sessions",
        session_table="code_sessions",
    )
    metrics = RunMetrics(
        input_tokens=1_000_000,
        output_tokens=100_000,
        total_tokens=1_100_000,
        details={
            "model": [
                ModelMetrics(
                    id=model.id,
                    provider=model.get_provider(),
                    input_tokens=1_000_000,
                    output_tokens=100_000,
                    total_tokens=1_100_000,
                ),
            ],
        },
    )
    try:
        seed_session(
            storage,
            AgentSession(
                session_id="session-1",
                agent_id="code",
                user_id=ALICE,
                runs=[
                    RunOutput(
                        run_id="run-1",
                        model_provider=model.provider,
                        model=model.id,
                        created_at=int(datetime(2026, 10, 3, tzinfo=UTC).timestamp()),
                        metrics=metrics,
                    ),
                ],
            ),
        )
    finally:
        storage.close()

    snapshot = collect_monthly_spend(config, paths, NOW, price_table(config, paths).prices)

    assert snapshot.spend_usd == {ALICE: pytest.approx(5.0 + 3.0)}
    assert snapshot.unpriced_models == ()
