"""Dashboard authentication and polling coverage for organization usage."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt, _trusted_upstream_jwt_key
from tests.api.test_usage_service_auth import _install_manual_export_runner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def signed_usage_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], Callable[..., dict[str, str]]]:
    """Return strict dashboard auth settings and real signed identity headers."""
    private_key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(private_key))
    env = {
        "MINDROOM_API_KEY": "test-usage-key",
        "MINDROOM_CONNECTIONS_AGENT": "test_agent",
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "Cf-Access-Authenticated-User-Email",
        "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "Cf-Access-Authenticated-User-Email",
        "MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN": "example.org",
        "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:example.org",
        "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "true",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER": "Cf-Access-Jwt-Assertion",
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL": "https://issuer.example/jwks",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE": "mindroom-dashboard",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER": "https://issuer.example",
    }

    def headers(
        email: str = "owner@example.org",
        *,
        audience: str = "mindroom-dashboard",
        issuer: str = "https://issuer.example",
        expires_at: datetime | None = None,
    ) -> dict[str, str]:
        return {
            "Cf-Access-Authenticated-User-Email": email,
            "Cf-Access-Jwt-Assertion": _trusted_upstream_jwt(
                private_key,
                email=email,
                audience=audience,
                issuer=issuer,
                expires_at=expires_at,
            ),
        }

    return env, headers


def _dashboard_client(temp_config_file: Path, tmp_path: Path, env: dict[str, str]) -> TestClient:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env=env,
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    return TestClient(main.app)


def test_usage_dashboard_uses_standard_api_key_auth_and_polling(
    temp_config_file: Path,
    tmp_path: Path,
) -> None:
    """Standalone dashboard auth must guard both preparation and cached reports."""
    client = _dashboard_client(temp_config_file, tmp_path, {"MINDROOM_API_KEY": "test-usage-key"})
    runner, workers = _install_manual_export_runner(client)
    valid_headers = {"Authorization": "Bearer test-usage-key"}

    try:
        assert client.get("/api/usage").status_code == 401
        assert client.get("/api/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert workers.targets == []

        pending = client.get("/api/usage", headers=valid_headers)
        assert pending.status_code == 202
        assert pending.json() == {"status": "pending"}
        assert pending.headers["retry-after"] == "5"
        assert pending.headers["cache-control"] == "no-store"
        assert len(workers.targets) == 1
        workers.run_next()

        assert client.get("/api/usage").status_code == 401
        ready = client.get("/api/usage", headers=valid_headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert ready.status_code == 200
    assert ready.headers["cache-control"] == "no-store"
    assert ready.json()["scope"] == "admin"


def test_usage_dashboard_sanitizes_failed_committed_configuration(
    temp_config_file: Path,
    tmp_path: Path,
) -> None:
    """Dashboard polling must not expose committed configuration diagnostics."""
    client = _dashboard_client(temp_config_file, tmp_path, {"MINDROOM_API_KEY": "test-usage-key"})
    runner, workers = _install_manual_export_runner(client)
    api_state = config_lifecycle.require_api_state(client.app)
    sensitive_detail = "sensitive-dashboard-config-diagnostic"
    with api_state.config_lock:
        snapshot = api_state.snapshot
        api_state.snapshot = replace(
            snapshot,
            generation=snapshot.generation + 1,
            config_load_result=config_lifecycle.ConfigLoadResult(
                success=False,
                error_status_code=422,
                error_detail={"diagnostic": sensitive_detail},
            ),
        )

    try:
        response = client.get(
            "/api/usage",
            headers={"Authorization": "Bearer test-usage-key"},
        )
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert response.status_code == 503
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert sensitive_detail not in response.text
    assert workers.targets == []


def test_usage_dashboard_accepts_real_signed_administrator(
    temp_config_file: Path,
    tmp_path: Path,
    signed_usage_auth: tuple[dict[str, str], Callable[..., dict[str, str]]],
) -> None:
    """A verified deployment administrator must prepare and poll the shared report."""
    env, signed_headers = signed_usage_auth
    client = _dashboard_client(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)

    try:
        pending = client.get("/api/usage", headers=signed_headers())
        assert pending.status_code == 202
        workers.run_next()
        ready = client.get("/api/usage", headers=signed_headers())
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert ready.status_code == 200
    assert ready.json()["scope"] == "admin"


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("anonymous", 401),
        ("owner-api-key", 401),
        ("unsigned-identity", 401),
        ("nonadmin", 403),
        ("expired", 401),
        ("wrong-audience", 401),
        ("wrong-issuer", 401),
        ("mismatched-email", 401),
    ],
)
def test_usage_dashboard_rejects_invalid_or_unauthorized_identity(
    temp_config_file: Path,
    tmp_path: Path,
    signed_usage_auth: tuple[dict[str, str], Callable[..., dict[str, str]]],
    case: str,
    status: int,
) -> None:
    """The usage route must retain the normal hosted dashboard access policy."""
    env, signed_headers = signed_usage_auth
    client = _dashboard_client(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)
    headers_by_case = {
        "anonymous": {},
        "owner-api-key": {"Authorization": "Bearer test-usage-key"},
        "unsigned-identity": {"Cf-Access-Authenticated-User-Email": "owner@example.org"},
        "nonadmin": signed_headers("alice@example.org"),
        "expired": signed_headers(expires_at=datetime.now(UTC) - timedelta(minutes=1)),
        "wrong-audience": signed_headers(audience="another-application"),
        "wrong-issuer": signed_headers(issuer="https://another-issuer.example"),
        "mismatched-email": {
            **signed_headers("alice@example.org"),
            "Cf-Access-Authenticated-User-Email": "owner@example.org",
        },
    }

    try:
        response = client.get("/api/usage", headers=headers_by_case[case])
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    assert workers.targets == []
