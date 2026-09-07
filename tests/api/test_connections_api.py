"""Personal portal API isolation and existing OAuth lifecycle integration."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle, main, oauth
from mindroom.oauth import registry as oauth_registry
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import (
    _fake_provider,
    _publish_config,
    _runtime_paths,
    _stored_oauth_credentials,
    _use_runtime_auth_settings,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Serve the real API with two signed users and one private agent."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    env = {
        **_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"),
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MINDROOM_PUBLIC_URL": "https://portal.example.org",
        "TEST_OAUTH_CLIENT_ID": "test-client",
        "TEST_OAUTH_CLIENT_SECRET": "test-secret",
    }
    paths = _runtime_paths(tmp_path, env)
    payload = {
        "administrators": ["@admin:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "personal": {
                "display_name": "Personal Mind",
                "role": "Personal assistant",
                "tools": ["calculator", {"name": "google_drive", "defer": True}],
                "private": {"per": "user_agent"},
                "access": {"users": ["@alice:example.org", "@bob:example.org"]},
            },
        },
    }
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    monkeypatch.setattr(
        oauth_registry,
        "_builtin_oauth_providers",
        lambda: (provider,),
    )
    headers = {
        name: {
            "X-Trusted-User": name,
            "X-Trusted-Jwt": _trusted_upstream_jwt(
                key,
                user_id=name,
                email=f"{name}@example.org",
                matrix_user_id=f"@{name}:example.org",
            ),
            "Origin": "https://portal.example.org",
        }
        for name in ("alice", "bob", "mallory")
    }
    return {
        "client": TestClient(main.app, base_url="https://portal.example.org"),
        "headers": headers,
        "paths": paths,
        "payload": payload,
        "provider": provider,
    }


def test_catalog_includes_deferred_oauth_tools_without_status_calls(
    portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private agent exposes its allowed OAuth tools without dashboard internals."""
    status = AsyncMock(side_effect=AssertionError("Catalog must not load remote status"))
    monkeypatch.setattr(oauth, "status", status)
    response = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    assert response.json()["agent_display_name"] == "Personal Mind"
    assert [item["provider"] for item in response.json()["services"]] == ["google_drive"]
    assert response.json()["services"][0]["tools"] == ["google_drive"]
    assert "credential_service" not in response.text
    assert "no-store" in response.headers["cache-control"]
    status.assert_not_called()


@pytest.mark.parametrize("query", ["agent_name=other", "worker_key=other", "user_id=bob", "execution_scope=user"])
def test_target_overrides_rejected(portal: dict[str, Any], query: str) -> None:
    """The browser cannot choose a credential owner or scope."""
    response = portal["client"].get(f"/api/connections?{query}", headers=portal["headers"]["alice"])
    assert response.status_code == 400


def test_agent_and_provider_access_rechecked(portal: dict[str, Any]) -> None:
    """A signed identity alone grants no access to another agent's tools."""
    client = portal["client"]
    assert client.get("/api/connections", headers=portal["headers"]["mallory"]).status_code == 403
    assert client.get("/api/connections/github/status", headers=portal["headers"]["alice"]).status_code == 404


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_mutations_require_same_origin_and_empty_body(portal: dict[str, Any], action: str) -> None:
    """Cross-site requests and hidden body selectors cannot mutate accounts."""
    url = f"/api/connections/google_drive/{action}"
    headers = portal["headers"]["alice"]
    client = portal["client"]
    assert client.post(url, headers={**headers, "Origin": "https://evil.example.org"}, json={}).status_code == 403
    assert (
        client.post(url, headers={key: value for key, value in headers.items() if key != "Origin"}, json={}).status_code
        == 403
    )
    assert client.post(url, headers=headers, json={"agent_name": "other"}).status_code == 422


@pytest.mark.parametrize("configured_public_url", [True, False])
@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_personal_mutations_reject_cleartext_public_origin(
    portal: dict[str, Any],
    configured_public_url: bool,
    action: str,
) -> None:
    """Same-origin alone must not authorize hosted account changes over HTTP."""
    env = dict(portal["paths"].process_env)
    if configured_public_url:
        env["MINDROOM_PUBLIC_URL"] = "http://portal.example.org"
    else:
        env.pop("MINDROOM_PUBLIC_URL")
    paths = replace(portal["paths"], process_env=env)
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, portal["payload"])
    _use_runtime_auth_settings(main.app)
    client = TestClient(main.app, base_url="http://portal.example.org")
    response = client.post(
        f"/api/connections/google_drive/{action}",
        headers={**portal["headers"]["alice"], "Origin": "http://portal.example.org"},
        json={},
    )
    assert response.status_code == 403


