"""Budget status and per-user cap routes for the dashboard and the budget service."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.api.auth import require_budget_service
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_HEADERS = {"Authorization": "Bearer test-budget-key"}
_NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
ALICE = "@alice:example.org"
ALICE_BRIDGE = "@telegram-alice:example.org"


def _client(temp_config_file: Path, tmp_path: Path) -> TestClient:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_API_KEY": "test-budget-key"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    return TestClient(main.app)


def _bind_monitor(client: TestClient, config: Config | None) -> BudgetMonitor:
    state = config_lifecycle.app_state(client.app)
    runtime_paths = main._app_runtime_paths(client.app)
    state.budget_monitor = BudgetMonitor(runtime_paths=runtime_paths, config_provider=lambda: config)
    return state.budget_monitor


def _budgeted_config() -> Config:
    return Config(
        models={
            "default": ModelConfig(provider="ollama", id="test-model", pricing=ModelPricing(input=5, output=30)),
            "luna": ModelConfig(provider="ollama", id="cheap", pricing=ModelPricing(input=0.2, output=1.25)),
        },
        budgets=BudgetsConfig(fallback_model="luna", monthly_limit_usd=20),
    )


def test_budget_status_lists_settings_and_users_with_caps(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)
    monitor = _bind_monitor(client, _budgeted_config())
    monitor.caps.set(ALICE, 100, updated_by="budget-service", now=_NOW)

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
        {
            "user_id": ALICE,
            "spend_usd": 0.0,
            "limit_usd": 100.0,
            "over_budget": False,
            "override_usd": 100.0,
            "updated_at": "2026-10-11T12:00:00+00:00",
            "updated_by": "budget-service",
        },
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


@pytest.fixture
def budget_client(temp_config_file: Path, tmp_path: Path) -> Iterator[tuple[TestClient, BudgetMonitor]]:
    """Return a dashboard client bound to a monitor whose config has budgets, an alias, and a bot account."""
    client = _client(temp_config_file, tmp_path)
    config = _budgeted_config().model_copy(
        update={
            "authorization": AuthorizationConfig(aliases={ALICE: [ALICE_BRIDGE]}),
            "bot_accounts": ["@bridgebot:example.org"],
        },
    )
    monitor = _bind_monitor(client, config)
    try:
        yield client, monitor
    finally:
        config_lifecycle.app_state(client.app).budget_monitor = None
        client.app.dependency_overrides.pop(require_budget_service, None)


def _row(client: TestClient, user_id: str) -> dict[str, object] | None:
    rows = client.get("/api/budgets", headers=_HEADERS).json()["users"]
    return next((row for row in rows if row["user_id"] == user_id), None)


def test_dashboard_sets_a_cap_for_a_user_without_spend(budget_client: tuple[TestClient, BudgetMonitor]) -> None:
    client, _monitor = budget_client

    response = client.put(f"/api/budgets/users/{ALICE}", json={"limit_usd": 50}, headers=_HEADERS)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["user_id"] == ALICE
    assert body["limit_usd"] == 50.0
    assert body["updated_by"] == "dashboard"
    row = _row(client, ALICE)
    assert row is not None
    assert row["spend_usd"] == 0.0
    assert row["limit_usd"] == 50.0
    assert row["override_usd"] == 50.0
    assert row["updated_by"] == "dashboard"
    assert row["updated_at"] == body["updated_at"]


def test_service_sets_a_cap_as_the_budget_service(budget_client: tuple[TestClient, BudgetMonitor]) -> None:
    client, monitor = budget_client
    client.app.dependency_overrides[require_budget_service] = lambda: None

    response = client.put(f"/api/budgets/service/users/{ALICE}", json={"limit_usd": 100})
    status_payload = client.get("/api/budgets/service").json()

    assert response.status_code == 200
    assert response.json()["updated_by"] == "budget-service"
    assert monitor.caps.get(ALICE) is not None
    assert [row["override_usd"] for row in status_payload["users"] if row["user_id"] == ALICE] == [100.0]


def test_a_cap_for_a_bridge_alias_belongs_to_its_canonical_user(
    budget_client: tuple[TestClient, BudgetMonitor],
) -> None:
    client, monitor = budget_client

    response = client.put(f"/api/budgets/users/{ALICE_BRIDGE}", json={"limit_usd": 50}, headers=_HEADERS)

    assert response.json()["user_id"] == ALICE
    assert set(monitor.caps.entries()) == {ALICE}
    assert _row(client, ALICE_BRIDGE) is None


def test_a_percent_encoded_user_id_addresses_the_same_cap(budget_client: tuple[TestClient, BudgetMonitor]) -> None:
    client, monitor = budget_client

    client.put(f"/api/budgets/users/{quote(ALICE, safe='')}", json={"limit_usd": 50}, headers=_HEADERS)
    client.put(f"/api/budgets/users/{ALICE}", json={"limit_usd": 150}, headers=_HEADERS)

    assert {user_id: cap.limit_usd for user_id, cap in monitor.caps.entries().items()} == {ALICE: 150.0}


def test_clearing_a_cap_is_idempotent(budget_client: tuple[TestClient, BudgetMonitor]) -> None:
    client, monitor = budget_client
    client.put(f"/api/budgets/users/{ALICE}", json={"limit_usd": 50}, headers=_HEADERS)

    first = client.delete(f"/api/budgets/users/{ALICE}", headers=_HEADERS)
    second = client.delete(f"/api/budgets/users/{ALICE}", headers=_HEADERS)

    assert (first.status_code, second.status_code) == (204, 204)
    assert monitor.caps.entries() == {}
    assert _row(client, ALICE) is None


def test_service_clears_a_cap(budget_client: tuple[TestClient, BudgetMonitor]) -> None:
    client, monitor = budget_client
    client.app.dependency_overrides[require_budget_service] = lambda: None
    client.put(f"/api/budgets/service/users/{ALICE}", json={"limit_usd": 50})

    response = client.delete(f"/api/budgets/service/users/{ALICE_BRIDGE}")

    assert response.status_code == 204
    assert monitor.caps.entries() == {}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"limit_usd": -1}, id="negative"),
        pytest.param({"limit_usd": True}, id="boolean"),
        pytest.param({"limit_usd": "5"}, id="string"),
        pytest.param({"limit_usd": None}, id="null"),
        pytest.param({}, id="missing"),
        pytest.param({"limit_usd": 5, "note": "x"}, id="extra-field"),
    ],
)
def test_invalid_amounts_are_rejected(budget_client: tuple[TestClient, BudgetMonitor], body: object) -> None:
    client, monitor = budget_client

    response = client.put(f"/api/budgets/users/{ALICE}", json=body, headers=_HEADERS)

    assert response.status_code == 422
    assert "detail" in response.json()
    assert monitor.caps.entries() == {}


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1e400"])
def test_non_finite_amounts_are_rejected(budget_client: tuple[TestClient, BudgetMonitor], amount: str) -> None:
    client, monitor = budget_client

    response = client.put(
        f"/api/budgets/users/{ALICE}",
        content=f'{{"limit_usd": {amount}}}',
        headers={**_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert monitor.caps.entries() == {}


@pytest.mark.parametrize(
    ("user_id", "detail"),
    [
        pytest.param("alice", "User ID must be a concrete Matrix user ID", id="not-a-matrix-id"),
        pytest.param("@*:example.org", "User ID must be a concrete Matrix user ID", id="wildcard"),
        pytest.param("@bridgebot:example.org", "Caps apply only to people, not agents or bots", id="bot-account"),
    ],
)
def test_caps_need_a_concrete_human_user_id(
    budget_client: tuple[TestClient, BudgetMonitor],
    user_id: str,
    detail: str,
) -> None:
    client, monitor = budget_client

    response = client.put(f"/api/budgets/users/{quote(user_id, safe='')}", json={"limit_usd": 5}, headers=_HEADERS)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert monitor.caps.entries() == {}


def test_caps_cannot_be_written_while_budgets_are_off(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)
    monitor = _bind_monitor(client, Config())

    try:
        put = client.put(f"/api/budgets/users/{ALICE}", json={"limit_usd": 5}, headers=_HEADERS)
        delete = client.delete(f"/api/budgets/users/{ALICE}", headers=_HEADERS)
    finally:
        config_lifecycle.app_state(client.app).budget_monitor = None

    assert (put.status_code, put.json()) == (409, {"detail": "Budgets are off"})
    assert (delete.status_code, delete.json()) == (409, {"detail": "Budgets are off"})
    assert monitor.caps.entries() == {}


def test_cap_writes_need_the_runtime_monitor(temp_config_file: Path, tmp_path: Path) -> None:
    client = _client(temp_config_file, tmp_path)

    response = client.put(f"/api/budgets/users/{ALICE}", json={"limit_usd": 5}, headers=_HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "Budget monitor unavailable"}


def test_cap_routes_require_authentication(budget_client: tuple[TestClient, BudgetMonitor]) -> None:
    client, monitor = budget_client

    assert client.put(f"/api/budgets/users/{ALICE}", json={"limit_usd": 5}).status_code == 401
    assert client.delete(f"/api/budgets/users/{ALICE}").status_code == 401
    assert client.put(f"/api/budgets/service/users/{ALICE}", json={"limit_usd": 5}).status_code == 503
    assert client.delete(f"/api/budgets/service/users/{ALICE}", headers=_HEADERS).status_code == 503
    assert monitor.caps.entries() == {}
