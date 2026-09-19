"""Usage export service authentication integration tests."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.api.usage_export import UsageExportRunner
from mindroom.legacy_usage_storage import migrate_usage_database
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt, _trusted_upstream_jwt_key

_ISSUER = "https://issuer.example"
_SERVICE_AUDIENCE = "mindroom-usage-export"
_SERVICE_CLIENT_ID = "usage-export-client.example.org"
_ASSERTION_HEADER = "Cf-Access-Jwt-Assertion"


class ManualExportWorkers:
    """Capture export jobs so handler tests complete them deterministically."""

    def __init__(self) -> None:
        self.targets: list[Callable[[], None]] = []

    def start(self, target: Callable[[], None]) -> None:
        """Capture one worker target without starting it."""
        self.targets.append(target)

    def run_next(self) -> None:
        """Run the oldest captured target synchronously."""
        self.targets.pop(0)()


def _service_assertion(
    private_key: rsa.RSAPrivateKey,
    *,
    omit_claims: frozenset[str] = frozenset(),
    **claim_overrides: object,
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


def _install_manual_export_runner(client: TestClient) -> tuple[UsageExportRunner, ManualExportWorkers]:
    workers = ManualExportWorkers()
    runner = UsageExportRunner(start_worker=workers.start)
    state = config_lifecycle.app_state(client.app)
    if state.usage_export_runner is not None:
        state.usage_export_runner.close()
    state.usage_export_runner = runner
    return runner, workers


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
            "CREATE TABLE test_agent_sessions (session_id TEXT PRIMARY KEY, session_type TEXT, agent_id TEXT, "
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
                            "content": "sensitive-conversation-content",
                            "messages": [{"content": "sensitive-prompt-content"}],
                        },
                    ],
                ),
            ),
        )

    migrate_usage_database(database, "test_agent_sessions")


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
        "non-ascii-client": lambda: _service_assertion(private_key, common_name="service-élève.example.org"),
        "lone-surrogate-client": lambda: _service_assertion(private_key, common_name="\ud800"),
        "non-string-client": lambda: _service_assertion(private_key, common_name=123),
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


def test_usage_export_prepares_and_returns_real_daily_report(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """Authenticated polling must return a prepared report from real retained storage."""
    env, private_key = usage_service_auth
    client, storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    _seed_usage_database(storage_root)
    runner, workers = _install_manual_export_runner(client)
    headers = {_ASSERTION_HEADER: _service_assertion(private_key)}

    try:
        response = client.get(
            "/api/usage/export",
            params={"include_daily": "true"},
            headers=headers,
        )

        assert response.status_code == 202
        assert response.json() == {"status": "pending"}
        assert response.headers["retry-after"] == "5"
        assert response.headers["cache-control"] == "no-store"
        assert len(workers.targets) == 1

        repeated = client.get("/api/usage/export", params={"include_daily": "true"}, headers=headers)
        assert repeated.status_code == 202
        assert len(workers.targets) == 1

        before_scan = datetime.now(UTC)
        workers.run_next()
        after_scan = datetime.now(UTC)
        ready = client.get("/api/usage/export", params={"include_daily": "true"}, headers=headers)
        cached = client.get("/api/usage/export", params={"include_daily": "true"}, headers=headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert ready.status_code == 200
    assert ready.headers["cache-control"] == "no-store"
    payload = ready.json()
    assert payload["schema_version"] == 1
    generated_at = datetime.fromisoformat(payload["generated_at"])
    assert generated_at.utcoffset() == timedelta(0)
    assert before_scan <= generated_at <= after_scan
    assert cached.json() == payload
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
    assert payload["user_breakdown"][0]["model_breakdown"] == payload["model_breakdown"]
    assert [row["model"] for row in payload["model_breakdown"]] == ["test-model", "other-model"]
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
    assert payload["daily_breakdown"][0]["date"] == "2023-11-14"
    assert payload["daily_breakdown"][0]["run_count"] == 1
    assert {key: payload["daily_breakdown"][0]["totals"][key] for key in expected_metrics} == expected_metrics
    assert payload["daily_breakdown"][0]["model_breakdown"] == payload["model_breakdown"]
    assert payload["user_breakdown"][0]["daily_breakdown"] == payload["daily_breakdown"]
    assert "daily_coverage" in payload
    assert "sensitive-conversation-content" not in ready.text
    assert "sensitive-prompt-content" not in ready.text
    assert str(storage_root) not in ready.text


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


def test_usage_export_sanitizes_failed_committed_configuration(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """Committed configuration diagnostics must not cross the service boundary."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)
    api_state = config_lifecycle.require_api_state(client.app)
    sensitive_detail = "sensitive-config-diagnostic"
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
            "/api/usage/export",
            headers={_ASSERTION_HEADER: _service_assertion(private_key)},
        )
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert response.status_code == 503
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert sensitive_detail not in response.text
    assert workers.targets == []


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
        "non-ascii-client",
        "lone-surrogate-client",
        "non-string-client",
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
    if case in {"non-ascii-client", "lone-surrogate-client", "non-string-client"}:
        assert response.json() == {"detail": "Invalid usage service JWT"}
    else:
        assert set(response.json()) == {"detail"}


