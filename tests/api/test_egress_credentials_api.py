"""Personal egress credentials API authorization and write-only guarantee."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.api import main, oauth
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import credential_store as oauth_credential_store
from mindroom.oauth import registry as oauth_registry
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import (
    _fake_provider,
    _oauth_credential_context,
    _publish_config,
    _runtime_paths,
    _use_runtime_auth_settings,
)
from tests.oauth_test_utils import corrupt_oauth_credential_payload

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def egress_portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> dict[str, Any]:  # noqa: ARG001
    """Serve the real API with signed users and agents with shell/python tools."""
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
    }
    paths = _runtime_paths(tmp_path, env)
    payload = {
        "administrators": ["@admin:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "egress_broker": {
            "services": {
                "github": {
                    "display_name": "GitHub",
                    "description": "GitHub API and git over HTTPS",
                    "rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}],
                },
                "openai": {
                    "description": "OpenAI API",
                    "rules": [{"host": "api.openai.com", "auth": {"type": "header", "name": "Authorization"}}],
                },
            },
        },
        "agents": {
            "personal": {
                "display_name": "Personal Mind",
                "role": "Personal assistant",
                "tools": ["shell", "python"],
                "private": {"per": "user_agent"},
                "access": {"users": ["@alice:example.org", "@bob:example.org"]},
            },
            "shared_dev": {
                "display_name": "Shared Dev",
                "role": "Team agent",
                "tools": ["shell", "python"],
                "worker_scope": "shared",
                "credential_managers": ["@bob:example.org", "@carol:example.org"],
                "access": {"users": ["@alice:example.org", "@bob:example.org"]},
            },
            "no_shell": {
                "display_name": "Calculator",
                "role": "Math only",
                "tools": ["calculator"],
                "private": {"per": "user_agent"},
                "access": {"users": ["@alice:example.org"]},
            },
            "other_private": {
                "display_name": "Other Private",
                "role": "Another private agent",
                "tools": ["shell"],
                "private": {"per": "user"},
                "access": {"users": ["@mallory:example.org"]},
            },
        },
    }
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
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
        for name in ("alice", "bob", "carol", "admin")
    }
    return {
        "client": TestClient(main.app, base_url="https://portal.example.org"),
        "headers": headers,
        "paths": paths,
        "payload": payload,
    }


@pytest.mark.parametrize("with_connections_agent", [False, True])
def test_works_with_and_without_connections_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce_turn_authorization: None,  # noqa: ARG001
    with_connections_agent: bool,
) -> None:
    """The egress routes work with or without MINDROOM_CONNECTIONS_AGENT set."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    env = {
        **_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"),
        "MINDROOM_PUBLIC_URL": "https://portal.example.org",
    }
    if with_connections_agent:
        env["MINDROOM_CONNECTIONS_AGENT"] = "personal"

    paths = _runtime_paths(tmp_path, env)
    payload = {
        "administrators": ["@admin:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "egress_broker": {
            "services": {
                "github": {
                    "display_name": "GitHub",
                    "description": "GitHub API",
                    "rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}],
                },
            },
        },
        "agents": {
            "personal": {
                "display_name": "Personal",
                "tools": ["shell"],
                "private": {"per": "user_agent"},
                "access": {"users": ["@alice:example.org"]},
            },
        },
    }
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    headers = {
        "X-Trusted-User": "alice",
        "X-Trusted-Jwt": _trusted_upstream_jwt(
            key,
            user_id="alice",
            email="alice@example.org",
            matrix_user_id="@alice:example.org",
        ),
        "Origin": "https://portal.example.org",
    }
    client = TestClient(main.app, base_url="https://portal.example.org")
    response = client.get("/api/connections/egress", headers=headers)
    assert response.status_code == 200, response.text
    assert len(response.json()["agents"]) == 1
    assert response.json()["agents"][0]["agent_name"] == "personal"

    # Non-admin user should be allowed (personal user gate test)
    assert "Administrator access required" not in response.text


