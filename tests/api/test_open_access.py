"""Tests for the browser guards on requests the API serves without a credential."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.api.open_access import open_access_rejection

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALLOWED_HOSTS_ENV = "MINDROOM_DASHBOARD_ALLOWED_HOSTS"


def _runtime_paths(tmp_path: Path, config_path: Path | None = None, **process_env: str) -> RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=config_path or tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env,
    )


def _status(tmp_path: Path, headers: dict[str, str], method: str = "GET", **process_env: str) -> int:
    rejection = open_access_rejection(Headers(headers), method, _runtime_paths(tmp_path, **process_env))
    return 200 if rejection is None else rejection.status_code


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
def test_only_the_runtimes_own_hosts_are_answered(tmp_path: Path, host: str, *, allowed: bool) -> None:
    """A DNS-rebound attacker name must not reach a request that needs no credential."""
    assert _status(tmp_path, {"host": host}) == (200 if allowed else 400)


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("MINDROOM_PUBLIC_URL", "https://Dashboard.example.org/mindroom"),
        ("MINDROOM_BASE_URL", "https://dashboard.example.org"),
        ("MINDROOM_URL", "http://dashboard.example.org:8765"),
        ("MINDROOM_SCRIPT_GATEWAY_URL", "http://dashboard.example.org:8765/api/script-gateway"),
        (ALLOWED_HOSTS_ENV, "other.example, Dashboard.Example.org:8765"),
    ],
)
def test_configured_hosts_are_answered_and_may_send_changes(tmp_path: Path, env_name: str, value: str) -> None:
    """The runtime's own URLs and explicitly named hosts are hosts it answers, and pages it serves."""
    env = {env_name: value}
    served = _status(tmp_path, {"host": "dashboard.example.org"}, **env)
    from_page = _status(tmp_path, {"host": "localhost", "origin": "https://dashboard.example.org"}, "POST", **env)

    assert (served, from_page) == (200, 200)
    assert _status(tmp_path, {"host": "localhost", "origin": "https://dashboard.example.org"}, "POST") == 403


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # API clients send no Origin.
        ({}, 200),
        ({"origin": "http://localhost", "sec-fetch-site": "same-origin"}, 200),
        # The frontend dev server calls the dashboard from another loopback port.
        ({"origin": "http://localhost:3003", "sec-fetch-site": "same-site"}, 200),
        ({"origin": "http://127.0.0.1:5173"}, 200),
        ({"origin": "https://attacker.example"}, 403),
        ({"origin": "null"}, 403),
        ({"origin": "http://[bad"}, 403),
        # Another machine's page is not this dashboard's page, even when it is served from an address.
        ({"origin": "http://192.168.1.99"}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
        ({"origin": "http://localhost", "sec-fetch-site": "cross-site"}, 403),
    ],
)
def test_changes_from_other_sites_are_refused(tmp_path: Path, headers: dict[str, str], expected: int) -> None:
    """A cross-site form or fetch must not change anything that needs no credential."""
    assert _status(tmp_path, {"host": "localhost", **headers}, "POST") == expected


def test_cross_origin_reads_are_refused_but_links_are_followed(tmp_path: Path) -> None:
    """Another site may link to the dashboard, but may not read it with a cross-origin fetch."""
    cross_origin_read = _status(tmp_path, {"host": "localhost", "origin": "https://attacker.example"})
    followed_link = _status(
        tmp_path,
        {"host": "localhost", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"},
    )

    assert (cross_origin_read, followed_link) == (403, 200)


def test_open_dashboard_refuses_rebound_hosts_and_cross_origin_reads(temp_config_file: Path) -> None:
    """Dashboard routes and pages are guarded, and a wildcard CORS opt-in cannot expose them."""
    client = _dashboard_client(temp_config_file, MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS="true")
    rebound = {"Host": "attacker.example"}

    local = client.get("/api/config/raw")
    rebound_api = client.get("/api/config/raw", headers=rebound)
    rebound_change = client.post("/api/config/load", headers=rebound)
    rebound_page = client.get("/", headers=rebound)
    cross_origin = client.get("/api/config/raw", headers={"Origin": "https://attacker.example"})

    assert local.status_code == 200, local.text
    statuses = [response.status_code for response in (rebound_api, rebound_change, rebound_page, cross_origin)]
    assert statuses == [400, 400, 400, 403]
    assert ALLOWED_HOSTS_ENV in rebound_page.json()["detail"]


def test_open_dashboard_leaves_self_authenticated_routes_alone(temp_config_file: Path) -> None:
    """Probes, keyed `/v1`, and computer sessions carry their own authority, so any host and allowed origin work."""
    chat_origin = "https://chat.example.org"
    client = _dashboard_client(
        temp_config_file,
        OPENAI_COMPAT_API_KEYS="k1",
        MINDROOM_COMPUTER_ALLOWED_ORIGINS=f'["{chat_origin}"]',
    )
    service_name = {"Host": "mindroom:8765"}

    health = client.get("/api/health", headers=service_name)
    keyed_openai = client.get("/v1/models", headers={**service_name, "Authorization": "Bearer k1"})
    computer_preflight = client.options(
        "/api/computers/sessions",
        headers={
            "Host": "mindroom.example.org",
            "Origin": chat_origin,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert (health.status_code, keyed_openai.status_code, computer_preflight.status_code) == (200, 200, 200)
    assert computer_preflight.headers["access-control-allow-origin"] == chat_origin


def test_keyed_dashboard_serves_any_host(temp_config_file: Path) -> None:
    """With a dashboard key, the key authorizes a request, not the name it was sent to."""
    client = _dashboard_client(temp_config_file, MINDROOM_API_KEY="test-key")
    foreign = {"Host": "mindroom.internal"}

    keyed = client.get("/api/config/raw", headers={**foreign, "Authorization": "Bearer test-key"})
    unkeyed = client.get("/api/config/raw", headers=foreign)

    assert (keyed.status_code, unkeyed.status_code) == (200, 401)


def test_guard_follows_the_current_runtime(temp_config_file: Path) -> None:
    """Removing the key at runtime guards the next request, and naming the host lifts the guard."""
    rebound = {"Host": "attacker.example"}
    client = _dashboard_client(temp_config_file, MINDROOM_API_KEY="test-key")
    keyed = client.get("/api/config/raw", headers={**rebound, "Authorization": "Bearer test-key"})
    opened = _dashboard_client(temp_config_file).get("/api/config/raw", headers=rebound)
    named = _dashboard_client(temp_config_file, **{ALLOWED_HOSTS_ENV: "attacker.example"}).get(
        "/api/config/raw",
        headers=rebound,
    )

    assert (keyed.status_code, opened.status_code, named.status_code) == (200, 400, 200)


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
