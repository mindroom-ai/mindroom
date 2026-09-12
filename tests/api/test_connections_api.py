"""Personal portal API isolation and existing OAuth lifecycle integration."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
import yaml
from aioresponses import aioresponses
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle, main, oauth
from mindroom.matrix.state import MatrixState
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
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> dict[str, Any]:  # noqa: ARG001
    """Serve the real API with signed users and one private agent."""
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
        for name in ("alice", "bob", "mallory", "admin")
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
    assert response.json()["agents"][0]["agent_display_name"] == "Personal Mind"
    assert [item["provider"] for item in response.json()["agents"][0]["services"]] == ["google_drive"]
    assert response.json()["agents"][0]["services"][0]["tools"] == ["google_drive"]
    assert "credential_service" not in response.text
    assert "no-store" in response.headers["cache-control"]
    status.assert_not_called()


def test_catalog_lists_tools_without_browser_authentication(portal: dict[str, Any]) -> None:
    """Tool visibility does not depend on having an OAuth provider."""
    portal["payload"]["agents"]["personal"]["tools"].append("matrix_message")
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    response = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    tools = {tool["name"]: tool for tool in response.json()["agents"][0]["tools"]}
    assert tools["calculator"]["provider"] is None
    assert tools["calculator"]["requires_room_context"] is False
    assert tools["google_drive"]["provider"] == "google_drive"
    assert tools["matrix_message"]["requires_room_context"] is True


@pytest.mark.parametrize("agent_name", ["personal", "research"])
@pytest.mark.parametrize(("content_type", "expected"), [("image/png", 200), ("image/svg+xml", 404)])
def test_avatar_serves_current_matrix_thumbnail(
    shared_portal: dict[str, Any],
    agent_name: str,
    content_type: str,
    expected: int,
) -> None:
    """Visible private and management-only shared agents use their saved Matrix identity."""
    state = MatrixState()
    state.add_account(f"agent_{agent_name}", "custom_bot", None, domain="example.org", access_token="avatar-token")  # noqa: S106
    state.save(runtime_paths=shared_portal["paths"])
    with aioresponses() as matrix:
        matrix.get(
            "http://localhost:8008/_matrix/client/v3/profile/@custom_bot:example.org",
            payload={"avatar_url": "mxc://example.org/current-avatar"},
        )
        matrix.get(
            "http://localhost:8008/_matrix/client/v1/media/thumbnail/example.org/current-avatar"
            "?width=96&height=96&method=scale&allow_remote=true",
            body=b"thumbnail",
            content_type=content_type,
        )
        response = shared_portal["client"].get(
            f"/api/connections/agents/{agent_name}/avatar",
            headers=shared_portal["headers"]["alice"],
        )
        assert response.status_code == expected, (response.text, list(matrix.requests))
        assert all(
            call.kwargs["headers"]["Authorization"] == "Bearer avatar-token"
            for calls in matrix.requests.values()
            for call in calls
        )
    if expected != 200:
        return
    assert response.content == b"thumbnail"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in response.headers["cache-control"]
    assert "avatar-token" not in str(response.headers)


@pytest.mark.parametrize("avatar_url", [None, "https://example.org/picture.png", "mxc://example.org/"])
def test_avatar_unavailable_for_missing_or_invalid_profile(portal: dict[str, Any], avatar_url: str | None) -> None:
    """An absent or non-Matrix picture cannot turn the endpoint into an arbitrary URL proxy."""
    state = MatrixState()
    state.add_account("agent_personal", "personal", None, domain="example.org", access_token="avatar-token")  # noqa: S106
    state.save(runtime_paths=portal["paths"])
    with aioresponses() as matrix:
        matrix.get(
            re.compile(r"http://localhost:8008/_matrix/client/v3/profile/.*"),
            payload={"avatar_url": avatar_url},
        )
        response = portal["client"].get("/api/connections/agents/personal/avatar", headers=portal["headers"]["alice"])
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("user", "agent_name", "expected"),
    [("alice", "support", 404), ("admin", "other_private", 404), (None, "personal", 401)],
)
def test_avatar_requires_visible_agent(
    shared_portal: dict[str, Any],
    user: str | None,
    agent_name: str,
    expected: int,
) -> None:
    """Signed-in access alone cannot reveal hidden agents, including to administrators."""
    with aioresponses() as matrix:
        response = shared_portal["client"].get(
            f"/api/connections/agents/{agent_name}/avatar",
            headers=shared_portal["headers"][user] if user else {},
        )
        assert not matrix.requests
    assert response.status_code == expected


@pytest.mark.parametrize("query", ["agent_name=other", "worker_key=other", "user_id=bob", "execution_scope=user"])
def test_target_overrides_rejected(portal: dict[str, Any], query: str) -> None:
    """The browser cannot choose a credential owner or scope."""
    response = portal["client"].get(f"/api/connections?{query}", headers=portal["headers"]["alice"])
    assert response.status_code == 400


def test_agent_and_provider_access_rechecked(portal: dict[str, Any]) -> None:
    """A signed identity alone grants no access to another agent's tools."""
    client = portal["client"]
    assert client.get("/api/connections", headers=portal["headers"]["mallory"]).status_code == 403
    assert (
        client.get("/api/connections/agents/personal/github/status", headers=portal["headers"]["alice"]).status_code
        == 404
    )


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_mutations_require_same_origin_and_empty_body(portal: dict[str, Any], action: str) -> None:
    """Cross-site requests and hidden body selectors cannot mutate accounts."""
    url = f"/api/connections/agents/personal/google_drive/{action}"
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
        f"/api/connections/agents/personal/google_drive/{action}",
        headers={**portal["headers"]["alice"], "Origin": "http://portal.example.org"},
        json={},
    )
    assert response.status_code == 403


