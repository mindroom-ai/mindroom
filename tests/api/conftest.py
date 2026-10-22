"""Pytest configuration and fixtures for dashboard backend tests."""

# Import the app after we can mock the config path
import asyncio
import json
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from mindroom.api import config_lifecycle

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def _single_line_execute_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a quick /execute call to its one result line, however slowly a loaded machine runs it."""
    monkeypatch.setattr("mindroom.api.sandbox_runner.EXECUTE_KEEPALIVE_SECONDS", 3600.0)


_TRUSTED_UPSTREAM_AUDIENCE = "mindroom-dashboard"
_TRUSTED_UPSTREAM_ISSUER = "https://issuer.example"
_TRUSTED_UPSTREAM_JWKS_URL = "https://issuer.example/conftest/jwks"


@cache
def _trusted_upstream_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _serve_trusted_upstream_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the key `trusted_upstream_headers` signs with at the JWKS URL `use_trusted_upstream_runtime` sets."""
    fetch_data = jwt.PyJWKClient.fetch_data

    def serve(client: jwt.PyJWKClient) -> Any:  # noqa: ANN401
        if client.uri != _TRUSTED_UPSTREAM_JWKS_URL:
            return fetch_data(client)
        jwk = json.loads(RSAAlgorithm.to_jwk(_trusted_upstream_key().public_key()))
        return {"keys": [{**jwk, "kid": "conftest-key", "use": "sig", "alg": "RS256"}]}

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", serve)


def trusted_upstream_headers(
    *,
    user_id: str = "alice",
    email: str = "alice@example.com",
    matrix_user_id: str = "@alice:example.org",
) -> dict[str, str]:
    """Return the Origin header and a gateway-signed assertion for an app set up by `use_trusted_upstream_runtime`."""
    now = datetime.now(UTC)
    claims = {
        "iss": _TRUSTED_UPSTREAM_ISSUER,
        "aud": _TRUSTED_UPSTREAM_AUDIENCE,
        "exp": now + timedelta(minutes=5),
        "email": email,
        "sub": user_id,
        "matrix_user_id": matrix_user_id,
    }
    token = jwt.encode(claims, _trusted_upstream_key(), algorithm="RS256", headers={"kid": "conftest-key"})
    return {"Origin": "http://testserver", "X-Trusted-Jwt": token}


def trusted_upstream_env() -> dict[str, str]:
    """Return the env that turns on trusted-upstream auth accepting `trusted_upstream_headers`."""
    return {
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER": "X-Trusted-Jwt",
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL": _TRUSTED_UPSTREAM_JWKS_URL,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE": _TRUSTED_UPSTREAM_AUDIENCE,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER": _TRUSTED_UPSTREAM_ISSUER,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM": "sub",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM": "matrix_user_id",
    }


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
def temp_config_file(tmp_path: Path) -> Generator[Path, None, None]:
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

    yield temp_path

    # Cleanup
    temp_path.unlink(missing_ok=True)


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
def sample_agent_data() -> dict[str, Any]:
    """Sample agent data for testing."""
    return {
        "display_name": "New Test Agent",
        "role": "A new test agent for testing",
        "tools": ["file", "shell"],
        "instructions": ["Do something", "Do something else"],
        "rooms": ["lobby", "dev"],
    }


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
