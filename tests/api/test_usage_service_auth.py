"""Usage export service authentication integration tests."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt, _trusted_upstream_jwt_key

_ISSUER = "https://issuer.example"
_SERVICE_AUDIENCE = "mindroom-usage-export"
_SERVICE_CLIENT_ID = "usage-export-client.example.org"
_ASSERTION_HEADER = "Cf-Access-Jwt-Assertion"


def _service_assertion(
    private_key: rsa.RSAPrivateKey,
    *,
    omit_claims: frozenset[str] = frozenset(),
    **claim_overrides: str | datetime,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": _ISSUER,
        "aud": _SERVICE_AUDIENCE,
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "type": "app",
        "common_name": _SERVICE_CLIENT_ID,
        "sub": "",
    }
    claims.update(claim_overrides)
    for claim in omit_claims:
        claims.pop(claim, None)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture
def usage_service_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], rsa.RSAPrivateKey]:
    """Return strict service-auth settings backed by a locally served RSA JWKS."""
    private_key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(private_key))
    return (
        {
            "MINDROOM_API_KEY": "test-usage-key",
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "Cf-Access-Authenticated-User-Email",
            "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "true",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER": _ASSERTION_HEADER,
            "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL": "https://issuer.example/jwks",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE": "mindroom-dashboard",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER": _ISSUER,
            "MINDROOM_USAGE_SERVICE_JWT_AUDIENCE": _SERVICE_AUDIENCE,
            "MINDROOM_USAGE_SERVICE_CLIENT_ID": _SERVICE_CLIENT_ID,
        },
        private_key,
    )


def _initialize_usage_runtime(
    temp_config_file: Path,
    tmp_path: Path,
    env: dict[str, str],
) -> tuple[TestClient, Path]:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env=env,
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    return TestClient(main.app), runtime_paths.storage_root


def _seed_usage_database(storage_root: Path) -> None:
    database = storage_root / "agents/test_agent/sessions/test_agent.db"
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
                        },
                    ],
                ),
            ),
        )


def _invalid_service_assertion(case: str, private_key: rsa.RSAPrivateKey) -> str | None:
    """Build the one malformed or untrusted assertion named by a rejection test case."""
    now = datetime.now(UTC)
    missing_claims = ("exp", "iat", "iss", "aud", "type", "common_name", "sub")
    builders = {
        "missing-assertion": lambda: None,
        "query-assertion": lambda: None,
        "invalid-signature": lambda: _service_assertion(_trusted_upstream_jwt_key()),
        "expired": lambda: _service_assertion(private_key, exp=now - timedelta(minutes=1)),
        "future-iat": lambda: _service_assertion(private_key, iat=now + timedelta(minutes=1)),
        "wrong-issuer": lambda: _service_assertion(private_key, iss="https://other-issuer.example"),
        "wrong-audience": lambda: _service_assertion(private_key, aud="another-service"),
        "wrong-client": lambda: _service_assertion(private_key, common_name="other-client.example.org"),
        "wrong-type": lambda: _service_assertion(private_key, type="user"),
        "nonempty-subject": lambda: _service_assertion(private_key, sub="person@example.org"),
        "human-token": lambda: _trusted_upstream_jwt(
            private_key,
            audience=_SERVICE_AUDIENCE,
            email="owner@example.org",
            issuer=_ISSUER,
        ),
        "wrong-algorithm": lambda: jwt.encode(
            {
                "iss": _ISSUER,
                "aud": _SERVICE_AUDIENCE,
                "exp": now + timedelta(minutes=5),
                "iat": now,
                "type": "app",
                "common_name": _SERVICE_CLIENT_ID,
                "sub": "",
            },
            b"symmetric-test-key-at-least-32-bytes",
            algorithm="HS256",
            headers={"kid": "test-key"},
        ),
        "oversized": lambda: "x" * (16 * 1024 + 1),
        **{
            f"missing-{claim}": lambda claim=claim: _service_assertion(
                private_key,
                omit_claims=frozenset({claim}),
            )
            for claim in missing_claims
        },
    }
    return builders[case]()


def test_usage_export_accepts_service_assertion_and_returns_daily_report(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """The export route must verify a service assertion before reading real retained usage."""
    env, private_key = usage_service_auth
    client, storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    _seed_usage_database(storage_root)

    response = client.get(
        "/api/usage/export",
        params={"include_daily": "true"},
        headers={_ASSERTION_HEADER: _service_assertion(private_key)},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    expected_metrics = {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cache_read_tokens": 9,
        "cache_write_tokens": 3,
    }
    assert {key: payload["totals"][key] for key in expected_metrics} == expected_metrics
    assert payload["user_breakdown"][0]["user_id"] == "@alice:example.org"
    assert payload["user_breakdown"][0]["run_count"] == 1
    assert [row["model"] for row in payload["model_breakdown"]] == ["test-model", "other-model"]
    assert payload["daily_breakdown"][0]["date"] == "2023-11-14"
    assert {key: payload["daily_breakdown"][0]["totals"][key] for key in expected_metrics} == expected_metrics
    assert payload["daily_breakdown"][0]["model_breakdown"] == payload["model_breakdown"]


def test_usage_export_fails_closed_when_strict_jwt_configuration_is_partial(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """The export route must stay disabled until the shared strict JWT settings are complete."""
    env, private_key = usage_service_auth
    env.pop("MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE")
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)

    response = client.get(
        "/api/usage/export",
        headers={_ASSERTION_HEADER: _service_assertion(private_key)},
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    "missing_setting",
    [
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED",
        "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER",
        "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER",
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER",
        "MINDROOM_USAGE_SERVICE_JWT_AUDIENCE",
        "MINDROOM_USAGE_SERVICE_CLIENT_ID",
    ],
)
def test_usage_export_fails_closed_when_auth_setting_is_missing(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    missing_setting: str,
) -> None:
    """Every shared strict-JWT and service-specific setting must be present."""
    env, private_key = usage_service_auth
    env.pop(missing_setting)
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)

    response = client.get(
        "/api/usage/export",
        headers={_ASSERTION_HEADER: _service_assertion(private_key)},
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    "case",
    [
        "missing-assertion",
        "query-assertion",
        "invalid-signature",
        "expired",
        "future-iat",
        "missing-exp",
        "missing-iat",
        "missing-iss",
        "missing-aud",
        "missing-type",
        "missing-common_name",
        "missing-sub",
        "wrong-issuer",
        "wrong-audience",
        "wrong-client",
        "wrong-type",
        "nonempty-subject",
        "human-token",
        "wrong-algorithm",
        "oversized",
    ],
)
def test_usage_export_rejects_invalid_service_assertions(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    case: str,
) -> None:
    """The export route must reject malformed, untrusted, and human assertions."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    token = _invalid_service_assertion(case, private_key)
    headers = {_ASSERTION_HEADER: token} if token is not None else {}
    params = {"token": _service_assertion(private_key)} if case == "query-assertion" else None

    response = client.get("/api/usage/export", headers=headers, params=params)

    assert response.status_code == 401
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize("path", ["/api/usage", "/api/config/raw"])
def test_service_assertion_does_not_authorize_administrator_routes(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    path: str,
) -> None:
    """A service assertion must never become a dashboard administrator identity."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)

    response = client.get(path, headers={_ASSERTION_HEADER: _service_assertion(private_key)})

    assert response.status_code == 401


def test_usage_export_rejects_unsupported_write_method(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """The service assertion must not create a write-capable usage endpoint."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)

    response = client.post(
        "/api/usage/export",
        headers={_ASSERTION_HEADER: _service_assertion(private_key)},
    )

    assert response.status_code == 405


def test_usage_export_omits_daily_breakdown_by_default(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """The export route must preserve the administrator report's include_daily behavior."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)

    response = client.get(
        "/api/usage/export",
        headers={_ASSERTION_HEADER: _service_assertion(private_key)},
    )

    assert response.status_code == 200
    assert "daily_breakdown" not in response.json()
    assert "daily_coverage" not in response.json()