def test_two_users_complete_and_disconnect_only_their_own_credentials(portal: dict[str, Any]) -> None:
    """Portal callbacks use the same private scope as tool execution."""
    client, headers = portal["client"], portal["headers"]
    status_url = "/api/connections/agents/personal/google_drive/status"
    for name in ("alice", "bob"):
        assert client.get(status_url, headers=headers[name]).json()["connected"] is False
    connect = client.post("/api/connections/agents/personal/google_drive/connect", headers=headers["alice"], json={})
    assert connect.status_code == 200
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    wrong_user = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        headers=headers["bob"],
        follow_redirects=False,
    )
    assert wrong_user.status_code in {400, 403}
    connect = client.post("/api/connections/agents/personal/google_drive/connect", headers=headers["alice"], json={})
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
    assert (
        client.post(
            "/api/connections/agents/personal/google_drive/disconnect",
            headers=headers["bob"],
            json={},
        ).status_code
        == 200
    )
    assert client.get(status_url, headers=headers["alice"]).json()["connected"] is True
    assert (
        client.post(
            "/api/connections/agents/personal/google_drive/disconnect",
            headers=headers["alice"],
            json={},
        ).status_code
        == 200
    )
    assert client.get(status_url, headers=headers["alice"]).json()["connected"] is False


