"""Pytest configuration and fixtures for dashboard backend tests."""

import asyncio
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.requests import Request

from mindroom.api import config_lifecycle
from mindroom.config.commit import ConfigCommit
from mindroom.config.load_attempt import ConfigLoadResult
from mindroom.config.main import Config, load_config

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def _single_line_execute_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a quick /execute call to its one result line, however slowly a loaded machine runs it."""
    monkeypatch.setattr("mindroom.api.sandbox_runner.EXECUTE_KEEPALIVE_SECONDS", 3600.0)


_TRUSTED_UPSTREAM_AUDIENCE = "mindroom-dashboard"
_TRUSTED_UPSTREAM_ISSUER = "https://issuer.example"
_TRUSTED_UPSTREAM_JWKS_URL = "https://issuer.example/conftest/jwks"


def trusted_upstream_jwt_key() -> rsa.RSAPrivateKey:
    """Return a new RSA key for signing trusted-upstream JWTs."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def trusted_upstream_jwks(private_key: rsa.RSAPrivateKey, kid: str = "test-key") -> dict[str, Any]:
    """Return the JWKS that publishes `private_key`."""
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "use": "sig", "alg": "RS256"}]}


def trusted_upstream_jwt(
    private_key: rsa.RSAPrivateKey,
    *,
    audience: str = _TRUSTED_UPSTREAM_AUDIENCE,
    email: str = "alice@example.com",
    expires_at: datetime | None = None,
    issuer: str = _TRUSTED_UPSTREAM_ISSUER,
    kid: str = "test-key",
    matrix_user_id: str | None = None,
    user_id: str = "user_123",
) -> str:
    """Return a gateway assertion signed by `private_key`."""
    now = datetime.now(UTC)
    claims = {
        "iss": issuer,
        "aud": audience,
        "exp": expires_at or now + timedelta(minutes=5),
        "iat": now,
        "email": email,
        "sub": user_id,
    }
    if matrix_user_id is not None:
        claims["matrix_user_id"] = matrix_user_id
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@cache
def _shared_trusted_upstream_key() -> rsa.RSAPrivateKey:
    return trusted_upstream_jwt_key()


@pytest.fixture(autouse=True)
def _serve_trusted_upstream_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the key `trusted_upstream_headers` signs with at the JWKS URL `use_trusted_upstream_runtime` sets."""
    fetch_data = jwt.PyJWKClient.fetch_data

    def serve(client: jwt.PyJWKClient) -> Any:  # noqa: ANN401
        if client.uri != _TRUSTED_UPSTREAM_JWKS_URL:
            return fetch_data(client)
        return trusted_upstream_jwks(_shared_trusted_upstream_key())

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", serve)


def trusted_upstream_headers(
    *,
    user_id: str = "alice",
    email: str = "alice@example.com",
    matrix_user_id: str = "@alice:example.org",
) -> dict[str, str]:
    """Return the Origin header and a gateway-signed assertion for an app set up by `use_trusted_upstream_runtime`."""
    token = trusted_upstream_jwt(
        _shared_trusted_upstream_key(),
        email=email,
        matrix_user_id=matrix_user_id,
        user_id=user_id,
    )
    return {"Origin": "http://testserver", "X-Trusted-Jwt": token}


def trusted_upstream_env(
    *,
    user_id_claim: str | None = "sub",
    matrix_user_id_claim: str | None = "matrix_user_id",
) -> dict[str, str]:
    """Return the env that turns on trusted-upstream auth accepting `trusted_upstream_jwt` assertions."""
    env = {
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER": "X-Trusted-Jwt",
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL": _TRUSTED_UPSTREAM_JWKS_URL,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE": _TRUSTED_UPSTREAM_AUDIENCE,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER": _TRUSTED_UPSTREAM_ISSUER,
    }
    if user_id_claim is not None:
        env["MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM"] = user_id_claim
    if matrix_user_id_claim is not None:
        env["MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM"] = matrix_user_id_claim
    return env


def use_trusted_upstream_runtime(api_app: object) -> "RuntimePaths":
    """Reinitialize one API app with trusted-upstream auth that accepts `trusted_upstream_headers`."""
    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import main  # noqa: PLC0415

    runtime_paths = main._app_runtime_paths(api_app)
    trusted_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=runtime_paths.config_path,
        storage_path=runtime_paths.storage_root,
        process_env=trusted_upstream_env(),
    )
    main.initialize_api_app(api_app, trusted_runtime_paths)
    return trusted_runtime_paths


@pytest.fixture
def temp_config_file(tmp_path: Path) -> Path:
    """Create a temporary config file for testing."""
    config_dir = tmp_path / "api-runtime"
    config_dir.mkdir()
    config_data = {
        "administrators": ["@owner:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "test_agent": {
                "display_name": "Test Agent",
                "role": "A test agent",
                "tools": ["calculator"],
                "instructions": ["Test instruction"],
                "rooms": ["test_room"],
            },
        },
        "defaults": {"markdown": True},
    }
    temp_path = config_dir / "config.yaml"
    temp_path.write_text(yaml.dump(config_data), encoding="utf-8")
    return temp_path


@pytest.fixture
def test_client(temp_config_file: Path) -> TestClient:
    """Create a test client with mocked config file."""
    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import main  # noqa: PLC0415

    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org", "MATRIX_HOMESERVER": "http://localhost:8008"},
    )
    main.initialize_api_app(main.app, runtime_paths)

    # Force reload of config
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)

    # Create test client
    return TestClient(main.app, base_url="http://localhost")


@pytest.fixture
def calls_on_event_loop(monkeypatch: pytest.MonkeyPatch) -> Callable[[object, str], list[bool]]:
    """Return ``record(module, name)``: wrap ``module.name`` and log, for each call, whether it ran on the event loop."""

    def record(module: object, name: str) -> list[bool]:
        real = getattr(module, name)
        on_loop: list[bool] = []

        def recorded(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop.append(False)
            else:
                on_loop.append(True)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, recorded)
        return on_loop

    return record


def make_api_app(runtime_paths: "RuntimePaths") -> FastAPI:
    """Build one API app with a fresh published snapshot, mirroring main.initialize_api_app."""
    api_app = FastAPI()
    state = config_lifecycle.ensure_app_state(api_app)
    state.api_state = config_lifecycle.ApiState(
        config_lock=threading.Lock(),
        snapshot=config_lifecycle.ApiSnapshot(
            generation=0,
            runtime_paths=runtime_paths,
        ),
    )
    config_lifecycle.register_api_app(api_app)
    return api_app


def request_for(api_app: FastAPI) -> Request:
    """Return a dashboard GET request against `api_app`."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/config",
            "query_string": b"",
            "headers": [],
            "app": api_app,
        },
    )


