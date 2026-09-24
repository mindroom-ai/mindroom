"""Tests for the browser guards on requests the API serves without a credential."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.api.open_access import OpenAccessGuard

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALLOWED_HOSTS_ENV = "MINDROOM_DASHBOARD_ALLOWED_HOSTS"
_TRUSTED_UPSTREAM_ENV = {
    "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
    "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
}


def _runtime_paths(tmp_path: Path, config_path: Path | None = None, **process_env: str) -> RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=config_path or tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env,
    )


def _guarded_client(tmp_path: Path, handled: list[str], **process_env: str) -> TestClient:
    """Return a client for an app whose routes need no credential, so only the guard can refuse."""
    app = FastAPI()

    @app.get("/probe")
    async def read() -> dict[str, bool]:
        handled.append("read")
        return {"ok": True}

    @app.post("/probe")
    async def write() -> dict[str, bool]:
        handled.append("write")
        return {"ok": True}

    @app.websocket("/socket")
    async def socket(websocket: WebSocket) -> None:
        handled.append("socket")
        await websocket.accept()
        await websocket.close()

    main.initialize_api_app(app, _runtime_paths(tmp_path, **process_env))
    app.add_middleware(OpenAccessGuard)
    return TestClient(app, base_url="http://localhost")


def _dashboard_client(temp_config_file: Path, **process_env: str) -> TestClient:
    runtime_paths = _runtime_paths(temp_config_file.parent, config_path=temp_config_file, **process_env)
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    return TestClient(main.app, base_url="http://localhost")


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("localhost:8765", True),
        ("LOCALHOST.:8765", True),
        ("dashboard.localhost", True),
        ("127.0.0.1:8765", True),
        ("[::1]:8765", True),
        # A page names an address only when it was served from that address, so LAN and pod IPs work.
        ("192.168.1.20:8765", True),
        ("[fd00::20]", True),
        ("attacker.example:8765", False),
        ("localhost.attacker.example", False),
        ("localhost:8765,attacker.example", False),
        ("", False),
    ],
)
def test_open_dashboard_answers_only_its_own_hosts(tmp_path: Path, host: str, *, allowed: bool) -> None:
    """A DNS-rebound attacker name must not reach a dashboard that needs no credential."""
    handled: list[str] = []

    response = _guarded_client(tmp_path, handled).get("/probe", headers={"Host": host})

    assert response.status_code == (200 if allowed else 400), response.text
    assert handled == (["read"] if allowed else [])
    if not allowed:
        assert ALLOWED_HOSTS_ENV in response.json()["detail"]


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("MINDROOM_PUBLIC_URL", "https://Dashboard.example.org/mindroom"),
        ("MINDROOM_URL", "http://dashboard.example.org:8765"),
        ("MINDROOM_SCRIPT_GATEWAY_URL", "http://dashboard.example.org:8765/api/script-gateway"),
        (ALLOWED_HOSTS_ENV, "other.example, Dashboard.Example.org:8765"),
    ],
)
def test_open_dashboard_answers_configured_hosts(tmp_path: Path, env_name: str, value: str) -> None:
    """The runtime's own URLs and explicitly named hosts are hosts it answers, and pages they serve."""
    handled: list[str] = []
    client = _guarded_client(tmp_path, handled, **{env_name: value})

    served = client.post("/probe", headers={"Host": "dashboard.example.org"})
    from_page = client.post("/probe", headers={"Origin": "https://dashboard.example.org"})

    assert (served.status_code, from_page.status_code) == (200, 200)
    assert handled == ["write", "write"]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # API clients send no Origin.
        ({}, 200),
        ({"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}, 200),
        # The frontend dev server calls the dashboard from another loopback port.
        ({"Origin": "http://localhost:3003", "Sec-Fetch-Site": "same-site"}, 200),
        ({"Origin": "http://127.0.0.1:5173"}, 200),
        ({"Origin": "https://attacker.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://[bad"}, 403),
        # Another machine's page is not this dashboard's page, even when it is served from an address.
        ({"Origin": "http://192.168.1.99"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}, 403),
    ],
)
def test_open_dashboard_refuses_changes_from_other_sites(
    tmp_path: Path,
    headers: dict[str, str],
    expected: int,
) -> None:
    """A cross-site form or fetch must not change a dashboard that needs no credential."""
    handled: list[str] = []

    response = _guarded_client(tmp_path, handled).post("/probe", headers=headers)

    assert response.status_code == expected, response.text
    assert handled == (["write"] if expected == 200 else [])


def test_open_dashboard_refuses_cross_origin_reads_but_not_links(tmp_path: Path) -> None:
    """Another site may link to the dashboard, but may not read it with a cross-origin fetch."""
    handled: list[str] = []
    client = _guarded_client(tmp_path, handled)

    cross_origin_read = client.get("/probe", headers={"Origin": "https://attacker.example"})
    followed_link = client.get("/probe", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})

    assert (cross_origin_read.status_code, followed_link.status_code) == (403, 200)
    assert handled == ["read"]