def test_provider_failure_does_not_block_catalog_or_leak_details(
    portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable backend affects only its status request."""
    monkeypatch.setattr(oauth, "status", AsyncMock(side_effect=HTTPException(503, "internal-client-secret")))
    response = portal["client"].get(
        "/api/connections/agents/personal/google_drive/status",
        headers=portal["headers"]["alice"],
    )
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
    for url in ("/api/connections", "/api/connections/agents/personal/google_drive/status"):
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
    response = portal["client"].get(
        "/api/connections/agents/personal/google_drive/status",
        headers=portal["headers"]["alice"],
    )
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
        .post("/api/connections/agents/personal/google_drive/connect", headers=portal["headers"]["alice"], json={})
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
    connect = client.post("/api/connections/agents/personal/google_drive/connect", headers=headers["bob"], json={})
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
    assert (
        client.get("/api/connections/agents/personal/google_drive/status", headers=headers["alice"]).json()["connected"]
        is True
    )


def test_shared_agent_configuration_fails_closed(portal: dict[str, Any]) -> None:
    """Removing private ownership cannot silently turn the portal into shared credentials."""
    portal["payload"]["agents"]["personal"].pop("private")
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 403


@pytest.fixture
def shared_portal(portal: dict[str, Any]) -> dict[str, Any]:
    """Add shared agents with distinct managers and an unrelated private agent."""
    portal["payload"]["agents"].update(
        {
            "research": {
                "display_name": "Research Team",
                "role": "Research assistant",
                "tools": ["google_drive"],
                "credential_managers": ["@alice:example.org", "@mallory:example.org"],
            },
            "support": {
                "display_name": "Support Team",
                "role": "Support assistant",
                "tools": ["google_drive"],
                "credential_managers": ["@bob:example.org"],
            },
            "other_private": {
                "display_name": "Other private agent",
                "role": "Private assistant",
                "tools": ["google_drive"],
                "private": {"per": "user_agent"},
                "credential_managers": ["@alice:example.org"],
            },
        },
    )
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    return portal


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("alice", ["personal", "research"]),
        ("bob", ["personal", "support"]),
        ("mallory", ["research"]),
        ("admin", ["personal", "research", "support"]),
    ],
)
def test_catalog_lists_only_authorized_agents(shared_portal: dict[str, Any], user: str, expected: list[str]) -> None:
    """Managers can discover shared connections even without private-agent access."""
    response = shared_portal["client"].get("/api/connections", headers=shared_portal["headers"][user])
    assert response.status_code == 200, response.text
    agents = response.json()["agents"]
    assert [agent["agent_name"] for agent in agents] == expected
    assert all(agent["is_shared"] == (agent["agent_name"] != "personal") for agent in agents)


def test_agent_user_sees_shared_connection_without_management(shared_portal: dict[str, Any]) -> None:
    """Using a shared agent reveals availability without shared-account mutation authority."""
    shared_portal["payload"]["agents"]["research"]["access"] = {"users": ["@bob:example.org"]}
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    client, headers = shared_portal["client"], shared_portal["headers"]
    response = client.get("/api/connections", headers=headers["bob"])
    research = next(agent for agent in response.json()["agents"] if agent["agent_name"] == "research")
    assert research["can_use"] is True
    assert research["services"][0]["can_manage"] is False
    base = "/api/connections/agents/research/google_drive"
    connect = client.post(f"{base}/connect", headers=headers["alice"], json={})
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    assert (
        client.get(
            "/api/oauth/google_drive/callback",
            params={"code": "test-code", "state": state},
            headers=headers["alice"],
            follow_redirects=False,
        ).status_code
        == 307
    )
    status = client.get(f"{base}/status", headers=headers["bob"])
    assert status.status_code == 200, status.text
    assert status.json()["connected"] is True
    assert status.json()["account_label"] is None
    assert status.json()["can_connect"] is False
    for action in ("connect", "disconnect"):
        assert client.post(f"{base}/{action}", headers=headers["bob"], json={}).status_code == 403
        assert (
            client.post(f"/api/oauth/google_drive/{action}?agent_name=research", headers=headers["bob"]).status_code
            == 403
        )
    assert client.get(f"{base}/status", headers=headers["alice"]).json()["connected"] is True


@pytest.mark.parametrize("requester_provider", [False, True])
def test_agent_user_can_manage_only_their_personal_connection(
    shared_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    requester_provider: bool,
) -> None:
    """A shared agent's user credentials remain owned by each authorized requester."""
    agent = shared_portal["payload"]["agents"]["research"]
    agent["access"] = {"users": ["@alice:example.org", "@bob:example.org"]}
    agent["worker_scope"] = "shared" if requester_provider else "user_agent"
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        requester_scoped_credentials=requester_provider,
    )
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (provider,))
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    client, headers = shared_portal["client"], shared_portal["headers"]
    base = "/api/connections/agents/research/google_drive"
    response = client.get("/api/connections", headers=headers["bob"])
    research = next(agent for agent in response.json()["agents"] if agent["agent_name"] == "research")
    assert research["services"][0]["can_manage"] is True
    connect = client.post(f"{base}/connect", headers=headers["bob"], json={})
    assert connect.status_code == 200, connect.text
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
    assert client.get(f"{base}/status", headers=headers["bob"]).json()["connected"] is True
    assert client.get(f"{base}/status", headers=headers["alice"]).json()["connected"] is False
    assert client.post(f"{base}/disconnect", headers=headers["alice"], json={}).status_code == 200
    assert client.get(f"{base}/status", headers=headers["bob"]).json()["connected"] is True
    assert client.post(f"{base}/disconnect", headers=headers["bob"], json={}).status_code == 200

    connect = client.post(f"{base}/connect", headers=headers["bob"], json={})
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    agent["access"]["users"] = ["@alice:example.org"]
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    callback = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        headers=headers["bob"],
        follow_redirects=False,
    )
    assert callback.status_code == 403
    agent["access"]["users"].append("@bob:example.org")
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    assert client.get(f"{base}/status", headers=headers["bob"]).json()["connected"] is False


