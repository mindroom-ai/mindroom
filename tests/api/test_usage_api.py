"""Usage API authentication and retained-storage integration."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt, _trusted_upstream_jwt_key


@pytest.fixture
def signed_usage_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], Callable[..., dict[str, str]]]:
    """Model an Access identity with email-to-Matrix mapping and locally served JWKS."""
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


@pytest.mark.parametrize("trusted_upstream", [False, True], ids=["api-key", "signed-admin"])
@pytest.mark.parametrize("include_daily", [None, False, True])
def test_usage_endpoint_reads_retained_tokens_and_requires_dashboard_auth(
    temp_config_file: Path,
    tmp_path: Path,
    include_daily: bool | None,
    trusted_upstream: bool,
    signed_usage_auth: tuple[dict[str, str], Callable[..., dict[str, str]]],
) -> None:
    """Missing auth must not expose usage; valid auth reads real retained sessions."""
    signed_env, signed_headers = signed_usage_auth
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env=signed_env if trusted_upstream else {"MINDROOM_API_KEY": "test-usage-key"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    database = runtime_paths.storage_root / "agents/test_agent/sessions/test_agent.db"
    database.parent.mkdir(parents=True)
    metrics = {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cache_read_tokens": 9,
        "cache_write_tokens": 3,
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE test_agent_sessions (session_id TEXT, session_type TEXT, agent_id TEXT, "
            "team_id TEXT, user_id TEXT, session_data TEXT, runs TEXT)",
        )
        connection.execute(
            "INSERT INTO test_agent_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session",
                "agent",
                "test_agent",
                None,
                "@alice:example.org",
                json.dumps({"session_metrics": metrics}),
                json.dumps(
                    [
                        {
                            "run_id": "run",
                            "model": "test-model",
                            "model_provider": "ollama",
                            "created_at": 1_700_000_000,
                            "metrics": {
                                **metrics,
                                "details": {
                                    "model": [
                                        {
                                            "id": "test-model",
                                            "provider": "ollama",
                                            "input_tokens": 10,
                                            "output_tokens": 5,
                                            "total_tokens": 15,
                                            "cache_read_tokens": 7,
                                            "cache_write_tokens": 2,
                                        },
                                    ],
                                    "output_model": [
                                        {
                                            "id": "other-model",
                                            "provider": "ollama",
                                            "input_tokens": 2,
                                            "output_tokens": 3,
                                            "total_tokens": 5,
                                            "cache_read_tokens": 2,
                                            "cache_write_tokens": 1,
                                        },
                                    ],
                                },
                            },
                            "content": "private message",
                            "messages": [{"content": "private prompt"}],
                        },
                    ],
                ),
            ),
        )
    client = TestClient(main.app)
    params = {} if include_daily is None else {"include_daily": str(include_daily).lower()}
    assert client.get("/api/usage", params=params).status_code == 401
    assert client.get("/api/usage", params=params, headers={"Authorization": "Bearer wrong"}).status_code == 401
    headers = signed_headers() if trusted_upstream else {"Authorization": "Bearer test-usage-key"}
    response = client.get("/api/usage", params=params, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["totals"]["total_tokens"] == 20
    assert payload["user_breakdown"][0]["user_id"] == "@alice:example.org"
    assert payload["user_breakdown"][0]["run_count"] == 1
    assert payload["user_breakdown"][0]["model_breakdown"] == payload["model_breakdown"]
    assert [
        (
            row["model"],
            row["totals"]["input_tokens"],
            row["totals"]["output_tokens"],
            row["totals"]["cache_read_tokens"],
            row["totals"]["cache_write_tokens"],
        )
        for row in payload["model_breakdown"]
    ] == [
        ("test-model", 10, 5, 7, 2),
        ("other-model", 2, 3, 2, 1),
    ]
    if include_daily:
        day = payload["daily_breakdown"][0]
        assert day["date"] == "2023-11-14"
        assert day["run_count"] == 1
        assert day["model_breakdown"] == payload["model_breakdown"]
        assert {key: day["totals"][key] for key in metrics} == metrics
        assert "daily_coverage" in payload
        assert payload["user_breakdown"][0]["daily_breakdown"] == payload["daily_breakdown"]
    else:
        assert "daily_breakdown" not in payload
        assert "daily_coverage" not in payload
        assert "daily_breakdown" not in payload["user_breakdown"][0]
    assert "private message" not in response.text
    assert "private prompt" not in response.text
    assert str(database) not in response.text


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
def test_usage_endpoint_requires_signed_administrator(
    temp_config_file: Path,
    tmp_path: Path,
    signed_usage_auth: tuple[dict[str, str], Callable[..., dict[str, str]]],
    case: str,
    status: int,
) -> None:
    """The real usage route must validate signatures and mapped administrator identity."""
    env, signed_headers = signed_usage_auth
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env=env,
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
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

    response = TestClient(main.app).get(
        "/api/usage",
        params={"include_daily": "true"},
        headers=headers_by_case[case],
    )

    assert response.status_code == status
    assert set(response.json()) == {"detail"}
