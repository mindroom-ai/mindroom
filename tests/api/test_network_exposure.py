"""Tests for the dashboard's network exposure guards."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mindroom import constants
from mindroom.api import network_exposure

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path, **process_env: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return constants.resolve_primary_runtime_paths(config_path=config_path, process_env=process_env)


def _guarded_client(runtime_paths: RuntimePaths, handled: list[str]) -> TestClient:
    app = FastAPI()

    @app.post("/api/config/save")
    async def save() -> dict[str, bool]:
        handled.append("save")
        return {"success": True}

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        handled.append("health")
        return {"status": "healthy"}

    return TestClient(network_exposure.DashboardHostGuard(app, runtime_paths), base_url="http://localhost:8765")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost:8765", 200),
        ("127.0.0.1:8765", 200),
        ("[::1]:8765", 200),
        ("mindroom.localhost:8765", 200),
        ("attacker.example:8765", 400),
        ("192.168.1.10:8765", 400),
        ("dashboard.example.org", 400),
    ],
)
def test_open_access_answers_only_expected_hosts(tmp_path: Path, host: str, expected: int) -> None:
    """A rebound attacker host must never reach an unauthenticated dashboard."""
    handled: list[str] = []
    client = _guarded_client(_runtime_paths(tmp_path), handled)

    response = client.post("/api/config/save", json={}, headers={"Host": host})

    assert response.status_code == expected
    assert handled == (["save"] if expected == 200 else [])


def test_public_url_host_is_accepted(tmp_path: Path) -> None:
    """The configured public host is how operators reach a hosted dashboard."""
    handled: list[str] = []
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_PUBLIC_URL="https://dashboard.example.org")
    client = _guarded_client(runtime_paths, handled)

    response = client.post("/api/config/save", json={}, headers={"Host": "dashboard.example.org"})

    assert response.status_code == 200
    assert handled == ["save"]


@pytest.mark.parametrize("configured", ["mindroom.internal", "other.example, mindroom.internal", "*"])
def test_configured_extra_hosts_are_accepted(tmp_path: Path, configured: str) -> None:
    """Operators can name the extra hosts their deployment answers."""
    handled: list[str] = []
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_DASHBOARD_ALLOWED_HOSTS=configured)
    client = _guarded_client(runtime_paths, handled)

    response = client.post("/api/config/save", json={}, headers={"Host": "mindroom.internal:8765"})

    assert response.status_code == 200
    assert handled == ["save"]


def test_websocket_handshake_is_rejected_for_unexpected_hosts(tmp_path: Path) -> None:
    """A rebound page must not open a socket to an unauthenticated dashboard either."""
    handled: list[str] = []
    app = FastAPI()

    @app.websocket("/api/computers/ws")
    async def socket(websocket: WebSocket) -> None:
        handled.append("socket")
        await websocket.accept()
        await websocket.close()

    client = TestClient(
        network_exposure.DashboardHostGuard(app, _runtime_paths(tmp_path)),
        base_url="http://localhost:8765",
    )

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/computers/ws", headers={"Host": "attacker.example:8765"}),
    ):
        pass  # pragma: no cover - the handshake is refused before the body runs.

    assert handled == []

    with client.websocket_connect("/api/computers/ws", headers={"Host": "localhost:8765"}):
        pass
    assert handled == ["socket"]


def test_missing_host_header_is_rejected(tmp_path: Path) -> None:
    """A request that names no host cannot be matched against the allow-list."""
    handled: list[str] = []
    client = _guarded_client(_runtime_paths(tmp_path), handled)

    response = client.post("/api/config/save", json={}, headers={"Host": ""})

    assert response.status_code == 400
    assert handled == []


def test_probe_paths_stay_reachable_under_any_host(tmp_path: Path) -> None:
    """Schedulers probe the runtime by their own routable address."""
    handled: list[str] = []
    client = _guarded_client(_runtime_paths(tmp_path), handled)

    response = client.get("/api/health", headers={"Host": "10.1.2.3:8765"})

    assert response.status_code == 200
    assert handled == ["health"]


def test_probe_exemption_does_not_cover_browser_requests(tmp_path: Path) -> None:
    """A rebound page must not read runtime status through the probe exemption."""
    handled: list[str] = []
    client = _guarded_client(_runtime_paths(tmp_path), handled)

    response = client.get(
        "/api/health",
        headers={"Host": "attacker.example:8765", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 400
    assert handled == []


@pytest.mark.parametrize(
    "process_env",
    [
        {"MINDROOM_API_KEY": "test-key"},
        {"SUPABASE_URL": "https://project.supabase.co", "SUPABASE_ANON_KEY": "anon-key"},
        {"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"},
    ],
)
def test_authenticated_dashboards_keep_answering_every_host(tmp_path: Path, process_env: dict[str, str]) -> None:
    """Credentialed deployments sit behind proxies that route arbitrary host names."""
    handled: list[str] = []
    runtime_paths = _runtime_paths(tmp_path, **process_env)
    assert not network_exposure._dashboard_open_access(runtime_paths)
    client = _guarded_client(runtime_paths, handled)

    response = client.post("/api/config/save", json={}, headers={"Host": "anything.example"})

    assert response.status_code == 200
    assert handled == ["save"]


def test_open_access_is_detected_without_configured_auth(tmp_path: Path) -> None:
    """An empty API key is the documented open-access default, not a credential."""
    assert network_exposure._dashboard_open_access(_runtime_paths(tmp_path))
    assert network_exposure._dashboard_open_access(_runtime_paths(tmp_path, MINDROOM_API_KEY="  "))
    assert network_exposure._dashboard_open_access(_runtime_paths(tmp_path, SUPABASE_URL="https://project.supabase.co"))


@pytest.mark.parametrize(
    ("host", "event"),
    [
        ("127.0.0.1", "dashboard_unauthenticated"),
        ("0.0.0.0", "dashboard_unauthenticated_non_loopback_bind"),  # noqa: S104
    ],
)
def test_unauthenticated_bind_is_reported_at_startup(tmp_path: Path, host: str, event: str) -> None:
    """Operators must see that the dashboard serves requests without a credential."""
    with patch.object(network_exposure.logger, "warning") as warning:
        network_exposure.warn_unauthenticated_dashboard_exposure(_runtime_paths(tmp_path), host=host)

    assert warning.call_args.args[0] == event


def test_authenticated_bind_is_not_warned_about(tmp_path: Path) -> None:
    """A configured API key is the expected deployment, not a warning."""
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_API_KEY="test-key")

    with patch.object(network_exposure.logger, "warning") as warning:
        network_exposure.warn_unauthenticated_dashboard_exposure(runtime_paths, host="0.0.0.0")  # noqa: S104

    warning.assert_not_called()