def test_open_dashboard_origin_may_be_allowed_by_name(tmp_path: Path) -> None:
    """A separately hosted frontend works once its host is named."""
    unnamed: list[str] = []
    named: list[str] = []
    headers = {"Origin": "https://ui.example.org"}

    refused = _guarded_client(tmp_path, unnamed).post("/probe", headers=headers)
    allowed = _guarded_client(tmp_path, named, **{ALLOWED_HOSTS_ENV: "ui.example.org"}).post("/probe", headers=headers)

    assert (refused.status_code, allowed.status_code) == (403, 200)
    assert ALLOWED_HOSTS_ENV in refused.json()["detail"]
    assert (unnamed, named) == ([], ["write"])


@pytest.mark.parametrize(
    ("headers", "status_code"),
    [
        ({"Host": "attacker.example", "Origin": "http://attacker.example"}, 400),
        ({"Host": "localhost", "Origin": "https://attacker.example"}, 403),
        ({"Host": "localhost", "Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}, 403),
    ],
)
def test_open_dashboard_refuses_rebound_and_cross_site_websockets(
    tmp_path: Path,
    headers: dict[str, str],
    status_code: int,
) -> None:
    """Browsers open WebSockets to any origin without CORS, so the handshake is guarded too."""
    handled: list[str] = []
    client = _guarded_client(tmp_path, handled)

    with pytest.raises(WebSocketDenialResponse) as denial, client.websocket_connect("/socket", headers=headers):
        pass
    with client.websocket_connect("/socket", headers={"Host": "localhost", "Origin": "http://localhost"}):
        pass

    assert denial.value.status_code == status_code
    assert handled == ["socket"]


@pytest.mark.parametrize("process_env", [{"MINDROOM_API_KEY": "test-key"}, _TRUSTED_UPSTREAM_ENV])
def test_authenticated_dashboard_is_not_guarded(tmp_path: Path, process_env: dict[str, str]) -> None:
    """Credentialed deployments sit behind proxies that route arbitrary names and origins."""
    handled: list[str] = []
    client = _guarded_client(tmp_path, handled, **process_env)

    response = client.post("/probe", headers={"Host": "mindroom.internal", "Origin": "https://ui.example.org"})
    with client.websocket_connect("/socket", headers={"Host": "mindroom.internal"}):
        pass

    assert response.status_code == 200
    assert handled == ["write", "socket"]


def test_guard_follows_the_current_runtime(tmp_path: Path) -> None:
    """Removing the key at runtime guards the next request, and adding one lifts the guard."""
    handled: list[str] = []
    client = _guarded_client(tmp_path, handled, MINDROOM_API_KEY="test-key")
    rebound = {"Host": "attacker.example"}

    keyed = client.get("/probe", headers=rebound)
    main.initialize_api_app(client.app, _runtime_paths(tmp_path))
    opened = client.get("/probe", headers=rebound)
    main.initialize_api_app(client.app, _runtime_paths(tmp_path, **{ALLOWED_HOSTS_ENV: "attacker.example"}))
    named = client.get("/probe", headers=rebound)

    assert (keyed.status_code, opened.status_code, named.status_code) == (200, 400, 200)
    assert handled == ["read", "read"]


def test_exported_dashboard_refuses_rebound_hosts_and_cross_origin_reads(temp_config_file: Path) -> None:
    """The served app guards administrator routes, and a wildcard CORS opt-in cannot expose them."""
    client = _dashboard_client(temp_config_file, MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS="true")

    local = client.get("/api/config/raw")
    rebound = client.get("/api/config/raw", headers={"Host": "attacker.example"})
    cross_origin = client.get("/api/config/raw", headers={"Origin": "https://attacker.example"})
    preflight = client.options(
        "/api/config/raw",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
    )

    assert local.status_code == 200, local.text
    assert (rebound.status_code, cross_origin.status_code, preflight.status_code) == (400, 403, 403)
    assert "access-control-allow-origin" not in cross_origin.headers


def test_exported_dashboard_serves_keyed_requests_on_any_host(temp_config_file: Path) -> None:
    """With a dashboard key, the key authorizes a request, not the name it was sent to."""
    client = _dashboard_client(temp_config_file, MINDROOM_API_KEY="test-key")
    foreign = {"Host": "mindroom.internal"}

    keyed = client.get("/api/config/raw", headers={**foreign, "Authorization": "Bearer test-key"})
    unkeyed = client.get("/api/config/raw", headers=foreign)

    assert (keyed.status_code, unkeyed.status_code) == (200, 401)


def test_keyed_dashboard_still_guards_unauthenticated_openai_api(temp_config_file: Path) -> None:
    """An unauthenticated `/v1` beside a keyed dashboard is still served without a credential."""
    client = _dashboard_client(
        temp_config_file,
        MINDROOM_API_KEY="test-key",
        OPENAI_COMPAT_ALLOW_UNAUTHENTICATED="true",
    )

    local = client.get("/v1/models")
    rebound = client.get("/v1/models", headers={"Host": "attacker.example"})
    keyed_dashboard = client.get(
        "/api/config/raw",
        headers={"Host": "attacker.example", "Authorization": "Bearer test-key"},
    )

    assert (local.status_code, rebound.status_code, keyed_dashboard.status_code) == (200, 400, 200)
    assert ALLOWED_HOSTS_ENV in rebound.json()["error"]["message"]
