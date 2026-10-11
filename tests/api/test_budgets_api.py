"""Dashboard budget status route."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing

if TYPE_CHECKING:
    from pathlib import Path

_HEADERS = {"Authorization": "Bearer test-budget-key"}


def _client(temp_config_file: Path, tmp_path: Path) -> TestClient:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_API_KEY": "test-budget-key"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    return TestClient(main.app)


def _bind_monitor(client: TestClient, config: Config | None) -> None:
    state = config_lifecycle.app_state(client.app)
    runtime_paths = main._app_runtime_paths(client.app)
    state.budget_monitor = BudgetMonitor(runtime_paths=runtime_paths, config_provider=lambda: config)


def _budgeted_config() -> Config:
    return Config(
        models={
            "default": ModelConfig(provider="ollama", id="test-model", pricing=ModelPricing(input=5, output=30)),
            "luna": ModelConfig(provider="ollama", id="cheap", pricing=ModelPricing(input=0.2, output=1.25)),
        },
        budgets=BudgetsConfig(fallback_model="luna", monthly_limit_usd=20, users={"@alice:example.org": 100}),
    )


def test_budget_status_lists_settings_and_configured_users(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)
    _bind_monitor(client, _budgeted_config())

    try:
        response = client.get("/api/budgets", headers=_HEADERS)
    finally:
        config_lifecycle.app_state(client.app).budget_monitor = None

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["default_limit_usd"] == 20.0
    assert payload["fallback_model"] == "luna"
    assert payload["generated_at"] is None
    assert payload["users"] == [
        {"user_id": "@alice:example.org", "spend_usd": 0.0, "limit_usd": 100.0, "over_budget": False},
    ]


def test_budget_status_reports_disabled_budgets(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)
    _bind_monitor(client, Config())

    try:
        response = client.get("/api/budgets", headers=_HEADERS)
    finally:
        config_lifecycle.app_state(client.app).budget_monitor = None

    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_budget_status_is_unavailable_without_a_runtime_monitor(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)

    response = client.get("/api/budgets", headers=_HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "Budget monitor unavailable"}


def test_budget_status_requires_dashboard_authentication(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)

    assert client.get("/api/budgets").status_code == 401
    assert client.get("/api/budgets", headers={"Authorization": "Bearer wrong"}).status_code == 401