def test_lists_only_usable_shell_or_python_agents(egress_portal: dict[str, Any]) -> None:
    """Only agents with shell or python tools that the user may use appear."""
    response = egress_portal["client"].get("/api/connections/egress", headers=egress_portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    agent_names = {a["agent_name"] for a in response.json()["agents"]}
    assert agent_names == {"personal", "shared_dev"}
    assert all(len(a["services"]) == 2 for a in response.json()["agents"])
    assert all(s["name"] in ("github", "openai") for a in response.json()["agents"] for s in a["services"])


def test_requester_sets_own_user_agent_secret(egress_portal: dict[str, Any]) -> None:
    """A requester sets their own secret on a private agent, and another user sees it unconfigured."""
    client = egress_portal["client"]
    alice_headers = egress_portal["headers"]["alice"]
    bob_headers = egress_portal["headers"]["bob"]

    # Alice sets her secret
    response = client.put(
        "/api/connections/egress/agents/personal/github",
        json={"secret": "alice-token"},
        headers=alice_headers,
    )
    assert response.status_code == 204, response.text

    # Alice sees it configured
    response = client.get("/api/connections/egress", headers=alice_headers)
    assert response.status_code == 200, response.text
    personal_services = [s for a in response.json()["agents"] if a["agent_name"] == "personal" for s in a["services"]]
    github_service = next(s for s in personal_services if s["name"] == "github")
    assert github_service["configured"] is True
    assert github_service["updated_at"] is not None
    assert "alice-token" not in response.text

    # Bob sees it unconfigured (his own scope)
    response = client.get("/api/connections/egress", headers=bob_headers)
    assert response.status_code == 200, response.text
    personal_services_bob = [
        s for a in response.json()["agents"] if a["agent_name"] == "personal" for s in a["services"]
    ]
    github_service_bob = next(s for s in personal_services_bob if s["name"] == "github")
    assert github_service_bob["configured"] is False
    assert github_service_bob["updated_at"] is None


def test_shared_agent_requires_credential_manager(egress_portal: dict[str, Any]) -> None:
    """A plain user cannot set shared agent secrets; credential managers can."""
    client = egress_portal["client"]
    alice_headers = egress_portal["headers"]["alice"]
    bob_headers = egress_portal["headers"]["bob"]

    # Alice (not a manager) cannot set
    response = client.put(
        "/api/connections/egress/agents/shared_dev/github",
        json={"secret": "alice-token"},
        headers=alice_headers,
    )
    assert response.status_code == 403, response.text
    assert "Credential management is required" in response.text

    # Bob (manager) can set
    response = client.put(
        "/api/connections/egress/agents/shared_dev/github",
        json={"secret": "team-token"},
        headers=bob_headers,
    )
    assert response.status_code == 204, response.text

    # Both see it configured
    for headers in (alice_headers, bob_headers):
        response = client.get("/api/connections/egress", headers=headers)
        assert response.status_code == 200, response.text
        shared_services = [
            s for a in response.json()["agents"] if a["agent_name"] == "shared_dev" for s in a["services"]
        ]
        github_service = next(s for s in shared_services if s["name"] == "github")
        assert github_service["configured"] is True


def test_cross_origin_mutation_rejected(egress_portal: dict[str, Any]) -> None:
    """Mutations require same-origin HTTPS requests."""
    client = egress_portal["client"]
    alice_headers = {**egress_portal["headers"]["alice"], "Origin": "https://evil.example.com"}

    response = client.put(
        "/api/connections/egress/agents/personal/github",
        json={"secret": "token"},
        headers=alice_headers,
    )
    assert response.status_code == 403, response.text

    # GET is allowed
    response = client.get("/api/connections/egress", headers=alice_headers)
    assert response.status_code == 200, response.text


def test_response_never_contains_secret(egress_portal: dict[str, Any]) -> None:
    """Responses never include secret values, only status."""
    client = egress_portal["client"]
    alice_headers = egress_portal["headers"]["alice"]

    put_response = client.put(
        "/api/connections/egress/agents/personal/github",
        json={"secret": "very-secret-token"},
        headers=alice_headers,
    )
    assert put_response.status_code == 204, put_response.text

    response = client.get("/api/connections/egress", headers=alice_headers)
    assert response.status_code == 200, response.text
    assert "very-secret-token" not in response.text
    # "secret" key exists in config but secret value never appears
    assert '"secret"' not in response.text


def test_unknown_agent_or_service_404(egress_portal: dict[str, Any]) -> None:
    """Unknown agent or service returns 404."""
    client = egress_portal["client"]
    alice_headers = egress_portal["headers"]["alice"]

    # Unknown agent
    response = client.put(
        "/api/connections/egress/agents/unknown/github",
        json={"secret": "token"},
        headers=alice_headers,
    )
    assert response.status_code == 404, response.text

    # Unknown service
    response = client.put(
        "/api/connections/egress/agents/personal/unknown",
        json={"secret": "token"},
        headers=alice_headers,
    )
    assert response.status_code == 404, response.text

    # DELETE unknown
    response = client.delete(
        "/api/connections/egress/agents/personal/unknown",
        headers=alice_headers,
    )
    assert response.status_code == 404, response.text


def test_portal_catalog_includes_egress_services(egress_portal: dict[str, Any]) -> None:
    """When MINDROOM_CONNECTIONS_AGENT is set, the portal catalog includes egress_services."""
    response = egress_portal["client"].get("/api/connections", headers=egress_portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    for agent in response.json()["agents"]:
        if agent["agent_name"] in ("personal", "shared_dev"):
            assert "egress_services" in agent
            assert len(agent["egress_services"]) == 2
            assert {s["name"] for s in agent["egress_services"]} == {"github", "openai"}
        else:
            assert "egress_services" in agent
            assert len(agent["egress_services"]) == 0


def test_ineligible_agents_return_404(egress_portal: dict[str, Any]) -> None:
    """Non-usable user or agent without shell/python returns 404."""
    client = egress_portal["client"]
    alice_headers = egress_portal["headers"]["alice"]

    # Agent without shell/python -> 404
    response = client.put(
        "/api/connections/egress/agents/no_shell/github",
        json={"secret": "token"},
        headers=alice_headers,
    )
    assert response.status_code == 404, response.text

    # User without access to private agent -> 404
    response = client.put(
        "/api/connections/egress/agents/other_private/github",
        json={"secret": "token"},
        headers=alice_headers,
    )
    assert response.status_code == 404, response.text


def test_credential_manager_without_use_access_sees_no_egress_anywhere(egress_portal: dict[str, Any]) -> None:
    """A manager who may not use the agent is shown no egress rows and cannot mutate them."""
    client = egress_portal["client"]
    carol_headers = egress_portal["headers"]["carol"]

    catalog = client.get("/api/connections", headers=carol_headers)
    assert catalog.status_code == 200, catalog.text
    shared_dev = next(agent for agent in catalog.json()["agents"] if agent["agent_name"] == "shared_dev")
    assert shared_dev["can_use"] is False
    assert shared_dev["egress_services"] == []

    listing = client.get("/api/connections/egress", headers=carol_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["agents"] == []

    response = client.put(
        "/api/connections/egress/agents/shared_dev/github",
        json={"secret": "token"},
        headers=carol_headers,
    )
    assert response.status_code == 404, response.text


@pytest.fixture
def oauth_egress_portal(egress_portal: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Extend the egress portal with services backed by an agent-scoped and a requester-scoped OAuth provider."""
    drive = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    github = _fake_provider(
        provider_id="github",
        credential_service="github_oauth",
        client_config_services=("github_oauth_client",),
        requester_scoped_credentials=True,
    )
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (drive, github))
    paths = egress_portal["paths"]
    manager = get_runtime_credentials_manager(paths)
    for client_service in ("test_drive_oauth_client", "github_oauth_client"):
        manager.save_credentials(
            client_service,
            {"client_id": "test-client", "client_secret": "test-secret", "_source": "ui"},
        )
    payload = egress_portal["payload"]
    for agent in ("personal", "shared_dev"):
        # The Connections portal lists a provider only for agents that have a tool using it.
        payload["agents"][agent]["tools"].append({"name": "google_drive", "defer": True})
    rules = [{"host": "www.googleapis.com", "auth": {"type": "bearer"}}]
    payload["egress_broker"]["services"].update(
        {
            "drive": {"description": "Drive", "oauth_provider": "google_drive", "rules": rules},
            "gh": {"display_name": "GH", "oauth_provider": "github", "rules": rules},
            "ghost": {"description": "Unknown provider", "oauth_provider": "no_such_provider", "rules": rules},
        },
    )
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    return {**egress_portal, "providers": {"google_drive": drive, "github": github}}


def _egress_row(portal: dict[str, Any], user: str, agent: str, service: str) -> dict[str, Any]:
    response = portal["client"].get("/api/connections/egress", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    agent_row = next(item for item in response.json()["agents"] if item["agent_name"] == agent)
    return next(item for item in agent_row["services"] if item["name"] == service)


def _connect_account(portal: dict[str, Any], user: str, agent: str, service: str, provider: str) -> None:
    """Run the whole OAuth flow through the egress connect route and the shared callback."""
    client, headers = portal["client"], portal["headers"][user]
    connect = client.post(f"/api/connections/egress/agents/{agent}/{service}/connect", headers=headers, json={})
    assert connect.status_code == 200, connect.text
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        f"/api/oauth/{provider}/callback",
        params={"code": "test-code", "state": state},
        headers=headers,
        follow_redirects=False,
    )
    assert callback.status_code in {302, 303, 307}, callback.text


def test_status_shows_the_oauth_connection_and_a_stored_key_wins(oauth_egress_portal: dict[str, Any]) -> None:
    """A connected account shows per scope, an explicit key takes over, and removing it falls back."""
    portal = oauth_egress_portal
    before = _egress_row(portal, "alice", "personal", "drive")
    assert before["active_source"] is None
    assert before["configured"] is False
    assert before["key_configured"] is False
    assert before["oauth"] == {
        "provider": "google_drive",
        "display_name": "Test Drive",
        "connected": False,
        "account_label": None,
        "can_connect": True,
        "reset_required": False,
    }

    _connect_account(portal, "alice", "personal", "drive", "google_drive")

    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["active_source"] == "oauth"
    assert row["configured"] is True
    assert row["key_configured"] is False
    assert row["key_updated_at"] is None
    assert row["updated_at"] is None
    assert row["oauth"]["connected"] is True
    assert row["oauth"]["account_label"] == "alice@example.com"
    assert _egress_row(portal, "bob", "personal", "drive")["oauth"]["connected"] is False

    put = portal["client"].put(
        "/api/connections/egress/agents/personal/drive",
        json={"secret": "alice-key"},
        headers=portal["headers"]["alice"],
    )
    assert put.status_code == 204, put.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["active_source"] == "key"
    assert row["key_configured"] is True
    assert row["key_updated_at"] is not None
    assert row["updated_at"] == row["key_updated_at"]
    assert row["oauth"]["connected"] is True

    delete = portal["client"].delete(
        "/api/connections/egress/agents/personal/drive",
        headers=portal["headers"]["alice"],
    )
    assert delete.status_code == 204, delete.text
    assert _egress_row(portal, "alice", "personal", "drive")["active_source"] == "oauth"


def test_services_without_an_oauth_provider_report_no_oauth(oauth_egress_portal: dict[str, Any]) -> None:
    """Plain key services keep their status and carry a null oauth block."""
    row = _egress_row(oauth_egress_portal, "alice", "personal", "github")
    assert row["oauth"] is None
    assert row["active_source"] is None
    assert row["key_configured"] is False


def test_unknown_provider_has_no_oauth_block(oauth_egress_portal: dict[str, Any]) -> None:
    """A service naming a provider the registry lacks lists without an OAuth block instead of failing the page."""
    row = _egress_row(oauth_egress_portal, "alice", "personal", "ghost")
    assert row["oauth"] is None
    assert row["active_source"] is None


def test_connect_returns_an_authorize_url_for_an_eligible_manager(oauth_egress_portal: dict[str, Any]) -> None:
    """The route delegates to the existing OAuth connect flow for the service's provider."""
    portal = oauth_egress_portal
    for user, agent in (("alice", "personal"), ("bob", "shared_dev")):
        response = portal["client"].post(
            f"/api/connections/egress/agents/{agent}/drive/connect",
            headers=portal["headers"][user],
            json={},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "google_drive"
        assert body["auth_url"].startswith("https://auth.example.test/google_drive/authorize?")
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("action", ["connect", "disconnect"])
@pytest.mark.parametrize(
    ("user", "agent", "service", "expected"),
    [
        ("alice", "shared_dev", "drive", 403),
        ("alice", "shared_dev", "github", 403),
        ("carol", "shared_dev", "drive", 404),
        ("alice", "no_shell", "drive", 404),
        ("alice", "other_private", "drive", 404),
        ("alice", "unknown", "drive", 404),
        ("alice", "personal", "unknown", 404),
        ("alice", "personal", "github", 404),
        ("alice", "personal", "ghost", 404),
    ],
)
def test_connect_and_disconnect_use_the_key_route_gates(
    oauth_egress_portal: dict[str, Any],
    action: str,
    user: str,
    agent: str,
    service: str,
    expected: int,
) -> None:
    """Eligibility 404s come before the management 403, and a service without a usable provider is a 404."""
    portal = oauth_egress_portal
    response = portal["client"].post(
        f"/api/connections/egress/agents/{agent}/{service}/{action}",
        headers=portal["headers"][user],
        json={},
    )
    assert response.status_code == expected, response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_reject_other_origins_and_bodies(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """Cross-site requests and hidden body selectors cannot change an account."""
    portal = oauth_egress_portal
    url = f"/api/connections/egress/agents/personal/drive/{action}"
    headers = portal["headers"]["alice"]
    client = portal["client"]
    assert client.post(url, headers={**headers, "Origin": "https://evil.example.org"}, json={}).status_code == 403
    no_origin = {key: value for key, value in headers.items() if key != "Origin"}
    assert client.post(url, headers=no_origin, json={}).status_code == 403
    assert client.post(url, headers=headers, json={"agent_name": "other"}).status_code == 422
    assert client.post(f"{url}?agent_name=other", headers=headers, json={}).status_code == 400
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["connected"] is False


def test_disconnect_clears_only_the_callers_connection(oauth_egress_portal: dict[str, Any]) -> None:
    """Disconnect resets the caller's scoped connection and leaves other users' connections alone."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    _connect_account(portal, "bob", "personal", "drive", "google_drive")

    response = portal["client"].post(
        "/api/connections/egress/agents/personal/drive/disconnect",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "disconnected", "provider": "google_drive"}
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["connected"] is False
    assert _egress_row(portal, "alice", "personal", "drive")["active_source"] is None
    assert _egress_row(portal, "bob", "personal", "drive")["oauth"]["connected"] is True


def test_shared_agent_connection_is_managed_but_visible_to_users(oauth_egress_portal: dict[str, Any]) -> None:
    """Users of a shared agent see that it is connected, while its account and connect action stay with managers."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "shared_dev", "drive", "google_drive")

    manager = _egress_row(portal, "bob", "shared_dev", "drive")["oauth"]
    assert manager["connected"] is True
    assert manager["account_label"] == "alice@example.com"
    assert manager["can_connect"] is True
    user = _egress_row(portal, "alice", "shared_dev", "drive")["oauth"]
    assert user["connected"] is True
    assert user["account_label"] is None
    assert user["can_connect"] is False


def test_requester_scoped_provider_connects_the_requester(oauth_egress_portal: dict[str, Any]) -> None:
    """GitHub-style providers store the connection per requester even on a shared agent."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "shared_dev", "gh", "github")

    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["connected"] is True
    assert _egress_row(portal, "alice", "shared_dev", "gh")["oauth"]["connected"] is False


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_service_account_provider_is_not_managed_as_a_personal_account(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """A shared Google service account is runtime configuration, never a personal account."""
    portal = oauth_egress_portal
    paths = replace(
        portal["paths"],
        process_env={**portal["paths"].process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"},
    )
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, portal["payload"])
    _use_runtime_auth_settings(main.app)

    response = portal["client"].post(
        f"/api/connections/egress/agents/personal/drive/{action}",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert response.status_code == 409, response.text
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["can_connect"] is False


def test_portal_catalog_includes_the_oauth_status(oauth_egress_portal: dict[str, Any]) -> None:
    """The Connections portal shows the same OAuth status as the egress page."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")

    response = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    personal = next(agent for agent in response.json()["agents"] if agent["agent_name"] == "personal")
    drive = next(service for service in personal["egress_services"] if service["name"] == "drive")
    assert drive["active_source"] == "oauth"
    assert drive["oauth"]["connected"] is True
    assert drive["oauth"]["account_label"] == "alice@example.com"
    assert drive == _egress_row(portal, "alice", "personal", "drive")
    github = next(service for service in personal["egress_services"] if service["name"] == "github")
    assert github["oauth"] is None


def _portal_oauth_view(portal: dict[str, Any], user: str, agent: str) -> dict[str, Any]:
    """Return the Connections portal's status for the agent's Drive connection, in the egress field names."""
    response = portal["client"].get(
        f"/api/connections/agents/{agent}/google_drive/status",
        headers=portal["headers"][user],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {key: body[key] for key in ("connected", "can_connect", "reset_required", "account_label")}


def _egress_oauth_view(portal: dict[str, Any], user: str, agent: str) -> dict[str, Any]:
    oauth = _egress_row(portal, user, agent, "drive")["oauth"]
    return {key: oauth[key] for key in ("connected", "can_connect", "reset_required", "account_label")}


def _assert_portal_and_egress_agree(portal: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare every viewer of the personal and the shared agent, and return the views for further checks."""
    views = []
    for user, agent in (("alice", "personal"), ("bob", "personal"), ("alice", "shared_dev"), ("bob", "shared_dev")):
        egress = _egress_oauth_view(portal, user, agent)
        assert egress == _portal_oauth_view(portal, user, agent), (user, agent)
        views.append(egress)
    return views


def test_egress_oauth_status_agrees_with_the_portal_status(oauth_egress_portal: dict[str, Any]) -> None:
    """Not connected, connected, and unreadable connections read the same on the egress and portal pages."""
    portal = oauth_egress_portal
    assert _assert_portal_and_egress_agree(portal)[0]["connected"] is False

    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    _connect_account(portal, "bob", "shared_dev", "drive", "google_drive")
    alice, bob, alice_shared, bob_shared = _assert_portal_and_egress_agree(portal)
    assert alice == {
        "connected": True,
        "can_connect": True,
        "reset_required": False,
        "account_label": "alice@example.com",
    }
    assert bob["connected"] is False
    assert alice_shared["connected"] is True
    assert alice_shared["account_label"] is None
    assert alice_shared["can_connect"] is False
    assert bob_shared["account_label"] == "alice@example.com"

    context = _oauth_credential_context(
        portal["providers"]["google_drive"],
        portal["paths"],
        requester_id="@alice:example.org",
        agent_name="personal",
    )
    corrupt_oauth_credential_payload(oauth_credential_store._oauth_credential_database_path(context), b"unreadable")
    alice, *_rest = _assert_portal_and_egress_agree(portal)
    assert alice["reset_required"] is True
    assert alice["connected"] is False


def test_egress_oauth_status_agrees_with_the_portal_for_a_service_account(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """A shared service account counts as connected for users, is no personal account, and cannot be connected."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    paths = replace(
        portal["paths"],
        process_env={**portal["paths"].process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"},
    )
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, portal["payload"])
    _use_runtime_auth_settings(main.app)

    alice, bob, alice_shared, bob_shared = _assert_portal_and_egress_agree(portal)
    # Managers of their own agents see no personal account to manage; plain users of a shared agent see it served.
    assert alice == {"connected": False, "can_connect": False, "reset_required": False, "account_label": None}
    assert bob == alice
    assert alice_shared == {"connected": True, "can_connect": False, "reset_required": False, "account_label": None}
    assert bob_shared == {"connected": False, "can_connect": False, "reset_required": False, "account_label": None}


def test_listing_never_refreshes_tokens(oauth_egress_portal: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing and the portal catalog read stored state, so they never wait on a provider's token endpoint."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")

    def refresh_must_not_run(*_args: object, **_kwargs: object) -> None:
        msg = "A listing must not refresh tokens"
        raise AssertionError(msg)

    monkeypatch.setattr(oauth, "refresh_oauth_credentials", refresh_must_not_run)
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["connected"] is True
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 200


def test_an_unloadable_connection_state_does_not_fail_the_listing(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider failing to report leaves the page up, with its service shown as not connectable."""
    portal = oauth_egress_portal

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise HTTPException(503, "internal-client-secret")

    monkeypatch.setattr(oauth, "agent_connection_status", unavailable)
    response = portal["client"].get("/api/connections/egress", headers=portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    assert "internal-client-secret" not in response.text
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"] == {
        "provider": "google_drive",
        "display_name": "Test Drive",
        "connected": False,
        "account_label": None,
        "can_connect": False,
        "reset_required": False,
    }
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 200
