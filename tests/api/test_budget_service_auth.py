"""Budget service authentication integration tests."""
# ruff: noqa: D103

from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt_key
from tests.api.test_usage_service_auth import _ASSERTION_HEADER, _service_assertion

_BUDGET_AUDIENCE = "mindroom-budget-service"
_BUDGET_CLIENT_ID = "budget-service-client.example.org"


@pytest.fixture
def budget_service_auth(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], rsa.RSAPrivateKey]:
    """Return strict trusted-upstream settings with both the usage and the budget service configured."""
    private_key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(private_key))
    return (
        {
            "MINDROOM_API_KEY": "test-budget-key",
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "Cf-Access-Authenticated-User-Email",
            "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "true",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER": _ASSERTION_HEADER,
            "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL": "https://issuer.example/jwks",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE": "mindroom-dashboard",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER": "https://issuer.example",
            "MINDROOM_USAGE_SERVICE_JWT_AUDIENCE": "mindroom-usage-export",
            "MINDROOM_USAGE_SERVICE_CLIENT_ID": "usage-export-client.example.org",
            "MINDROOM_BUDGET_SERVICE_JWT_AUDIENCE": _BUDGET_AUDIENCE,
            "MINDROOM_BUDGET_SERVICE_CLIENT_ID": _BUDGET_CLIENT_ID,
        },
        private_key,
    )


def _client(temp_config_file: Path, tmp_path: Path, env: dict[str, str]) -> TestClient:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env=env,
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    client = TestClient(main.app)
    config = Config(
        models={"luna": ModelConfig(provider="ollama", id="cheap", pricing=ModelPricing(input=0.2, output=1.25))},
        budgets=BudgetsConfig(fallback_model="luna", monthly_limit_usd=20),
    )
    config_lifecycle.app_state(client.app).budget_monitor = BudgetMonitor(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
    )
    return client


def _budget_assertion(private_key: rsa.RSAPrivateKey, **claim_overrides: object) -> str:
    return _service_assertion(
        private_key,
        **{"aud": _BUDGET_AUDIENCE, "common_name": _BUDGET_CLIENT_ID, **claim_overrides},
    )


def test_budget_service_assertion_reads_budget_status(
    temp_config_file: Path,
    tmp_path: Path,
    budget_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    env, private_key = budget_service_auth
    client = _client(temp_config_file, tmp_path, env)

    response = client.get("/api/budgets/service", headers={_ASSERTION_HEADER: _budget_assertion(private_key)})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["default_limit_usd"] == 20.0


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({}, id="usage-export-assertion"),
        pytest.param({"aud": _BUDGET_AUDIENCE}, id="usage-export-client"),
        pytest.param({"common_name": _BUDGET_CLIENT_ID}, id="usage-export-audience"),
        pytest.param({"aud": _BUDGET_AUDIENCE, "common_name": _BUDGET_CLIENT_ID, "type": "user"}, id="wrong-type"),
        pytest.param(
            {"aud": _BUDGET_AUDIENCE, "common_name": _BUDGET_CLIENT_ID, "sub": "person@example.org"},
            id="nonempty-subject",
        ),
    ],
)
def test_budget_service_rejects_other_service_assertions(
    temp_config_file: Path,
    tmp_path: Path,
    budget_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    claims: dict[str, object],
) -> None:
    env, private_key = budget_service_auth
    client = _client(temp_config_file, tmp_path, env)

    response = client.get(
        "/api/budgets/service",
        headers={_ASSERTION_HEADER: _service_assertion(private_key, **claims)},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid budget service JWT"}


@pytest.mark.parametrize("path", ["/api/budgets", "/api/usage/export", "/api/config/raw"])
def test_budget_service_assertion_does_not_authorize_other_routes(
    temp_config_file: Path,
    tmp_path: Path,
    budget_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    path: str,
) -> None:
    env, private_key = budget_service_auth
    client = _client(temp_config_file, tmp_path, env)

    response = client.get(path, headers={_ASSERTION_HEADER: _budget_assertion(private_key)})

    assert response.status_code == 401


@pytest.mark.parametrize("missing", ["MINDROOM_BUDGET_SERVICE_JWT_AUDIENCE", "MINDROOM_BUDGET_SERVICE_CLIENT_ID"])
def test_budget_service_fails_closed_when_its_setting_is_missing(
    temp_config_file: Path,
    tmp_path: Path,
    budget_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    missing: str,
) -> None:
    env, private_key = budget_service_auth
    client = _client(temp_config_file, tmp_path, {key: value for key, value in env.items() if key != missing})

    response = client.get("/api/budgets/service", headers={_ASSERTION_HEADER: _budget_assertion(private_key)})

    assert response.status_code == 503
    assert response.json() == {"detail": "Budget service authentication is not configured"}
