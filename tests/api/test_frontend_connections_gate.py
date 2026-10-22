"""Static serving of the Connections bundle and its Agent Keys page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, frontend, main
from tests.api.conftest import (
    trusted_upstream_env,
    trusted_upstream_jwks,
    trusted_upstream_jwt,
    trusted_upstream_jwt_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def signed_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Sign a real upstream JWT for Alice while keeping JWKS retrieval local."""
    private_key = trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: trusted_upstream_jwks(private_key))
    return {
        "X-Trusted-Jwt": trusted_upstream_jwt(
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

    def client(
        *,
        trusted_upstream: bool,
        broker_on: bool = True,
        extra_env: dict[str, str] | None = None,
        base_url: str = "http://localhost",
    ) -> TestClient:
        env = trusted_upstream_env() if trusted_upstream else {}
        if broker_on:
            env["MINDROOM_EGRESS_BROKER_PORT"] = "8768"
        env.update(extra_env or {})
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=temp_config_file,
            storage_path=tmp_path / "storage",
            process_env=env,
        )
        app = FastAPI()
        main.initialize_api_app(app, runtime_paths)
        config_lifecycle.load_config_into_app(runtime_paths, app)
        app.include_router(frontend.router)
        return TestClient(app, base_url=base_url, follow_redirects=False)

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
    response = gate_client(trusted_upstream=trusted_upstream).get(path)
    assert response.status_code == 200
    assert response.text in {"connections bundle", "portal asset"}


@pytest.mark.parametrize("path", _EGRESS_PATHS)
def test_signed_trusted_upstream_serves_egress_page(
    path: str,
    gate_client: Callable[..., TestClient],
    signed_headers: dict[str, str],
) -> None:
    """Signed trusted users load the Agent Keys page too."""
    response = gate_client(trusted_upstream=True).get(path, headers=signed_headers)
    assert response.status_code == 200
    assert response.text == "connections bundle"


@pytest.mark.parametrize("path", [*_PORTAL_PATHS, *_EGRESS_PATHS])
def test_agent_keys_page_is_absent_while_the_broker_is_off(
    path: str,
    gate_client: Callable[..., TestClient],
) -> None:
    """Without a broker port only the Agent Keys page is a 404; the Agents page still loads."""
    response = gate_client(trusted_upstream=True, broker_on=False).get(path)
    if path in _EGRESS_PATHS:
        assert response.status_code == 404
        assert response.json()["detail"] == "Agent Keys are not enabled"
    else:
        assert response.status_code == 200


_SEPARATE_DASHBOARD = {
    "MINDROOM_PUBLIC_URL": "https://chat.example.org",
    "MINDROOM_DASHBOARD_URL": "https://admin.example.org",
}


def test_connections_pages_on_the_dashboard_host_move_to_the_public_origin(
    gate_client: Callable[..., TestClient],
    signed_headers: dict[str, str],
) -> None:
    """Connections sign-in, account links, and MCP consent belong to the public origin, so their pages move there."""
    admin = gate_client(trusted_upstream=True, extra_env=_SEPARATE_DASHBOARD, base_url="https://admin.example.org")
    moved = admin.get("/connections/egress", params={"agent": "personal"})
    assert moved.status_code == 307
    assert moved.headers["location"] == "https://chat.example.org/connections/egress?agent=personal"
    dashboard = admin.get("/", headers=signed_headers)
    assert dashboard.status_code == 200
    assert dashboard.text == "administrator dashboard"

    public = gate_client(trusted_upstream=True, extra_env=_SEPARATE_DASHBOARD, base_url="https://chat.example.org")
    served = public.get("/connections")
    assert served.status_code == 200
    assert served.text == "connections bundle"


def test_dashboard_on_another_port_of_the_public_host_moves_only_its_own_requests(
    gate_client: Callable[..., TestClient],
) -> None:
    """Origins that share a host name but not a port are told apart, so the public origin never redirects to itself."""
    env = {"MINDROOM_PUBLIC_URL": "http://localhost:3000", "MINDROOM_DASHBOARD_URL": "http://localhost:8765"}
    public = gate_client(trusted_upstream=True, extra_env=env, base_url="http://localhost:3000")
    assert public.get("/connections").status_code == 200
    dashboard = gate_client(trusted_upstream=True, extra_env=env, base_url="http://localhost:8765")
    moved = dashboard.get("/connections")
    assert moved.status_code == 307
    assert moved.headers["location"] == "http://localhost:3000/connections"


def test_a_dashboard_url_equal_to_the_public_url_moves_nothing(gate_client: Callable[..., TestClient]) -> None:
    """Setting both URLs to one origin keeps Connections pages where they are instead of redirecting to themselves."""
    env = {"MINDROOM_PUBLIC_URL": "https://chat.example.org", "MINDROOM_DASHBOARD_URL": "https://chat.example.org"}
    client = gate_client(trusted_upstream=True, extra_env=env, base_url="https://chat.example.org")
    assert client.get("/connections").status_code == 200
