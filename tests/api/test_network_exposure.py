"""Tests for the Host allow-list of a dashboard that needs no credential."""

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

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104


def _runtime_paths(tmp_path: Path, **process_env: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return constants.resolve_primary_runtime_paths(config_path=config_path, process_env=process_env)


def _app(handled: list[str]) -> FastAPI:
    app = FastAPI()

    @app.post("/api/config/save")
    async def save() -> dict[str, bool]:
        handled.append("save")
        return {"success": True}

    @app.websocket("/api/computers/ws")
    async def socket(websocket: WebSocket) -> None:
        handled.append("socket")
        await websocket.accept()
        await websocket.close()

    return app


def _client(runtime_paths: RuntimePaths, handled: list[str]) -> TestClient:
    guarded = network_exposure.guard_unauthenticated_dashboard(_app(handled), runtime_paths, host="127.0.0.1")
    return TestClient(guarded)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("localhost:8765", True),
        ("LOCALHOST.:8765", True),
        ("myapp.localhost", True),
        ("127.0.0.1:8765", True),
        ("[::1]:8765", True),
        # An address literal cannot be a rebinding target, so pod IPs and LAN addresses work.
        ("10.1.2.3:8765", True),
        ("attacker.example:8765", False),
        ("localhost:8765,attacker.example", False),
        ("", False),
    ],
)
def test_unauthenticated_dashboard_answers_only_its_own_hosts(tmp_path: Path, host: str, *, allowed: bool) -> None:
    """A DNS-rebound attacker host must never reach an unauthenticated dashboard."""
    handled: list[str] = []

    response = _client(_runtime_paths(tmp_path), handled).post("/api/config/save", headers={"Host": host})

    assert response.status_code == (200 if allowed else 400)
    assert handled == (["save"] if allowed else [])


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("MINDROOM_PUBLIC_URL", "https://dashboard.example.org"),
        ("MINDROOM_SCRIPT_GATEWAY_URL", "http://dashboard.example.org:8765/api/script-gateway"),
        ("MINDROOM_DASHBOARD_ALLOWED_HOSTS", "other.example, Dashboard.Example.org"),
    ],
)
def test_configured_hosts_are_answered(tmp_path: Path, env_name: str, value: str) -> None:
    """The runtime's own URLs and explicitly named hosts are expected hosts."""
    handled: list[str] = []
    client = _client(_runtime_paths(tmp_path, **{env_name: value}), handled)

    response = client.post("/api/config/save", headers={"Host": "dashboard.example.org"})

    assert response.status_code == 200
    assert handled == ["save"]


def test_websocket_handshake_is_refused_for_unexpected_hosts(tmp_path: Path) -> None:
    """A rebound page must not open a socket to an unauthenticated dashboard either."""
    handled: list[str] = []
    client = _client(_runtime_paths(tmp_path), handled)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/computers/ws", headers={"Host": "attacker.example"}),
    ):
        pass
    with client.websocket_connect("/api/computers/ws", headers={"Host": "localhost"}):
        pass

    assert handled == ["socket"]


def test_unauthenticated_bind_is_guarded_and_warned_about(tmp_path: Path) -> None:
    """Operators must see that the dashboard serves requests without a credential."""
    app = _app([])

    with patch.object(network_exposure.logger, "warning") as warning:
        served = network_exposure.guard_unauthenticated_dashboard(app, _runtime_paths(tmp_path), host=_ALL_INTERFACES)

    assert served is not app
    assert warning.call_args.kwargs["bind_host"] == _ALL_INTERFACES


@pytest.mark.parametrize(
    "process_env",
    [
        {"MINDROOM_API_KEY": "test-key"},
        {"SUPABASE_URL": "https://project.supabase.co", "SUPABASE_ANON_KEY": "anon-key"},
        {"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true"},
    ],
)
def test_authenticated_dashboards_are_served_unchanged(tmp_path: Path, process_env: dict[str, str]) -> None:
    """Credentialed deployments sit behind proxies that route arbitrary host names."""
    app = _app([])

    with patch.object(network_exposure.logger, "warning") as warning:
        served = network_exposure.guard_unauthenticated_dashboard(
            app,
            _runtime_paths(tmp_path, **process_env),
            host=_ALL_INTERFACES,
        )

    assert served is app
    warning.assert_not_called()