@pytest.mark.parametrize(
    ("user", "agent"),
    [
        ("bob", "research"),
        ("bob", "other_private"),
        ("bob", "missing"),
        ("alice", "other_private"),
        ("admin", "other_private"),
    ],
)
@pytest.mark.parametrize("action", ["status", "connect", "disconnect"])
def test_unlisted_agent_actions_are_denied(
    shared_portal: dict[str, Any],
    user: str,
    agent: str,
    action: str,
) -> None:
    """Direct URLs cannot bypass catalog authorization or choose another private agent."""
    response = shared_portal["client"].request(
        "GET" if action == "status" else "POST",
        f"/api/connections/agents/{agent}/google_drive/{action}",
        headers=shared_portal["headers"][user],
        **({} if action == "status" else {"json": {}}),
    )
    assert response.status_code == 404


@pytest.mark.parametrize(("worker_scope", "shared_across_agents"), [(None, True), ("shared", False)])
def test_shared_connection_lifecycle_and_revoked_manager(
    shared_portal: dict[str, Any],
    worker_scope: str | None,
    shared_across_agents: bool,
) -> None:
    """Connections follow configured ownership, isolate private stores, and enforce revocation."""
    if worker_scope is not None:
        for agent_name in ("research", "support"):
            shared_portal["payload"]["agents"][agent_name]["worker_scope"] = worker_scope
        _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
        _use_runtime_auth_settings(main.app)
    client, headers = shared_portal["client"], shared_portal["headers"]
    base = "/api/connections/agents/research/google_drive"
    connect = client.post(f"{base}/connect", headers=headers["alice"], json={})
    assert connect.status_code == 200, connect.text
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        headers=headers["alice"],
        follow_redirects=False,
    )
    assert callback.status_code == 307, callback.text
    assert client.get(f"{base}/status", headers=headers["mallory"]).json()["connected"] is True
    assert (
        client.get("/api/connections/agents/support/google_drive/status", headers=headers["bob"]).json()["connected"]
        is shared_across_agents
    )
    assert (
        client.get(
            "/api/connections/agents/personal/google_drive/status",
            headers=headers["alice"],
        ).json()["connected"]
        is False
    )
    shared_portal["payload"]["agents"]["research"]["credential_managers"] = ["@mallory:example.org"]
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    for action in ("status", "connect", "disconnect"):
        response = client.request(
            "GET" if action == "status" else "POST",
            f"{base}/{action}",
            headers=headers["alice"],
            **({} if action == "status" else {"json": {}}),
        )
        assert response.status_code == 404
    assert client.post(f"{base}/disconnect", headers=headers["mallory"], json={}).status_code == 200
    assert client.get(f"{base}/status", headers=headers["mallory"]).json()["connected"] is False
    assert (
        client.get("/api/connections/agents/support/google_drive/status", headers=headers["bob"]).json()["connected"]
        is False
    )


def test_shared_manager_alias_is_canonicalized(shared_portal: dict[str, Any]) -> None:
    """Catalog and actions use the same canonical identity as credential authorization."""
    shared_portal["payload"]["authorization"] = {"aliases": {"@alice:example.org": ["@bob:example.org"]}}
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    response = shared_portal["client"].get("/api/connections", headers=shared_portal["headers"]["bob"])
    assert [agent["agent_name"] for agent in response.json()["agents"]] == ["personal", "research"]
    assert (
        shared_portal["client"]
        .get(
            "/api/connections/agents/research/google_drive/status",
            headers=shared_portal["headers"]["bob"],
        )
        .status_code
        == 200
    )


