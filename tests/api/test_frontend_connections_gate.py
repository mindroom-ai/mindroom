"""Static serving of the Connections bundle and its Agent Keys page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, frontend, main
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TRUSTED_HEADERS = {"X-Trusted-User": "alice"}


@pytest.fixture
def signed_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Sign a real upstream JWT for Alice while keeping JWKS retrieval local."""
    private_key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(private_key))
    return {
        **_TRUSTED_HEADERS,
        "X-Trusted-Email": "alice@example.com",
        "X-Trusted-Jwt": _trusted_upstream_jwt(
            private_key,
            matrix_user_id="@alice:example.org",
            user_id="alice",
        ),
    }


@pytest.fixture
def gate_client(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
    tmp_path: Path,
) -> Callable[..., TestClient]:
    """Build an app serving a fake dashboard dist with a dedicated connections bundle."""
    dist = tmp_path / "dist"
    portal = dist / "connections"
    (portal / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("administrator dashboard")
    (portal / "index.html").write_text("connections bundle")
    (portal / "assets" / "portal.js").write_text("portal asset")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: dist)

    def client(*, trusted_upstream: bool, broker_on: bool = True, require_jwt: bool = False) -> TestClient:
        env = {
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true" if trusted_upstream else "false",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
        }
        if require_jwt:
            env.update(_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"))
        if broker_on:
            env["MINDROOM_EGRESS_BROKER_PORT"] = "8768"
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=temp_config_file,
            storage_path=tmp_path / "storage",
            process_env=env,
        )
        app = FastAPI()
        main.initialize_api_app(app, runtime_paths)
        config_lifecycle.load_config_into_app(runtime_paths, app)
        app.include_router(frontend.router)
        return TestClient(app, base_url="http://localhost")

    return client


_PORTAL_PATHS = ["/connections", "/connections/", "/connections/nested", "/connections/egressx"]
_EGRESS_PATHS = ["/connections/egress", "/connections/egress/", "/connections/egress/agents"]


@pytest.mark.parametrize("path", [*_PORTAL_PATHS, *_EGRESS_PATHS, "/connections/assets/portal.js"])
@pytest.mark.parametrize("trusted_upstream", [False, True])
def test_connections_bundle_is_served_with_no_settings(
    path: str,
    trusted_upstream: bool,
    gate_client: Callable[..., TestClient],
) -> None:
    """The pages are static shells that sign themselves in from MindRoom Chat, so they load before any sign-in."""
    response = gate_client(trusted_upstream=trusted_upstream).get(path, headers=_TRUSTED_HEADERS)
    assert response.status_code == 200
    assert response.text in {"connections bundle", "portal asset"}


@pytest.mark.parametrize("path", _EGRESS_PATHS)
def test_signed_trusted_upstream_serves_egress_page(
    path: str,
    gate_client: Callable[..., TestClient],
    signed_headers: dict[str, str],
) -> None:
    """Signed trusted users load the Agent Keys page too."""
    response = gate_client(trusted_upstream=True, require_jwt=True).get(path, headers=signed_headers)
    assert response.status_code == 200
    assert response.text == "connections bundle"


@pytest.mark.parametrize("path", [*_PORTAL_PATHS, *_EGRESS_PATHS])
def test_agent_keys_page_is_absent_while_the_broker_is_off(
    path: str,
    gate_client: Callable[..., TestClient],
) -> None:
    """Without a broker port only the Agent Keys page is a 404; the Agents page still loads."""
    response = gate_client(trusted_upstream=True, broker_on=False).get(path, headers=_TRUSTED_HEADERS)
    if path in _EGRESS_PATHS:
        assert response.status_code == 404
        assert response.json()["detail"] == "Agent Keys are not enabled"
    else:
        assert response.status_code == 200