@pytest.mark.parametrize("path", ["/api/usage", "/api/config/raw", "/api/usage/me/private-agents"])
def test_service_assertion_does_not_authorize_other_routes(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    path: str,
) -> None:
    """A service assertion must not become an administrator or personal identity."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)

    response = client.get(path, headers={_ASSERTION_HEADER: _service_assertion(private_key)})

    assert response.status_code == 401


def test_dashboard_and_service_routes_share_preparation_and_cache(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """Both authenticated adapters must share work while guarding every poll."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)
    dashboard_email = "owner@example.org"
    dashboard_headers = {
        "Cf-Access-Authenticated-User-Email": dashboard_email,
        _ASSERTION_HEADER: _trusted_upstream_jwt(
            private_key,
            email=dashboard_email,
            audience="mindroom-dashboard",
            issuer=_ISSUER,
        ),
    }
    service_headers = {_ASSERTION_HEADER: _service_assertion(private_key)}

    try:
        dashboard_pending = client.get("/api/usage", headers=dashboard_headers)
        service_pending = client.get("/api/usage/export", headers=service_headers)
        assert dashboard_pending.status_code == 202
        assert service_pending.status_code == 202
        assert len(workers.targets) == 1
        workers.run_next()

        assert client.get("/api/usage").status_code == 401
        assert client.get("/api/usage/export").status_code == 401
        dashboard_ready = client.get("/api/usage", headers=dashboard_headers)
        service_ready = client.get("/api/usage/export", headers=service_headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert dashboard_ready.status_code == 200
    assert service_ready.status_code == 200
    assert dashboard_ready.json() == service_ready.json()


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
    """The export route must preserve the organization report's include_daily behavior."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)
    headers = {_ASSERTION_HEADER: _service_assertion(private_key)}

    try:
        pending = client.get("/api/usage/export", headers=headers)
        assert pending.status_code == 202
        workers.run_next()
        response = client.get("/api/usage/export", headers=headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert response.status_code == 200
    assert "daily_breakdown" not in response.json()
    assert "daily_coverage" not in response.json()


def test_usage_export_failure_is_content_free_and_temporarily_cached(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan exception must become a stable content-free service failure."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)
    headers = {_ASSERTION_HEADER: _service_assertion(private_key)}

    private_error = "sensitive-retained-report-detail"

    def fail_report(**_kwargs: object) -> object:
        raise RuntimeError(private_error)

    monkeypatch.setattr("mindroom.api.usage.collect_admin_usage", fail_report)
    try:
        with capture_logs() as logs:
            assert client.get("/api/usage/export", headers=headers).status_code == 202
            workers.run_next()
            first_failure = client.get("/api/usage/export", headers=headers)
            repeated_failure = client.get("/api/usage/export", headers=headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert first_failure.status_code == 503
    assert first_failure.content == b""
    assert first_failure.headers["cache-control"] == "no-store"
    assert private_error not in first_failure.text
    assert private_error not in str(logs)
    assert repeated_failure.status_code == 503
    assert workers.targets == []


def test_usage_export_worker_start_failure_returns_immediate_cached_503(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """A worker-start failure must return 503 immediately and honor retry backoff."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    headers = {_ASSERTION_HEADER: _service_assertion(private_key)}
    now = 0.0
    start_calls = 0
    sensitive_value = "sensitive-worker-start-detail"

    def fail_start(_target: Callable[[], None]) -> None:
        nonlocal start_calls
        start_calls += 1
        raise RuntimeError(sensitive_value)

    runner = UsageExportRunner(start_worker=fail_start, clock=lambda: now)
    config_lifecycle.app_state(client.app).usage_export_runner = runner
    try:
        with capture_logs() as logs:
            first = client.get("/api/usage/export", headers=headers)
            cached = client.get("/api/usage/export", headers=headers)
            now = 5
            retried = client.get("/api/usage/export", headers=headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert first.status_code == 503
    assert first.content == b""
    assert first.headers["cache-control"] == "no-store"
    assert cached.status_code == 503
    assert retried.status_code == 503
    assert start_calls == 2
    assert sensitive_value not in first.text + str(logs)


def test_usage_export_authentication_precedes_start_and_cache_access(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],
) -> None:
    """Unauthenticated requests must neither start work nor read a completed report."""
    env, private_key = usage_service_auth
    client, _storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    runner, workers = _install_manual_export_runner(client)
    headers = {_ASSERTION_HEADER: _service_assertion(private_key)}

    try:
        assert client.get("/api/usage/export").status_code == 401
        assert workers.targets == []
        assert client.get("/api/usage/export", headers=headers).status_code == 202
        workers.run_next()
        assert client.get("/api/usage/export").status_code == 401
        ready = client.get("/api/usage/export", headers=headers)
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None

    assert ready.status_code == 200