def test_two_users_complete_and_disconnect_only_their_own_credentials(portal: dict[str, Any]) -> None:
    """Portal callbacks use the same private scope as tool execution."""
    client, headers = portal["client"], portal["headers"]
    status_url = "/api/connections/google_drive/status"
    for name in ("alice", "bob"):
        assert client.get(status_url, headers=headers[name]).json()["connected"] is False
    connect = client.post("/api/connections/google_drive/connect", headers=headers["alice"], json={})
    assert connect.status_code == 200
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    wrong_user = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        headers=headers["bob"],
        follow_redirects=False,
    )
    assert wrong_user.status_code in {400, 403}
    connect = client.post("/api/connections/google_drive/connect", headers=headers["alice"], json={})
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        headers=headers["alice"],
        follow_redirects=False,
    )
    assert callback.status_code in {302, 303, 307}
    assert (
        client.get(
            "/api/oauth/google_drive/callback",
            params={"code": "test-code", "state": state},
            headers=headers["alice"],
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        _stored_oauth_credentials(
            portal["provider"],
            portal["paths"],
            requester_id="@alice:example.org",
            agent_name="personal",
        )
        is not None
    )
    assert (
        _stored_oauth_credentials(
            portal["provider"],
            portal["paths"],
            requester_id="@bob:example.org",
            agent_name="personal",
        )
        is None
    )
    assert client.get(status_url, headers=headers["alice"]).json()["connected"] is True
    assert client.get(status_url, headers=headers["bob"]).json()["connected"] is False
    assert client.post("/api/connections/google_drive/disconnect", headers=headers["bob"], json={}).status_code == 200
    assert client.get(status_url, headers=headers["alice"]).json()["connected"] is True
    assert client.post("/api/connections/google_drive/disconnect", headers=headers["alice"], json={}).status_code == 200
    assert client.get(status_url, headers=headers["alice"]).json()["connected"] is False


def test_provider_failure_does_not_block_catalog_or_leak_details(
    portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable backend affects only its status request."""
    monkeypatch.setattr(oauth, "status", AsyncMock(side_effect=HTTPException(503, "internal-client-secret")))
    response = portal["client"].get("/api/connections/google_drive/status", headers=portal["headers"]["alice"])
    assert response.status_code == 503
    assert "internal-client-secret" not in response.text
    assert "no-store" in response.headers["cache-control"]
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 200


def test_unavailable_optional_plugin_does_not_block_healthy_provider(portal: dict[str, Any]) -> None:
    """Mirror runtime degraded startup while retaining healthy account connections."""
    portal["payload"]["plugins"] = ["./plugins/missing"]
    portal["paths"].config_path.write_text(yaml.safe_dump(portal["payload"]))
    assert config_lifecycle.load_config_into_app(portal["paths"], main.app)
    client = TestClient(main.app, base_url="https://portal.example.org", raise_server_exceptions=False)
    for url in ("/api/connections", "/api/connections/google_drive/status"):
        assert client.get(url, headers=portal["headers"]["alice"]).status_code == 200


def test_shared_service_account_is_not_a_personal_connection(portal: dict[str, Any]) -> None:
    """Global service configuration never appears as the user's account."""
    paths = replace(
        portal["paths"],
        process_env={**portal["paths"].process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"},
    )
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, portal["payload"])
    _use_runtime_auth_settings(main.app)
    response = portal["client"].get("/api/connections/google_drive/status", headers=portal["headers"]["alice"])
    assert response.status_code == 200
    assert response.json() == {
        "provider": "google_drive",
        "connected": False,
        "can_connect": False,
        "reset_required": False,
        "account_label": None,
    }
    assert (
        portal["client"]
        .post("/api/connections/google_drive/connect", headers=portal["headers"]["alice"], json={})
        .status_code
        == 409
    )


def test_unconfigured_portal_is_unavailable(portal: dict[str, Any]) -> None:
    """An explicit operator opt-in is required."""
    paths = replace(portal["paths"], process_env={**portal["paths"].process_env, "MINDROOM_CONNECTIONS_AGENT": ""})
    main.initialize_api_app(main.app, paths)
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 404


def test_canonical_alias_uses_the_same_private_scope(portal: dict[str, Any]) -> None:
    """An alternate signed Matrix identity resolves to its configured canonical owner."""
    portal["payload"]["authorization"] = {"aliases": {"@alice:example.org": ["@bob:example.org"]}}
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    client, headers = portal["client"], portal["headers"]
    connect = client.post("/api/connections/google_drive/connect", headers=headers["bob"], json={})
    assert connect.status_code == 200
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    assert (
        client.get(
            "/api/oauth/google_drive/callback",
            params={"code": "test-code", "state": state},
            headers=headers["bob"],
            follow_redirects=False,
        ).status_code
        == 307
    )
    assert client.get("/api/connections/google_drive/status", headers=headers["alice"]).json()["connected"] is True


def test_shared_agent_configuration_fails_closed(portal: dict[str, Any]) -> None:
    """Removing private ownership cannot silently turn the portal into shared credentials."""
    portal["payload"]["agents"]["personal"].pop("private")
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 403