def api_snapshot(api_app: FastAPI) -> config_lifecycle.ApiSnapshot:
    """Return the snapshot `api_app` currently publishes."""
    return config_lifecycle.require_api_state(api_app).snapshot


def publish_api_config(api_app: FastAPI, config: Config) -> None:
    """Serve `config` as the app's committed config without loading or committing a file."""
    snapshot = api_snapshot(api_app)
    snapshot.runtime_config = config
    snapshot.config_load_result = ConfigLoadResult(success=True)


def save_config(request: Request, payload: dict[str, Any], *, expected_generation: int | None = None) -> int:
    """Commit a dashboard structured save and return its generation."""
    build = partial(ConfigCommit.from_payload, payload)
    return config_lifecycle.commit_dashboard_config(request, build, expected_generation=expected_generation).generation


def save_edited_config(request: Request, edit: Callable[[dict[str, Any]], object]) -> int:
    """Save the config the request pinned after `edit` changes its authored payload, as the dashboard editor does."""
    config_lifecycle.bind_current_request_snapshot(request)
    config, _ = config_lifecycle.read_committed_runtime_config(request)
    payload = config.authored_model_dump()
    edit(payload)
    return save_config(request, payload)


def save_raw_config(request: Request, source: str, *, expected_generation: int | None = None) -> int:
    """Commit a dashboard raw save and return its generation."""
    build = partial(ConfigCommit.from_source, source)
    return config_lifecycle.commit_dashboard_config(request, build, expected_generation=expected_generation).generation


def commit_tool_change(
    payload: dict[str, Any],
    runtime_paths: "RuntimePaths",
    *,
    loaded: Config | None = None,
) -> Config:
    """Commit a change the way a tool does: validated, and refused when the file changed since `loaded` was read.

    Without `loaded`, the tool loads the file right before committing.
    """
    if loaded is None:
        loaded = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    commit = ConfigCommit.from_payload(payload, runtime_paths)
    config_lifecycle.commit_config(runtime_paths, commit, loaded=loaded)
    return commit.config


PLATFORM_SSO_SECRET = "instance-platform-sso-secret-for-tests-0001"  # noqa: S105


def set_platform_auth(
    *,
    valid_tokens: set[str],
    platform_sso_url: str = "https://platform.example.com/instance-sso/authorize",
    public_url: str | None = None,
    account_id: str | None = None,
    user_id: str = "user-123",
    platform_sso_secret: str | None = PLATFORM_SSO_SECRET,
) -> None:
    """Configure the API module for platform-managed login tests."""
    from mindroom.api import auth, main  # noqa: PLC0415

    class _FakeUser:
        id = user_id
        email = "user@example.com"

    class _FakeResponse:
        user = _FakeUser()

    class _FakeAuth:
        @staticmethod
        def get_user(token: str) -> _FakeResponse | None:
            if token not in valid_tokens:
                return None
            return _FakeResponse()

    class _FakeClient:
        auth = _FakeAuth()

    config_lifecycle.app_state(main.app).api_auth_state = auth.ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=auth._ApiAuthSettings(
            supabase_url="https://supabase.example.com",
            supabase_anon_key="anon-key",
            account_id=account_id,
            mindroom_api_key=None,
            public_url=public_url,
            platform_sso_url=platform_sso_url,
            platform_sso_secret=platform_sso_secret,
        ),
        supabase_auth=_FakeClient(),
    )