@pytest.mark.parametrize("action", ["status", "connect", "disconnect"])
def test_provider_must_belong_to_selected_agent(shared_portal: dict[str, Any], action: str) -> None:
    """A provider available to the personal agent cannot be used for an unrelated shared agent."""
    shared_portal["payload"]["agents"]["research"]["tools"] = ["calculator"]
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    response = shared_portal["client"].request(
        "GET" if action == "status" else "POST",
        f"/api/connections/agents/research/google_drive/{action}",
        headers=shared_portal["headers"]["alice"],
        **({} if action == "status" else {"json": {}}),
    )
    assert response.status_code == 404


def test_shared_agent_requester_scoped_provider_stays_personal(
    shared_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester-only providers never expose a manager's credentials to other managers."""
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        requester_scoped_credentials=True,
    )
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (provider,))
    client, headers = shared_portal["client"], shared_portal["headers"]
    base = "/api/connections/agents/research/google_drive"
    connect = client.post(f"{base}/connect", headers=headers["alice"], json={})
    assert connect.status_code == 200, connect.text
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        headers=headers["alice"],
        follow_redirects=False,
    )
    assert callback.status_code == 307, callback.text
    assert client.get(f"{base}/status", headers=headers["alice"]).json()["connected"] is True
    assert client.get(f"{base}/status", headers=headers["mallory"]).json()["connected"] is False
    assert client.post(f"{base}/disconnect", headers=headers["mallory"], json={}).status_code == 200
    assert client.get(f"{base}/status", headers=headers["alice"]).json()["connected"] is True


@pytest.mark.parametrize("invalid_target", ["missing", "shared"])
@pytest.mark.parametrize("user", ["alice", "admin"])
def test_invalid_personal_target_disables_shared_connections(
    shared_portal: dict[str, Any],
    invalid_target: str,
    user: str,
) -> None:
    """A malformed portal configuration cannot become a shared credential portal."""
    if invalid_target == "missing":
        del shared_portal["payload"]["agents"]["personal"]
    else:
        shared_portal["payload"]["agents"]["personal"].pop("private")
        shared_portal["payload"]["agents"]["personal"]["credential_managers"] = ["@alice:example.org"]
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    client, headers = shared_portal["client"], shared_portal["headers"][user]
    assert client.get("/api/connections", headers=headers).status_code == 403
    for action in ("status", "connect", "disconnect"):
        response = client.request(
            "GET" if action == "status" else "POST",
            f"/api/connections/agents/research/google_drive/{action}",
            headers=headers,
            **({} if action == "status" else {"json": {}}),
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    ("requester_scoped", "worker_scope", "expected_shared"),
    [
        (False, None, True),
        (False, "shared", True),
        (False, "user", False),
        (False, "user_agent", False),
        (True, None, False),
        (True, "shared", False),
        (True, "user", False),
        (True, "user_agent", False),
    ],
)
def test_catalog_reports_connection_sharing_by_provider_and_scope(
    shared_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    requester_scoped: bool,
    worker_scope: str | None,
    expected_shared: bool,
) -> None:
    """Disconnect impact follows credential ownership rather than agent privacy."""
    shared_portal["payload"]["agents"]["research"]["worker_scope"] = worker_scope
    _publish_config(main.app, shared_portal["paths"], shared_portal["payload"])
    _use_runtime_auth_settings(main.app)
    provider = _fake_provider(
        provider_id="google_drive",
        credential_service="google_drive_oauth",
        requester_scoped_credentials=requester_scoped,
    )
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (provider,))
    response = shared_portal["client"].get("/api/connections", headers=shared_portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    personal, research = response.json()["agents"]
    assert personal["services"][0]["is_shared"] is False
    assert research["is_shared"] is True
    assert research["services"][0]["is_shared"] is expected_shared
