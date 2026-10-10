"""Personal egress credentials API authorization and write-only guarantee."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.api import connections, egress_credentials, egress_status, main, oauth
from mindroom.api.connection_agents import build_connection_agent_target
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker import secrets
from mindroom.egress_broker.audit import AuditLog, AuditRecord
from mindroom.egress_broker.oauth_source import Token, resolve_oauth_token
from mindroom.egress_broker.presets import EGRESS_PRESETS
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
    from collections.abc import Iterator


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
        # A dedicated backend, where a personal agent's worker belongs to one requester.
        "MINDROOM_WORKER_BACKEND": "docker",
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
    """When MINDROOM_CONNECTIONS_AGENT is set, the portal catalog lists the services of eligible agents only."""
    response = egress_portal["client"].get("/api/connections", headers=egress_portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    agents = {agent["agent_name"]: agent for agent in response.json()["agents"]}
    assert {"personal", "shared_dev"} <= agents.keys()
    for name, agent in agents.items():
        assert "egress_services" in agent
        if name in ("personal", "shared_dev"):
            assert {s["name"] for s in agent["egress_services"]} == {"github", "openai"}
        else:
            assert agent["egress_services"] is None


def test_portal_catalog_sends_an_empty_list_for_an_eligible_agent_without_services(
    egress_portal: dict[str, Any],
) -> None:
    """An agent the user may use egress for gets a list even when empty, so the portal can link its services page."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["services"] = {}
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    response = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])

    assert response.status_code == 200, response.text
    agents = {agent["agent_name"]: agent for agent in response.json()["agents"]}
    assert agents["personal"]["egress_services"] == []
    assert agents["shared_dev"]["egress_services"] == []
    assert {name for name, agent in agents.items() if agent["egress_services"] is None} == set(agents) - {
        "personal",
        "shared_dev",
    }
    # The user of the agent keeps writing services, so the list fills as soon as one exists.
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    after = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
    personal = next(agent for agent in after.json()["agents"] if agent["agent_name"] == "personal")
    assert [row["name"] for row in personal["egress_services"]] == ["notes"]


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
    assert shared_dev["egress_services"] is None

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
        "service_account": False,
        "unavailable_reason": None,
        "shared_worker_opt_in": False,
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


def _opt_in_to_shared_workers(portal: dict[str, Any]) -> None:
    """Let the requester-scoped `gh` service use connected accounts on shared and unscoped workers."""
    portal["payload"]["egress_broker"]["services"]["gh"]["oauth_on_shared_workers"] = True
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)


def test_requester_scoped_provider_on_a_shared_agent_is_unavailable_without_the_opt_in(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """Everyone's commands share a shared agent's worker, so a GitHub account is neither used nor offered there."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "personal", "gh", "github")
    assert _egress_row(portal, "bob", "personal", "gh")["oauth"]["connected"] is True

    for user in ("alice", "bob"):
        row = _egress_row(portal, user, "shared_dev", "gh")
        assert row["oauth"] == {
            "provider": "github",
            "display_name": "Test Drive",
            "connected": False,
            "account_label": None,
            "can_connect": False,
            "reset_required": False,
            "service_account": False,
            "unavailable_reason": "shared_sandbox",
            "shared_worker_opt_in": False,
        }
        assert row["active_source"] is None
        assert row["configured"] is False
    # Agent-scoped providers keep their shared connection on the same agent.
    assert _egress_row(portal, "bob", "shared_dev", "drive")["oauth"]["unavailable_reason"] is None


def _use_worker_backend(portal: dict[str, Any], backend: str) -> None:
    paths = replace(portal["paths"], process_env={**portal["paths"].process_env, "MINDROOM_WORKER_BACKEND": backend})
    portal["paths"] = paths
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, portal["payload"])
    _use_runtime_auth_settings(main.app)


def test_requester_scoped_provider_on_a_personal_agent_is_unavailable_on_the_static_runner(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """The static runner serves every user's calls from one process, so a personal agent shares its sandbox there."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "personal", "gh", "github")
    assert _egress_row(portal, "bob", "personal", "gh")["oauth"]["connected"] is True

    _use_worker_backend(portal, "static_runner")

    row = _egress_row(portal, "bob", "personal", "gh")
    assert row["oauth"]["unavailable_reason"] == "shared_sandbox"
    assert (row["oauth"]["connected"], row["oauth"]["can_connect"]) == (False, False)
    assert (row["active_source"], row["configured"]) == (None, False)
    # Agent-scoped providers are not requester-scoped, so the static runner leaves them alone.
    assert _egress_row(portal, "bob", "personal", "drive")["oauth"]["unavailable_reason"] is None

    _opt_in_to_shared_workers(portal)

    opted_in = _egress_row(portal, "bob", "personal", "gh")
    assert (opted_in["oauth"]["connected"], opted_in["oauth"]["unavailable_reason"]) == (True, None)
    assert opted_in["oauth"]["shared_worker_opt_in"] is True
    assert opted_in["active_source"] == "oauth"


def test_requester_scoped_provider_connects_the_requester(oauth_egress_portal: dict[str, Any]) -> None:
    """With the opt-in, GitHub-style providers store the connection per requester even on a shared agent."""
    portal = oauth_egress_portal
    _opt_in_to_shared_workers(portal)
    _connect_account(portal, "bob", "shared_dev", "gh", "github")

    bob = _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]
    assert (bob["connected"], bob["unavailable_reason"], bob["shared_worker_opt_in"]) == (True, None, True)
    assert _egress_row(portal, "alice", "shared_dev", "gh")["oauth"]["connected"] is False
    assert _egress_row(portal, "bob", "personal", "gh")["oauth"]["shared_worker_opt_in"] is False


def _enable_service_account(portal: dict[str, Any]) -> None:
    paths = replace(
        portal["paths"],
        process_env={**portal["paths"].process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"},
    )
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, portal["payload"])
    _use_runtime_auth_settings(main.app)


def test_service_account_provider_cannot_be_connected(oauth_egress_portal: dict[str, Any]) -> None:
    """A shared Google service account is runtime configuration, never a personal account."""
    portal = oauth_egress_portal
    _enable_service_account(portal)

    response = portal["client"].post(
        "/api/connections/egress/agents/personal/drive/connect",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert response.status_code == 409, response.text
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["can_connect"] is False


def test_personal_connection_stored_before_a_service_account_can_still_be_disconnected(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """The broker keeps injecting a stored personal token, so the user must be able to revoke it."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    _enable_service_account(portal)
    assert _egress_row(portal, "alice", "personal", "drive")["active_source"] == "oauth"

    response = portal["client"].post(
        "/api/connections/egress/agents/personal/drive/disconnect",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert response.status_code == 200, response.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["oauth"]["connected"] is False
    assert row["oauth"]["service_account"] is True
    assert row["active_source"] is None


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


def test_service_account_is_reported_as_the_broker_sees_it_and_the_portal_keeps_its_semantics(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """The broker cannot inject a service account, so egress shows it as such while the portal status is unchanged."""
    portal = oauth_egress_portal
    _enable_service_account(portal)

    not_connected = {"connected": False, "can_connect": False, "reset_required": False, "account_label": None}
    for user, agent in (("alice", "personal"), ("bob", "personal"), ("alice", "shared_dev"), ("bob", "shared_dev")):
        row = _egress_row(portal, user, agent, "drive")
        assert row["oauth"]["service_account"] is True
        assert _egress_oauth_view(portal, user, agent) == not_connected
        assert row["active_source"] is None
        assert row["configured"] is False
    # The portal keeps its own view: managers see no personal account, plain users of a shared agent see it served.
    assert _portal_oauth_view(portal, "alice", "personal") == not_connected
    assert _portal_oauth_view(portal, "alice", "shared_dev") == {**not_connected, "connected": True}
    assert _portal_oauth_view(portal, "bob", "shared_dev") == not_connected

    put = portal["client"].put(
        "/api/connections/egress/agents/personal/drive",
        json={"secret": "alice-key"},
        headers=portal["headers"]["alice"],
    )
    assert put.status_code == 204, put.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["active_source"] == "key"
    assert row["oauth"]["connected"] is False


def test_service_account_keeps_reporting_a_personal_connection_the_broker_still_uses(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """A stored personal token is injected whatever the service account, so the status matches the broker."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    _enable_service_account(portal)

    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["oauth"] == {
        "provider": "google_drive",
        "display_name": "Test Drive",
        "connected": True,
        "account_label": None,
        "can_connect": False,
        "reset_required": False,
        "service_account": True,
        "unavailable_reason": None,
        "shared_worker_opt_in": False,
    }
    assert row["active_source"] == "oauth"
    config = Config.model_validate(portal["payload"], context={"runtime_paths": portal["paths"]})
    token = resolve_oauth_token(
        service="drive",
        provider_id="google_drive",
        config=config,
        runtime_paths=portal["paths"],
        credentials_manager=get_runtime_credentials_manager(portal["paths"]),
        worker_target=build_connection_agent_target(config, portal["paths"], "@alice:example.org", "personal"),
    )
    assert isinstance(token, Token)


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


@pytest.mark.parametrize(
    "failure",
    [HTTPException(503, "internal-client-secret"), RuntimeError("internal-client-secret")],
)
def test_an_unreadable_connection_state_degrades_one_service_without_failing_the_pages(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Whatever fails while reading one provider's state, that service shows as not connectable and the rest stay."""
    portal = oauth_egress_portal
    real = oauth.agent_connection_status

    async def fails_for_drive(request: Any, provider: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if provider.id == "google_drive":
            raise failure
        return await real(request, provider, *args, **kwargs)

    monkeypatch.setattr(oauth, "agent_connection_status", fails_for_drive)
    degraded = {
        "provider": "google_drive",
        "display_name": "Test Drive",
        "connected": False,
        "account_label": None,
        "can_connect": False,
        "reset_required": False,
        "service_account": False,
        "unavailable_reason": None,
        "shared_worker_opt_in": False,
    }
    listing = portal["client"].get("/api/connections/egress", headers=portal["headers"]["alice"])
    assert listing.status_code == 200, listing.text
    catalog = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
    assert catalog.status_code == 200, catalog.text
    assert "internal-client-secret" not in listing.text + catalog.text
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"] == degraded
    assert _egress_row(portal, "alice", "personal", "gh")["oauth"]["can_connect"] is True
    personal = next(agent for agent in catalog.json()["agents"] if agent["agent_name"] == "personal")
    rows = {service["name"]: service for service in personal["egress_services"]}
    assert rows["drive"]["oauth"] == degraded
    assert rows["gh"]["oauth"]["can_connect"] is True


def test_portal_routes_never_load_egress_oauth_status(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the catalog reads egress OAuth state; status, connect and disconnect of other providers do not."""
    portal = oauth_egress_portal
    client, headers = portal["client"], portal["headers"]["alice"]
    calls: list[str] = []
    real = connections.egress_services_for_agent

    async def counted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(args[1])
        return await real(*args, **kwargs)

    monkeypatch.setattr(connections, "egress_services_for_agent", counted)
    base = "/api/connections/agents/personal/google_drive"
    assert client.get(f"{base}/status", headers=headers).status_code == 200
    connect = client.post(f"{base}/connect", headers=headers, json={})
    assert connect.status_code == 200, connect.text
    assert client.post(f"{base}/disconnect", headers=headers, json={}).status_code == 200
    assert calls == []

    assert client.get("/api/connections", headers=headers).status_code == 200
    assert "personal" in calls


def test_key_status_is_read_off_the_event_loop(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a key's status decrypts a credential file, so the async listing runs it in a thread."""
    portal = oauth_egress_portal
    real = secrets.secret_status
    on_loop: list[bool] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(secrets, "secret_status", recorded)
    assert portal["client"].get("/api/connections/egress", headers=portal["headers"]["alice"]).status_code == 200
    assert portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).status_code == 200
    assert on_loop
    assert not any(on_loop)


def test_disconnecting_a_requester_scoped_account_leaves_other_users_connected(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """On a shared agent with the opt-in each user's GitHub connection is their own."""
    portal = oauth_egress_portal
    _opt_in_to_shared_workers(portal)
    _connect_account(portal, "alice", "shared_dev", "gh", "github")
    _connect_account(portal, "bob", "shared_dev", "gh", "github")

    response = portal["client"].post(
        "/api/connections/egress/agents/shared_dev/gh/disconnect",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert response.status_code == 200, response.text
    assert _egress_row(portal, "alice", "shared_dev", "gh")["oauth"]["connected"] is False
    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["connected"] is True


def test_plain_user_of_a_shared_agent_connects_their_own_requester_scoped_account(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """GitHub-style connections belong to the requester, so with the opt-in connecting needs no management."""
    portal = oauth_egress_portal
    _opt_in_to_shared_workers(portal)
    client = portal["client"]

    before = _egress_row(portal, "alice", "shared_dev", "gh")
    assert before["can_manage"] is False
    assert before["oauth"]["can_connect"] is True
    _connect_account(portal, "alice", "shared_dev", "gh", "github")

    alice = _egress_row(portal, "alice", "shared_dev", "gh")
    assert alice["oauth"]["connected"] is True
    assert alice["oauth"]["account_label"] == "alice@example.com"
    assert alice["active_source"] == "oauth"
    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["connected"] is False

    # The API key stays a shared credential that only managers may set.
    put = client.put(
        "/api/connections/egress/agents/shared_dev/gh",
        json={"secret": "alice-key"},
        headers=portal["headers"]["alice"],
    )
    assert put.status_code == 403, put.text

    disconnect = client.post(
        "/api/connections/egress/agents/shared_dev/gh/disconnect",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert disconnect.status_code == 200, disconnect.text
    assert _egress_row(portal, "alice", "shared_dev", "gh")["oauth"]["connected"] is False


def test_agent_scoped_provider_on_a_shared_agent_still_needs_management(oauth_egress_portal: dict[str, Any]) -> None:
    """A shared connection is shared state: plain users cannot connect it and are not offered the action."""
    portal = oauth_egress_portal
    assert _egress_row(portal, "alice", "shared_dev", "drive")["oauth"]["can_connect"] is False
    for action in ("connect", "disconnect"):
        response = portal["client"].post(
            f"/api/connections/egress/agents/shared_dev/drive/{action}",
            headers=portal["headers"]["alice"],
            json={},
        )
        assert response.status_code == 403, response.text


# --- User-defined services (spec 11.1) ---

_NOTES = {
    "description": "Team notes",
    "rules": [{"host": "api.notes.example.com", "auth": {"type": "bearer"}}],
}


def _service_url(agent: str, name: str) -> str:
    return f"/api/connections/egress/agents/{agent}/services/{name}"


def _put_service(
    portal: dict[str, Any],
    user: str,
    agent: str,
    name: str,
    body: dict[str, Any] | None = None,
) -> Any:  # noqa: ANN401
    return portal["client"].put(
        _service_url(agent, name),
        json=_NOTES if body is None else body,
        headers=portal["headers"][user],
    )


def _get_service(portal: dict[str, Any], user: str, agent: str, name: str) -> Any:  # noqa: ANN401
    return portal["client"].get(_service_url(agent, name), headers=portal["headers"][user])


def _delete_service(portal: dict[str, Any], user: str, agent: str, name: str) -> Any:  # noqa: ANN401
    return portal["client"].delete(_service_url(agent, name), headers=portal["headers"][user])


def _listed_services(portal: dict[str, Any], user: str, agent: str) -> list[dict[str, Any]]:
    response = portal["client"].get("/api/connections/egress", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["agents"] if item["agent_name"] == agent)["services"]


def _listed_names(portal: dict[str, Any], user: str, agent: str) -> list[str]:
    return [service["name"] for service in _listed_services(portal, user, agent)]


def test_user_service_is_created_read_updated_and_deleted(egress_portal: dict[str, Any]) -> None:
    """A requester manages a service of their own on a private agent, read back as authored."""
    portal = egress_portal
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404

    created = _put_service(portal, "alice", "personal", "notes")
    assert created.status_code == 204, created.text
    read = _get_service(portal, "alice", "personal", "notes")
    assert read.status_code == 200, read.text
    assert read.json() == _NOTES
    assert read.headers["cache-control"] == "private, no-store"

    updated_body = {**_NOTES, "description": "Renamed", "restrict_to_rules": True}
    assert _put_service(portal, "alice", "personal", "notes", updated_body).status_code == 204
    assert _get_service(portal, "alice", "personal", "notes").json() == updated_body
    assert _listed_names(portal, "alice", "personal").count("notes") == 1

    deleted = _delete_service(portal, "alice", "personal", "notes")
    assert deleted.status_code == 204, deleted.text
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404
    assert "notes" not in _listed_names(portal, "alice", "personal")
    assert _delete_service(portal, "alice", "personal", "notes").status_code == 404


def test_user_service_keeps_a_preset_as_authored(egress_portal: dict[str, Any]) -> None:
    """A preset stays a preset in the stored form, while the listing shows the preset's expanded name and text."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "my-github", {"preset": "github"}).status_code == 204
    assert _get_service(portal, "alice", "personal", "my-github").json() == {"preset": "github"}
    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "my-github")
    assert row["display_name"] == "GitHub"
    assert row["source"] == "user"


def test_user_service_cannot_take_a_config_service_name(egress_portal: dict[str, Any]) -> None:
    """The operator's service wins: creating or deleting a service under a config name is a 409."""
    portal = egress_portal
    created = _put_service(portal, "alice", "personal", "github")
    assert created.status_code == 409, created.text
    assert "administrator" in created.json()["detail"]
    assert _get_service(portal, "alice", "personal", "github").status_code == 404

    deleted = _delete_service(portal, "alice", "personal", "github")
    assert deleted.status_code == 409, deleted.text
    assert "administrator" in deleted.json()["detail"]
    assert _listed_names(portal, "alice", "personal") == ["github", "openai"]


def test_deleting_a_config_service_name_keeps_its_stored_key(egress_portal: dict[str, Any]) -> None:
    """The refused delete cannot be used to wipe the key of a service the administrator defines."""
    portal = egress_portal
    put = portal["client"].put(
        "/api/connections/egress/agents/personal/github",
        json={"secret": "alice-token"},
        headers=portal["headers"]["alice"],
    )
    assert put.status_code == 204, put.text
    assert _delete_service(portal, "alice", "personal", "github").status_code == 409
    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "github")
    assert row["key_configured"] is True


def test_an_entry_shadowed_by_a_new_config_service_can_be_removed_and_keeps_the_key(
    egress_portal: dict[str, Any],
) -> None:
    """When the administrator later defines a service under a user entry's name, the entry is ignored but removable."""
    portal = egress_portal
    client, alice = portal["client"], portal["headers"]["alice"]
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert (
        client.put(
            "/api/connections/egress/agents/personal/notes",
            json={"secret": "alice-token"},
            headers=alice,
        ).status_code
        == 204
    )

    portal["payload"]["egress_broker"]["services"]["notes"] = {
        "description": "Administrator notes",
        "rules": [{"host": "notes.admin.example.com", "auth": {"type": "bearer"}}],
    }
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "notes")
    assert (row["source"], row["description"], row["key_configured"]) == ("config", "Administrator notes", True)
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404
    assert _put_service(portal, "alice", "personal", "notes").status_code == 409

    assert _delete_service(portal, "alice", "personal", "notes").status_code == 204
    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "notes")
    assert (row["source"], row["key_configured"]) == ("config", True)
    # With the entry gone the name is a plain config service again.
    assert _delete_service(portal, "alice", "personal", "notes").status_code == 409


def test_oauth_on_shared_workers_is_refused_whatever_its_value(egress_portal: dict[str, Any]) -> None:
    """The operator-only flag is rejected by name, so a user cannot even send it as false."""
    portal = egress_portal
    for value in (True, False):
        response = _put_service(portal, "alice", "personal", "notes", {**_NOTES, "oauth_on_shared_workers": value})
        assert response.status_code == 422, response.text
        assert "oauth_on_shared_workers" in response.json()["detail"]
        assert "config.yaml" in response.json()["detail"]
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"description": "no rules"}, "at least one rule"),
        ({**_NOTES, "unknown_field": 1}, "unknown_field"),
        ({**_NOTES, "preset": "no_such_preset"}, "unknown egress preset"),
        (
            {"rules": [{"host": "https://api.notes.example.com", "auth": {"type": "bearer"}}]},
            "host must not contain scheme",
        ),
        ({**_NOTES, "placeholder_env": {"MINDROOM_X": "v"}}, "reserved prefix"),
    ],
)
def test_invalid_user_service_is_422_with_the_validators_message(
    egress_portal: dict[str, Any],
    body: dict[str, Any],
    fragment: str,
) -> None:
    """Config validation errors reach the user as a readable 422 and nothing is stored."""
    response = _put_service(egress_portal, "alice", "personal", "notes", body)
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]
    assert _get_service(egress_portal, "alice", "personal", "notes").status_code == 404


def test_non_object_body_and_bad_names_are_422(egress_portal: dict[str, Any]) -> None:
    """A body that is not an object and names the store reserves or the pattern forbids are refused."""
    portal = egress_portal
    not_object = portal["client"].put(
        _service_url("personal", "notes"),
        json=["rules"],
        headers=portal["headers"]["alice"],
    )
    assert not_object.status_code == 422, not_object.text
    for name in ("_services", "Notes", "x_oauth"):
        response = _put_service(portal, "alice", "personal", name)
        assert response.status_code == 422, (name, response.text)


def test_user_service_limits_are_422(egress_portal: dict[str, Any]) -> None:
    """At most 50 rules per service and 50 services per scope; replacing a service at the limit still works."""
    portal = egress_portal
    too_many_rules = {"rules": [{"host": f"h{i}.example.com", "auth": {"type": "bearer"}} for i in range(51)]}
    response = _put_service(portal, "alice", "personal", "wide", too_many_rules)
    assert response.status_code == 422, response.text
    assert "at most 50 rules" in response.json()["detail"]

    for i in range(50):
        assert _put_service(portal, "alice", "personal", f"svc{i}").status_code == 204
    response = _put_service(portal, "alice", "personal", "svc50")
    assert response.status_code == 422, response.text
    assert "at most 50 services" in response.json()["detail"]
    assert _put_service(portal, "alice", "personal", "svc0", {**_NOTES, "description": "again"}).status_code == 204
    # Another requester's scope is unaffected by Alice's count.
    assert _put_service(portal, "bob", "personal", "svc50").status_code == 204


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({**_NOTES, "placeholder_env": {"LD_PRELOAD": "evil"}}, "placeholder_env name 'LD_PRELOAD'"),
        ({**_NOTES, "placeholder_env": {"NOTES_TOKEN": "two words"}}, "placeholder_env value of 'NOTES_TOKEN'"),
        ({**_NOTES, "oauth_provider": "no_such_provider"}, "unknown oauth_provider 'no_such_provider'"),
    ],
)
def test_user_service_placeholders_and_provider_are_422(
    egress_portal: dict[str, Any],
    body: dict[str, Any],
    fragment: str,
) -> None:
    """User-only rules on placeholders and OAuth providers reach the user as a 422 and nothing is stored."""
    response = _put_service(egress_portal, "alice", "personal", "notes", body)
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]
    assert "two words" not in response.json()["detail"]
    assert _get_service(egress_portal, "alice", "personal", "notes").status_code == 404


def test_user_service_body_over_16_kib_is_413_before_validation(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized body, such as a huge rules list, is refused before the service model validates any of it."""
    parsed: list[object] = []
    monkeypatch.setattr(egress_credentials, "_parse_user_service", parsed.append)
    huge_rules = {"rules": [{"host": f"h{i}.example.com", "auth": {"type": "bearer"}} for i in range(2000)]}
    for body in (huge_rules, {**_NOTES, "description": "x" * 17_000}):
        response = _put_service(egress_portal, "alice", "personal", "notes", body)
        assert response.status_code == 413, response.text
        assert response.json()["detail"] == "A service can take at most 16 KiB"

    assert parsed == []
    assert _get_service(egress_portal, "alice", "personal", "notes").status_code == 404


def test_oauth_provider_is_refused_in_user_services_of_shared_agents(egress_portal: dict[str, Any]) -> None:
    """A manager cannot route a shared agent's OAuth connection to hosts of their choosing; personal agents can."""
    portal = egress_portal
    with_oauth = {**_NOTES, "oauth_provider": "github"}
    for body in (with_oauth, {"preset": "github"}):
        response = _put_service(portal, "bob", "shared_dev", "notes", body)
        assert response.status_code == 422, response.text
        assert "oauth_provider" in response.json()["detail"]
    assert _get_service(portal, "bob", "shared_dev", "notes").status_code == 404

    assert _put_service(portal, "bob", "shared_dev", "notes", {**_NOTES, "oauth_provider": None}).status_code == 204
    assert _put_service(portal, "alice", "personal", "notes", with_oauth).status_code == 204
    assert _get_service(portal, "alice", "personal", "notes").json() == with_oauth


def test_deny_refuses_user_rules_on_hosts_the_operator_does_not_name(egress_portal: dict[str, Any]) -> None:
    """Under unmatched_hosts: deny a user service may only use hosts a config service already has rules for."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["unmatched_hosts"] = "deny"
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    mixed = {
        "rules": [
            {"host": "api.notes.example.com", "auth": {"type": "bearer"}},
            {"host": "api.github.com", "path_prefix": "/repos/alice/", "auth": {"type": "bearer"}},
            {"host": "files.notes.example.com", "auth": {"type": "bearer"}},
        ],
    }
    response = _put_service(portal, "alice", "personal", "notes", mixed)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "api.notes.example.com" in detail
    assert "files.notes.example.com" in detail
    assert "api.github.com" not in detail
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404

    narrowed = {"rules": [mixed["rules"][1]]}
    assert _put_service(portal, "alice", "personal", "repos", narrowed).status_code == 204
    # Shared agents follow the same rule.
    assert _put_service(portal, "bob", "shared_dev", "notes").status_code == 422


def test_non_manager_cannot_write_services_of_a_shared_agent(egress_portal: dict[str, Any]) -> None:
    """On a shared agent managers write, and every user of the agent reads the same services."""
    portal = egress_portal
    for response in (
        _put_service(portal, "alice", "shared_dev", "notes"),
        _delete_service(portal, "alice", "shared_dev", "x"),
    ):
        assert response.status_code == 403, response.text
        assert "Credential management is required" in response.text
    assert _get_service(portal, "bob", "shared_dev", "notes").status_code == 404

    assert _put_service(portal, "bob", "shared_dev", "notes").status_code == 204
    assert _get_service(portal, "alice", "shared_dev", "notes").json() == _NOTES
    alice_row = next(s for s in _listed_services(portal, "alice", "shared_dev") if s["name"] == "notes")
    bob_row = next(s for s in _listed_services(portal, "bob", "shared_dev") if s["name"] == "notes")
    assert (alice_row["source"], alice_row["can_manage"], alice_row["is_shared"]) == ("user", False, True)
    assert (bob_row["source"], bob_row["can_manage"], bob_row["is_shared"]) == ("user", True, True)

    assert _delete_service(portal, "alice", "shared_dev", "notes").status_code == 403
    assert _get_service(portal, "alice", "shared_dev", "notes").status_code == 200
    assert _delete_service(portal, "bob", "shared_dev", "notes").status_code == 204
    assert "notes" not in _listed_names(portal, "alice", "shared_dev")
    # A manager who may not use the agent has no access at all, as for keys.
    assert _put_service(portal, "carol", "shared_dev", "notes").status_code == 404
    assert _get_service(portal, "carol", "shared_dev", "notes").status_code == 404


@pytest.mark.parametrize("agent", ["no_shell", "other_private", "unknown"])
def test_service_routes_hide_ineligible_agents(egress_portal: dict[str, Any], agent: str) -> None:
    """Unknown agents, agents without a shell or python tool, and agents the user may not use are all 404."""
    portal = egress_portal
    assert _put_service(portal, "alice", agent, "notes").status_code == 404
    assert _get_service(portal, "alice", agent, "notes").status_code == 404
    assert _delete_service(portal, "alice", agent, "notes").status_code == 404


def test_service_writes_require_the_same_origin(egress_portal: dict[str, Any]) -> None:
    """A write from another origin is refused before anything is validated or stored; reads are not affected."""
    portal = egress_portal
    client = portal["client"]
    evil = {**portal["headers"]["alice"], "Origin": "https://evil.example.com"}
    url = _service_url("personal", "notes")

    assert client.put(url, json=_NOTES, headers=evil).status_code == 403
    assert client.put(url, json={"description": "invalid"}, headers=evil).status_code == 403
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404

    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert client.delete(url, headers=evil).status_code == 403
    assert client.get(url, headers=evil).status_code == 200
    assert _get_service(portal, "alice", "personal", "notes").status_code == 200


def test_service_routes_require_a_signed_user(egress_portal: dict[str, Any]) -> None:
    """Without the signed identity the routes answer like the listing does."""
    client = egress_portal["client"]
    expected = client.get("/api/connections/egress").status_code
    assert expected in {401, 403}
    assert client.get(_service_url("personal", "notes")).status_code == expected
    assert client.put(_service_url("personal", "notes"), json=_NOTES).status_code in {401, 403}
    assert client.delete(_service_url("personal", "notes")).status_code in {401, 403}


def test_personal_services_never_reach_another_requester(egress_portal: dict[str, Any]) -> None:
    """Alice's services on a private agent are invisible to Bob's listing, catalog, reads, and writes."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204

    assert _listed_names(portal, "alice", "personal") == ["github", "openai", "notes"]
    assert _listed_names(portal, "bob", "personal") == ["github", "openai"]
    assert _listed_names(portal, "alice", "shared_dev") == ["github", "openai"]
    for user, expected in (("alice", True), ("bob", False)):
        catalog = portal["client"].get("/api/connections", headers=portal["headers"][user])
        assert catalog.status_code == 200, catalog.text
        personal = next(a for a in catalog.json()["agents"] if a["agent_name"] == "personal")
        assert ("notes" in {s["name"] for s in personal["egress_services"]}) is expected

    assert _get_service(portal, "bob", "personal", "notes").status_code == 404
    assert _delete_service(portal, "bob", "personal", "notes").status_code == 404
    key = portal["client"].put(
        "/api/connections/egress/agents/personal/notes",
        json={"secret": "bob-token"},
        headers=portal["headers"]["bob"],
    )
    assert key.status_code == 404, key.text

    # Bob's same-named service is his own and leaves Alice's untouched.
    other = {"rules": [{"host": "api.bobs.example.com", "auth": {"type": "bearer"}}]}
    assert _put_service(portal, "bob", "personal", "notes", other).status_code == 204
    assert _get_service(portal, "bob", "personal", "notes").json() == other
    assert _get_service(portal, "alice", "personal", "notes").json() == _NOTES
    assert _delete_service(portal, "bob", "personal", "notes").status_code == 204
    assert _get_service(portal, "alice", "personal", "notes").json() == _NOTES


def test_a_bridge_alias_shares_the_canonical_requesters_services(egress_portal: dict[str, Any]) -> None:
    """A requester reached through a human alias manages the canonical requester's services."""
    portal = egress_portal
    portal["payload"]["authorization"] = {"aliases": {"@alice:example.org": ["@bob:example.org"]}}
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert _get_service(portal, "bob", "personal", "notes").json() == _NOTES
    assert "notes" in _listed_names(portal, "bob", "personal")


def test_listing_marks_the_source_of_each_service(egress_portal: dict[str, Any]) -> None:
    """Config services are listed first with source config, then the scope's own with source user."""
    portal = egress_portal
    for agent in ("personal", "shared_dev"):
        assert {s["source"] for s in _listed_services(portal, "alice", agent)} == {"config"}

    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    rows = _listed_services(portal, "alice", "personal")
    assert [(row["name"], row["source"]) for row in rows] == [
        ("github", "config"),
        ("openai", "config"),
        ("notes", "user"),
    ]
    notes = rows[2]
    assert (notes["display_name"], notes["description"], notes["can_manage"], notes["is_shared"]) == (
        "Notes",
        "Team notes",
        True,
        False,
    )
    assert (notes["configured"], notes["key_configured"], notes["oauth"]) == (False, False, None)

    catalog = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
    personal = next(a for a in catalog.json()["agents"] if a["agent_name"] == "personal")
    assert {(s["name"], s["source"]) for s in personal["egress_services"]} == {
        ("github", "config"),
        ("openai", "config"),
        ("notes", "user"),
    }


def test_keys_follow_user_service_names(egress_portal: dict[str, Any]) -> None:
    """A key can be set for a user service; deleting the service removes the key and a new one starts without it."""
    portal = egress_portal
    client, alice = portal["client"], portal["headers"]["alice"]
    key_url = "/api/connections/egress/agents/personal/notes"
    assert client.put(key_url, json={"secret": "alice-token"}, headers=alice).status_code == 404

    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    put = client.put(key_url, json={"secret": "alice-token"}, headers=alice)
    assert put.status_code == 204, put.text
    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "notes")
    assert (row["configured"], row["key_configured"], row["active_source"]) == (True, True, "key")
    assert "alice-token" not in client.get("/api/connections/egress", headers=alice).text

    assert client.delete(key_url, headers=alice).status_code == 204
    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "notes")
    assert row["key_configured"] is False

    assert client.put(key_url, json={"secret": "second"}, headers=alice).status_code == 204
    assert _delete_service(portal, "alice", "personal", "notes").status_code == 204
    assert client.put(key_url, json={"secret": "third"}, headers=alice).status_code == 404
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    row = next(s for s in _listed_services(portal, "alice", "personal") if s["name"] == "notes")
    assert row["key_configured"] is False


def test_user_service_shares_a_key_gate_with_config_services(egress_portal: dict[str, Any]) -> None:
    """Keys of a shared agent's user service need management like its config services' keys."""
    portal = egress_portal
    assert _put_service(portal, "bob", "shared_dev", "notes").status_code == 204
    url = "/api/connections/egress/agents/shared_dev/notes"
    assert portal["client"].put(url, json={"secret": "x"}, headers=portal["headers"]["alice"]).status_code == 403
    assert portal["client"].put(url, json={"secret": "x"}, headers=portal["headers"]["bob"]).status_code == 204
    assert _listed_services(portal, "alice", "shared_dev")[-1]["key_configured"] is True


def test_user_service_can_connect_its_providers_account(oauth_egress_portal: dict[str, Any]) -> None:
    """A personal service whose OAuth provider is requester-scoped connects and disconnects like a config service."""
    portal = oauth_egress_portal
    body = {**_NOTES, "oauth_provider": "github"}
    assert _put_service(portal, "alice", "personal", "mygh", body).status_code == 204

    _connect_account(portal, "alice", "personal", "mygh", "github")
    row = _egress_row(portal, "alice", "personal", "mygh")
    assert row["source"] == "user"
    assert (row["oauth"]["provider"], row["oauth"]["connected"], row["active_source"]) == ("github", True, "oauth")

    disconnect = portal["client"].post(
        "/api/connections/egress/agents/personal/mygh/disconnect",
        headers=portal["headers"]["alice"],
        json={},
    )
    assert disconnect.status_code == 200, disconnect.text
    assert _egress_row(portal, "alice", "personal", "mygh")["oauth"]["connected"] is False
    # Bob has no such service, so the account routes do not exist for him.
    for action in ("connect", "disconnect"):
        response = portal["client"].post(
            f"/api/connections/egress/agents/personal/mygh/{action}",
            headers=portal["headers"]["bob"],
            json={},
        )
        assert response.status_code == 404, response.text


def test_the_admin_panel_shows_a_shared_agents_user_services_read_only_and_keys_them(
    egress_portal: dict[str, Any],
) -> None:
    """Services a manager defines for a shared agent show on the dashboard for that scope, where keys can be set."""
    portal = egress_portal
    client, admin = portal["client"], portal["headers"]["admin"]
    assert _put_service(portal, "bob", "shared_dev", "notes").status_code == 204
    assert _put_service(portal, "alice", "personal", "mine").status_code == 204

    panel = client.get("/api/egress-broker/services", params={"agent_name": "shared_dev"}, headers=admin)
    assert panel.status_code == 200, panel.text
    assert [(s["name"], s["source"]) for s in panel.json()["services"]] == [
        ("github", "config"),
        ("openai", "config"),
        ("notes", "user"),
    ]
    # Another scope shows neither the shared agent's services nor a requester's personal ones.
    unscoped = client.get("/api/egress-broker/services", headers=admin)
    assert [s["name"] for s in unscoped.json()["services"]] == ["github", "openai"]

    put = client.put(
        "/api/egress-broker/services/notes/secret?agent_name=shared_dev",
        json={"secret": "k"},
        headers=admin,
    )
    assert put.status_code == 204, put.text
    notes = next(s for s in _listed_services(portal, "alice", "shared_dev") if s["name"] == "notes")
    assert notes["key_configured"] is True
    assert (
        client.put("/api/egress-broker/services/notes/secret", json={"secret": "k"}, headers=admin).status_code == 404
    )
    assert (
        client.put(
            "/api/egress-broker/services/mine/secret?agent_name=shared_dev",
            json={"secret": "k"},
            headers=admin,
        ).status_code
        == 404
    )

    delete = client.delete("/api/egress-broker/services/notes/secret?agent_name=shared_dev", headers=admin)
    assert delete.status_code == 204, delete.text
    notes = next(s for s in _listed_services(portal, "alice", "shared_dev") if s["name"] == "notes")
    assert notes["key_configured"] is False


def test_user_services_are_loaded_off_the_event_loop(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a scope's services touches the credential store, so async routes do it in a thread."""
    portal = oauth_egress_portal
    assert _put_service(portal, "alice", "personal", "mygh", {**_NOTES, "oauth_provider": "github"}).status_code == 204
    real = egress_status.effective_config
    on_loop: list[bool] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(egress_status, "effective_config", recorded)
    client, headers = portal["client"], portal["headers"]["alice"]
    assert client.get("/api/connections/egress", headers=headers).status_code == 200
    assert client.get("/api/connections", headers=headers).status_code == 200
    connect = client.post("/api/connections/egress/agents/personal/mygh/connect", headers=headers, json={})
    assert connect.status_code == 200, connect.text
    assert client.put(_service_url("personal", "second"), json=_NOTES, headers=headers).status_code == 204
    assert on_loop
    assert not any(on_loop)


# --- Personal request log (spec 11.3) ---


@pytest.fixture
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuditLog]:
    """Serve the personal log from a real audit log, as if the broker were running."""
    log = AuditLog(tmp_path / "audit.sqlite3")
    monkeypatch.setattr(egress_credentials, "active_audit_log", lambda: log)
    yield log
    log.close()


_LOG_START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _record_request(
    log: AuditLog,
    requester_id: str | None,
    agent_name: str,
    seconds: int,
    *,
    host: str = "api.github.com",
    path: str = "/user",
    kind: Literal["request", "tunnel", "denied"] = "request",
    status: int = 200,
    code: str | None = None,
) -> None:
    log.record(
        AuditRecord(
            at=_LOG_START + timedelta(seconds=seconds),
            kind=kind,
            scope="shared" if agent_name == "shared_dev" else "user_agent",
            agent_name=agent_name,
            requester_id=requester_id,
            method="GET",
            host=host,
            path=path,
            service="github",
            status=status,
            bytes_up=1,
            bytes_down=2,
            duration_ms=3,
            code=code,
        ),
    )


def _get_logs(portal: dict[str, Any], user: str, query: str = "") -> Any:  # noqa: ANN401
    return portal["client"].get(f"/api/connections/egress/logs{query}", headers=portal["headers"][user])


def _log_paths(response: Any) -> list[str]:  # noqa: ANN401
    assert response.status_code == 200, response.text
    return [record["path"] for record in response.json()["records"]]


@pytest.fixture
def mixed_log(audit_log: AuditLog) -> AuditLog:
    """Alice and Bob both used the shared agent and each their own private agent; one row has no requester."""
    _record_request(audit_log, "@alice:example.org", "personal", 1, path="/alice/personal/old")
    _record_request(audit_log, "@bob:example.org", "personal", 2, path="/bob/personal")
    _record_request(audit_log, "@alice:example.org", "shared_dev", 3, path="/alice/shared")
    _record_request(audit_log, "@bob:example.org", "shared_dev", 4, path="/bob/shared")
    _record_request(audit_log, None, "shared_dev", 5, path="/nobody/shared")
    _record_request(audit_log, "@alice:example.org", "personal", 6, path="/alice/personal/new")
    return audit_log


def test_personal_log_returns_only_the_callers_rows_newest_first(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,  # noqa: ARG001
) -> None:
    """Each user sees their own rows across agents, including a shared agent others used too."""
    portal = egress_portal
    alice = _get_logs(portal, "alice")
    assert _log_paths(alice) == ["/alice/personal/new", "/alice/shared", "/alice/personal/old"]
    assert alice.headers["cache-control"] == "private, no-store"
    assert "bob" not in alice.text
    assert "nobody" not in alice.text
    first = alice.json()["records"][0]
    assert first == {
        "at": "2026-10-01T12:00:06+00:00",
        "kind": "request",
        "scope": "user_agent",
        "agent_name": "personal",
        "requester_id": "@alice:example.org",
        "method": "GET",
        "host": "api.github.com",
        "path": "/alice/personal/new",
        "service": "github",
        "status": 200,
        "bytes_up": 1,
        "bytes_down": 2,
        "duration_ms": 3,
        "code": None,
    }

    bob = _get_logs(portal, "bob")
    assert _log_paths(bob) == ["/bob/shared", "/bob/personal"]
    assert "alice" not in bob.text
    assert _log_paths(_get_logs(portal, "carol")) == []


def test_personal_log_agent_filter_never_reveals_other_users_rows(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,  # noqa: ARG001
) -> None:
    """Filtering by an agent that several users share still returns only the caller's rows."""
    portal = egress_portal
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=shared_dev")) == ["/alice/shared"]
    assert _log_paths(_get_logs(portal, "bob", "?agent_name=shared_dev")) == ["/bob/shared"]
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=personal")) == [
        "/alice/personal/new",
        "/alice/personal/old",
    ]
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=no_such_agent")) == []
    # An empty filter, as a form sends it, means no filter.
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=&limit=2")) == ["/alice/personal/new", "/alice/shared"]
    # Carol may manage shared_dev but never used it, so she has no rows on it.
    assert _log_paths(_get_logs(portal, "carol", "?agent_name=shared_dev")) == []


def test_personal_log_ignores_every_other_filter(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,  # noqa: ARG001
) -> None:
    """Filters that would select another requester, or anything the page does not offer, are refused."""
    portal = egress_portal
    for query in ("?requester_id=@bob:example.org", "?host=api.github.com", "?service=github", "?x=1"):
        response = _get_logs(portal, "alice", query)
        assert response.status_code == 400, (query, response.text)
        assert "bob" not in response.text


def test_personal_log_limit_is_clamped(egress_portal: dict[str, Any], audit_log: AuditLog) -> None:
    """The limit is clamped to 1..200, newest rows first."""
    portal = egress_portal
    for i in range(205):
        _record_request(audit_log, "@alice:example.org", "personal", i, path=f"/p{i}")
    _record_request(audit_log, "@bob:example.org", "personal", 500, path="/bob")

    newest = [f"/p{i}" for i in range(204, -1, -1)]
    assert _log_paths(_get_logs(portal, "alice", "?limit=3")) == newest[:3]
    assert _log_paths(_get_logs(portal, "alice", "?limit=0")) == newest[:1]
    assert _log_paths(_get_logs(portal, "alice", "?limit=-5")) == newest[:1]
    assert _log_paths(_get_logs(portal, "alice", "?limit=1000")) == newest[:200]
    assert _log_paths(_get_logs(portal, "alice", "?limit=200")) == newest[:200]
    assert _log_paths(_get_logs(portal, "alice")) == newest[:200]
    assert _get_logs(portal, "alice", "?limit=many").status_code == 422


def test_personal_log_follows_the_canonical_requester(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,  # noqa: ARG001
) -> None:
    """A caller reached through a human alias sees the canonical requester's rows, as the broker's claims carry."""
    portal = egress_portal
    portal["payload"]["authorization"] = {"aliases": {"@alice:example.org": ["@bob:example.org"]}}
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    # Bob now acts as Alice: the rows logged for the canonical id are his, and the alias id has none of its own.
    assert _log_paths(_get_logs(portal, "bob")) == ["/alice/personal/new", "/alice/shared", "/alice/personal/old"]
    assert _log_paths(_get_logs(portal, "alice")) == _log_paths(_get_logs(portal, "bob"))
    assert _log_paths(_get_logs(portal, "carol")) == []


def test_personal_log_is_unavailable_while_the_broker_is_stopped(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like the administrator's log, the personal log answers 409 when no broker runs."""
    monkeypatch.setattr(egress_credentials, "active_audit_log", lambda: None)
    response = _get_logs(egress_portal, "alice")
    assert response.status_code == 409, response.text
    assert "not running" in response.json()["detail"]


def test_personal_log_requires_a_signed_user(egress_portal: dict[str, Any], audit_log: AuditLog) -> None:  # noqa: ARG001
    """Without the signed identity the log answers like the listing does."""
    client = egress_portal["client"]
    expected = client.get("/api/connections/egress").status_code
    assert expected in {401, 403}
    assert client.get("/api/connections/egress/logs").status_code == expected
    assert client.get("/api/connections/egress/logs?agent_name=personal").status_code == expected


# --- Agent flags, rule summaries, presets, and refusal codes in the listing and the log ---


def _listed_agents(portal: dict[str, Any], user: str) -> dict[str, dict[str, Any]]:
    response = portal["client"].get("/api/connections/egress", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    return {agent["agent_name"]: agent for agent in response.json()["agents"]}


def test_listing_reports_whether_each_agent_is_shared_and_whether_the_caller_manages_it(
    egress_portal: dict[str, Any],
) -> None:
    """Every agent says if its services are shared and if the caller may change them, as the write routes decide."""
    portal = egress_portal
    alice = _listed_agents(portal, "alice")
    assert (alice["personal"]["shared"], alice["personal"]["can_manage"]) == (False, True)
    assert (alice["shared_dev"]["shared"], alice["shared_dev"]["can_manage"]) == (True, False)
    bob = _listed_agents(portal, "bob")
    assert (bob["personal"]["shared"], bob["personal"]["can_manage"]) == (False, True)
    assert (bob["shared_dev"]["shared"], bob["shared_dev"]["can_manage"]) == (True, True)
    # The per-service flags the page already used say the same.
    for agent in (*alice.values(), *bob.values()):
        assert {service["can_manage"] for service in agent["services"]} == {agent["can_manage"]}
        assert {service["is_shared"] for service in agent["services"]} == {agent["shared"]}
    # What the flags promise is what the write routes do.
    assert _put_service(portal, "alice", "shared_dev", "mine").status_code == 403
    assert _put_service(portal, "bob", "shared_dev", "mine").status_code == 204


def test_an_agent_without_services_still_reports_the_flags(egress_portal: dict[str, Any]) -> None:
    """With no service to carry them, the agent's own flags tell the page whether Add service is allowed."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["services"] = {}
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    alice = _listed_agents(portal, "alice")
    assert {agent["agent_name"]: agent["services"] for agent in alice.values()} == {"personal": [], "shared_dev": []}
    assert (alice["personal"]["shared"], alice["personal"]["can_manage"]) == (False, True)
    assert (alice["shared_dev"]["shared"], alice["shared_dev"]["can_manage"]) == (True, False)
    assert _listed_agents(portal, "bob")["shared_dev"]["can_manage"] is True


def test_listing_summarizes_where_each_service_applies_and_nothing_else(egress_portal: dict[str, Any]) -> None:
    """Rules show host, port, and path prefix, in rule order, for config, preset, and user services; never auth."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["services"]["drive"] = {"preset": "google_drive"}
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    own = {
        "rules": [
            {
                "host": "api.own.example.com",
                "port": 8443,
                "path_prefix": "/v1/",
                "auth": {"type": "header", "name": "X-Own-Key", "template": "Token-{secret}"},
            },
            {"host": "*.own.example.com", "auth": {"type": "basic", "username": "bot-user"}},
        ],
    }
    assert _put_service(portal, "alice", "personal", "own", own).status_code == 204

    rows = {service["name"]: service for service in _listed_services(portal, "alice", "personal")}

    assert rows["github"]["rules"] == [{"host": "api.github.com", "port": None, "path_prefix": "/"}]
    assert rows["drive"]["rules"] == [
        {"host": "www.googleapis.com", "port": None, "path_prefix": "/drive/"},
        {"host": "www.googleapis.com", "port": None, "path_prefix": "/upload/drive/"},
    ]
    assert rows["own"]["rules"] == [
        {"host": "api.own.example.com", "port": 8443, "path_prefix": "/v1/"},
        {"host": "*.own.example.com", "port": None, "path_prefix": "/"},
    ]
    listing = portal["client"].get("/api/connections/egress", headers=portal["headers"]["alice"]).text
    for hidden in ("X-Own-Key", "Token-", "bot-user", "x-access-token", "bearer"):
        assert hidden not in listing
    # Another requester's private services never show up in the summaries.
    assert "own" not in {service["name"] for service in _listed_services(portal, "bob", "personal")}
    catalog = portal["client"].get("/api/connections", headers=portal["headers"]["alice"]).json()
    personal = next(agent for agent in catalog["agents"] if agent["agent_name"] == "personal")
    assert {row["name"]: row["rules"] for row in personal["egress_services"]}["own"] == rows["own"]["rules"]


def test_presets_are_served_as_the_config_model_expands_them(egress_portal: dict[str, Any]) -> None:
    """The editor learns every preset's name, login, rules, and placeholders from the server, with no auth settings."""
    response = egress_portal["client"].get("/api/connections/egress/presets", headers=egress_portal["headers"]["alice"])
    assert response.status_code == 200, response.text
    presets = response.json()["presets"]
    assert [preset["id"] for preset in presets] == list(EGRESS_PRESETS)
    by_id = {preset["id"]: preset for preset in presets}
    assert by_id["github"] == {
        "id": "github",
        "display_name": "GitHub",
        "description": "GitHub API, gh CLI, and git over HTTPS",
        "oauth_provider": "github",
        "rules": [
            {"host": "api.github.com", "port": None, "path_prefix": "/"},
            {"host": "uploads.github.com", "port": None, "path_prefix": "/"},
            {"host": "github.com", "port": None, "path_prefix": "/"},
        ],
        "placeholder_env": {"GH_TOKEN": "mindroom-brokered", "GITHUB_TOKEN": "mindroom-brokered"},
    }
    assert by_id["google_gmail"]["rules"] == [
        {"host": "gmail.googleapis.com", "port": None, "path_prefix": "/"},
        {"host": "www.googleapis.com", "port": None, "path_prefix": "/gmail/"},
    ]
    assert by_id["openai"]["oauth_provider"] is None
    assert by_id["openai"]["placeholder_env"] == {"OPENAI_API_KEY": "mindroom-brokered"}
    assert by_id["google_sheets"]["placeholder_env"] == {}
    for preset in presets:
        assert set(preset) == {"id", "display_name", "description", "oauth_provider", "rules", "placeholder_env"}
        assert all(set(rule) == {"host", "port", "path_prefix"} for rule in preset["rules"])
    assert "x-access-token" not in response.text


def test_frontend_preset_fixture_matches_the_served_presets() -> None:
    """The preset fixture the UI tests load is exactly what the presets routes serve, so the two cannot drift."""
    fixture_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "test" / "fixtures" / "egressPresets.json"
    if not fixture_path.exists():
        pytest.skip(f"{fixture_path.relative_to(fixture_path.parents[4])} does not exist yet")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    # The fixture is the payload `{"presets": [...]}` or just its list.
    presets = fixture["presets"] if isinstance(fixture, dict) else fixture

    assert presets == egress_status.presets_response().model_dump(mode="json")["presets"]


def test_presets_need_a_signed_user_and_take_no_query(egress_portal: dict[str, Any]) -> None:
    """The preset list uses the personal auth gate: no identity, no list, and no stray query parameters."""
    client = egress_portal["client"]
    expected = client.get("/api/connections/egress").status_code
    assert expected in {401, 403}
    assert client.get("/api/connections/egress/presets").status_code == expected
    response = client.get("/api/connections/egress/presets?x=1", headers=egress_portal["headers"]["alice"])
    assert response.status_code == 400


def test_personal_log_returns_the_refusal_code_or_null(egress_portal: dict[str, Any], audit_log: AuditLog) -> None:
    """A refused request carries the error code its body had; a forwarded one has null."""
    _record_request(audit_log, "@alice:example.org", "personal", 1, path="/ok")
    _record_request(
        audit_log,
        "@alice:example.org",
        "personal",
        2,
        path="/repos/other/secret",
        kind="denied",
        status=403,
        code="path_not_allowed",
    )

    response = _get_logs(egress_portal, "alice")

    assert response.status_code == 200, response.text
    assert [(row["path"], row["status"], row["code"]) for row in response.json()["records"]] == [
        ("/repos/other/secret", 403, "path_not_allowed"),
        ("/ok", 200, None),
    ]


# --- Final fix wave: refused OAuth, degraded gate, inactive entries, write gates, response headers ---


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_are_refused_where_the_broker_refuses_the_account(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """The status offers nothing in a shared sandbox, so the routes answer 409 with a readable string, not a flow."""
    portal = oauth_egress_portal
    client, bob = portal["client"], portal["headers"]["bob"]
    url = f"/api/connections/egress/agents/shared_dev/gh/{action}"

    response = client.post(url, headers=bob, json={})

    assert response.status_code == 409, response.text
    assert isinstance(response.json()["detail"], str)
    assert "share" in response.json()["detail"]
    assert "no-store" in response.headers["cache-control"]
    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["can_connect"] is False
    # Agent-scoped providers on the same agent stay connectable, and a personal agent's own GitHub account too.
    drive = client.post(f"/api/connections/egress/agents/shared_dev/drive/{action}", headers=bob, json={})
    assert drive.status_code == 200, drive.text
    personal = client.post(f"/api/connections/egress/agents/personal/gh/{action}", headers=bob, json={})
    assert personal.status_code == 200, personal.text


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_are_refused_on_the_static_runner_until_the_service_opts_in(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """A personal agent shares its sandbox on the static runner, so its requester-scoped account is refused there."""
    portal = oauth_egress_portal
    _use_worker_backend(portal, "static_runner")
    url = f"/api/connections/egress/agents/personal/gh/{action}"
    headers = portal["headers"]["alice"]

    refused = portal["client"].post(url, headers=headers, json={})
    assert refused.status_code == 409, refused.text
    assert isinstance(refused.json()["detail"], str)

    _opt_in_to_shared_workers(portal)
    allowed = portal["client"].post(url, headers=headers, json={})
    assert allowed.status_code == 200, allowed.text


@pytest.mark.parametrize("failure", ["unsupported backend", "other"])
def test_an_unreadable_worker_backend_degrades_one_service_without_failing_the_pages(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The shared-sandbox check can fail on its own, and that spares the listing and the catalog but that service."""
    portal = oauth_egress_portal
    if failure == "unsupported backend":
        _use_worker_backend(portal, "no-such-backend")
    else:
        real = egress_credentials.shared_worker_oauth

        def broken(provider: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if provider.requester_scoped_credentials:
                msg = "internal-backend-detail"
                raise RuntimeError(msg)
            return real(provider, *args, **kwargs)

        monkeypatch.setattr(egress_credentials, "shared_worker_oauth", broken)

    listing = portal["client"].get("/api/connections/egress", headers=portal["headers"]["alice"])
    catalog = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])

    assert listing.status_code == 200, listing.text
    assert catalog.status_code == 200, catalog.text
    assert "internal-backend-detail" not in listing.text + catalog.text
    gh = _egress_row(portal, "alice", "personal", "gh")["oauth"]
    assert (gh["connected"], gh["can_connect"], gh["unavailable_reason"]) == (False, False, None)
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["can_connect"] is True
    assert _egress_row(portal, "alice", "personal", "github")["oauth"] is None


def _scope_target(portal: dict[str, Any], agent: str = "personal", requester: str = "@alice:example.org") -> Any:  # noqa: ANN401
    config = Config.model_validate(portal["payload"], context={"runtime_paths": portal["paths"]})
    return build_connection_agent_target(config, portal["paths"], requester, agent)


def _store_raw_entries(
    portal: dict[str, Any],
    entries: dict[str, Any],
    agent: str = "personal",
    requester: str = "@alice:example.org",
) -> None:
    """Write entries into a scope's services document as stored, without the save route's validation."""
    secrets.save_egress_document(
        get_runtime_credentials_manager(portal["paths"]),
        _scope_target(portal, agent, requester),
        "egress__services",
        {"services": entries},
    )


def _listed_agent(portal: dict[str, Any], user: str, agent: str) -> dict[str, Any]:
    response = portal["client"].get("/api/connections/egress", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["agents"] if item["agent_name"] == agent)


def _stored_key(
    portal: dict[str, Any],
    name: str,
    agent: str = "personal",
    requester: str = "@alice:example.org",
) -> Any:  # noqa: ANN401
    return secrets.load_secret(
        get_runtime_credentials_manager(portal["paths"]),
        _scope_target(portal, agent, requester),
        name,
    )


def test_active_services_are_not_listed_as_inactive(egress_portal: dict[str, Any]) -> None:
    """Every agent object carries inactive_services, empty while all of the scope's entries are in use."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    for agent in ("personal", "shared_dev"):
        assert _listed_agent(portal, "alice", agent)["inactive_services"] == []


def test_a_shadowed_entry_is_listed_with_its_reason_and_deleted_keeping_the_key(egress_portal: dict[str, Any]) -> None:
    """An entry an administrator's service now shadows is shown as inactive; deleting it leaves the config key."""
    portal = egress_portal
    client, alice = portal["client"], portal["headers"]["alice"]
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert (
        client.put("/api/connections/egress/agents/personal/notes", json={"secret": "k"}, headers=alice).status_code
        == 204
    )
    portal["payload"]["egress_broker"]["services"]["notes"] = {
        "description": "Administrator notes",
        "rules": [{"host": "notes.admin.example.com", "auth": {"type": "bearer"}}],
    }
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)

    agent = _listed_agent(portal, "alice", "personal")
    assert agent["inactive_services"] == [{"name": "notes", "reason": "shadowed"}]
    assert [(s["name"], s["source"]) for s in agent["services"] if s["name"] == "notes"] == [("notes", "config")]

    deleted = _delete_service(portal, "alice", "personal", "notes")
    assert deleted.status_code == 204, deleted.text
    assert _listed_agent(portal, "alice", "personal")["inactive_services"] == []
    assert _stored_key(portal, "notes") == "k"


def test_an_invalid_entry_is_listed_with_its_reason_and_deleted_with_its_key(egress_portal: dict[str, Any]) -> None:
    """An entry that no longer validates is shown as inactive, never as a service, and deleting it removes its key."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    _store_raw_entries(
        portal,
        {"notes": _NOTES, "stale": {"rules": []}, "odd": "not a service"},
    )
    secrets.save_secret(get_runtime_credentials_manager(portal["paths"]), _scope_target(portal), "stale", "old-key")

    agent = _listed_agent(portal, "alice", "personal")
    assert agent["inactive_services"] == [
        {"name": "odd", "reason": "invalid"},
        {"name": "stale", "reason": "invalid"},
    ]
    assert [s["name"] for s in agent["services"]] == ["github", "openai", "notes"]
    assert _get_service(portal, "alice", "personal", "stale").status_code == 404

    deleted = _delete_service(portal, "alice", "personal", "stale")
    assert deleted.status_code == 204, deleted.text
    assert _listed_agent(portal, "alice", "personal")["inactive_services"] == [{"name": "odd", "reason": "invalid"}]
    assert _stored_key(portal, "stale") is None
    assert _delete_service(portal, "alice", "personal", "stale").status_code == 404
    assert _delete_service(portal, "alice", "personal", "odd").status_code == 204
    assert _get_service(portal, "alice", "personal", "notes").json() == _NOTES
    assert _listed_agent(portal, "alice", "personal")["inactive_services"] == []


def test_inactive_entries_never_reach_another_requester(egress_portal: dict[str, Any]) -> None:
    """Inactive entries live in one requester's scope, so another requester's listing never names them."""
    portal = egress_portal
    _store_raw_entries(portal, {"stale": {"rules": []}})

    assert _listed_agent(portal, "alice", "personal")["inactive_services"] == [{"name": "stale", "reason": "invalid"}]
    assert _listed_agent(portal, "bob", "personal")["inactive_services"] == []
    assert _delete_service(portal, "bob", "personal", "stale").status_code == 404
    assert _listed_agent(portal, "alice", "personal")["inactive_services"] == [{"name": "stale", "reason": "invalid"}]


def test_inactive_entries_of_a_shared_agent_are_listed_for_its_users_and_deleted_by_managers(
    egress_portal: dict[str, Any],
) -> None:
    """On a shared agent everyone sees the same entries, but only a manager may delete them."""
    portal = egress_portal
    _store_raw_entries(portal, {"stale": {"rules": []}}, agent="shared_dev", requester="@bob:example.org")

    for user in ("alice", "bob"):
        assert _listed_agent(portal, user, "shared_dev")["inactive_services"] == [
            {"name": "stale", "reason": "invalid"},
        ]
    assert _delete_service(portal, "alice", "shared_dev", "stale").status_code == 403
    assert _delete_service(portal, "bob", "shared_dev", "stale").status_code == 204
    assert _listed_agent(portal, "alice", "shared_dev")["inactive_services"] == []


def test_inactive_entries_count_toward_the_limit_until_deleted(egress_portal: dict[str, Any]) -> None:
    """A scope full of ignored entries cannot take another service, so the user must be able to delete them."""
    portal = egress_portal
    _store_raw_entries(portal, {f"stale-{index}": {"rules": []} for index in range(50)})

    refused = _put_service(portal, "alice", "personal", "notes")
    assert refused.status_code == 422, refused.text
    assert "at most 50" in refused.json()["detail"]

    assert _delete_service(portal, "alice", "personal", "stale-0").status_code == 204
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert len(_listed_agent(portal, "alice", "personal")["inactive_services"]) == 49


@pytest.fixture
def unscoped_portal(egress_portal: dict[str, Any]) -> dict[str, Any]:
    """Add an agent without a worker scope, which everyone using it shares like a shared agent."""
    portal = egress_portal
    portal["payload"]["agents"]["unscoped_dev"] = {
        "display_name": "Unscoped Dev",
        "role": "Team agent without a worker scope",
        "tools": ["shell"],
        "credential_managers": ["@bob:example.org"],
        "access": {"users": ["@alice:example.org", "@bob:example.org"]},
    }
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    return portal


def test_a_non_manager_cannot_write_services_or_keys_of_an_unscoped_agent(unscoped_portal: dict[str, Any]) -> None:
    """An unscoped agent is shared by every user, so only its credential managers change services and keys."""
    portal = unscoped_portal
    client, alice = portal["client"], portal["headers"]["alice"]
    key_url = "/api/connections/egress/agents/unscoped_dev/github"

    agent = _listed_agent(portal, "alice", "unscoped_dev")
    assert (agent["shared"], agent["can_manage"]) == (True, False)
    for response in (
        _put_service(portal, "alice", "unscoped_dev", "notes"),
        _delete_service(portal, "alice", "unscoped_dev", "notes"),
        client.put(key_url, json={"secret": "x"}, headers=alice),
        client.delete(key_url, headers=alice),
    ):
        assert response.status_code == 403, response.text
        assert "Credential management is required" in response.text
    assert _get_service(portal, "alice", "unscoped_dev", "notes").status_code == 404

    assert _put_service(portal, "bob", "unscoped_dev", "notes").status_code == 204
    assert client.put(key_url, json={"secret": "x"}, headers=portal["headers"]["bob"]).status_code == 204
    assert _get_service(portal, "alice", "unscoped_dev", "notes").json() == _NOTES
    assert _delete_service(portal, "alice", "unscoped_dev", "notes").status_code == 403


def test_a_service_body_that_is_not_an_object_is_a_string_422_with_private_headers(
    egress_portal: dict[str, Any],
) -> None:
    """Like every other refusal of the save, a body of the wrong shape answers with a readable string detail."""
    portal = egress_portal
    url = _service_url("personal", "notes")
    for body in (["rules"], "rules", 3, None):
        # `json=None` sends the JSON text `null`.
        response = portal["client"].put(url, json=body, headers=portal["headers"]["alice"])
        assert response.status_code == 422, (body, response.text)
        assert response.json()["detail"] == "A service must be a JSON object"
        assert response.headers["cache-control"] == "private, no-store"
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404


def test_service_presets_and_log_responses_are_private_and_never_cached(
    egress_portal: dict[str, Any],
    audit_log: AuditLog,  # noqa: ARG001
) -> None:
    """Every service, preset, and log response, errors included, carries the same headers as the key routes."""
    portal = egress_portal
    client, alice = portal["client"], portal["headers"]["alice"]
    url = _service_url("personal", "notes")
    responses = {
        "put": _put_service(portal, "alice", "personal", "notes"),
        "put invalid": _put_service(portal, "alice", "personal", "notes", {"description": "no rules"}),
        "put config name": _put_service(portal, "alice", "personal", "github"),
        "put too big": _put_service(portal, "alice", "personal", "notes", {**_NOTES, "description": "x" * 17_000}),
        "put not allowed": _put_service(portal, "alice", "shared_dev", "notes"),
        "put unknown agent": _put_service(portal, "alice", "unknown", "notes"),
        "put cross origin": client.put(url, json=_NOTES, headers={**alice, "Origin": "https://evil.example.com"}),
        "put query": client.put(f"{url}?x=1", json=_NOTES, headers=alice),
        "get": _get_service(portal, "alice", "personal", "notes"),
        "get missing": _get_service(portal, "alice", "personal", "missing"),
        "delete": _delete_service(portal, "alice", "personal", "notes"),
        "delete missing": _delete_service(portal, "alice", "personal", "notes"),
        "delete config": _delete_service(portal, "alice", "personal", "github"),
        "presets": client.get("/api/connections/egress/presets", headers=alice),
        "presets query": client.get("/api/connections/egress/presets?x=1", headers=alice),
        "logs": client.get("/api/connections/egress/logs", headers=alice),
        "logs query": client.get("/api/connections/egress/logs?x=1", headers=alice),
    }

    statuses = {label: response.status_code for label, response in responses.items()}
    assert statuses == {
        "put": 204,
        "put invalid": 422,
        "put config name": 409,
        "put too big": 413,
        "put not allowed": 403,
        "put unknown agent": 404,
        "put cross origin": 403,
        "put query": 400,
        "get": 200,
        "get missing": 404,
        "delete": 204,
        "delete missing": 404,
        "delete config": 409,
        "presets": 200,
        "presets query": 400,
        "logs": 200,
        "logs query": 400,
    }
    for label, response in responses.items():
        assert response.headers["cache-control"] == "private, no-store", label
        assert response.headers["referrer-policy"] == "no-referrer", label


def test_user_scope_services_and_keys_are_shared_by_the_requesters_user_scope_agents(
    egress_portal: dict[str, Any],
) -> None:
    """A `user` scope is one store per requester, so a service saved on one such agent is the service of them all."""
    portal = egress_portal
    for name, scope in (("user_a", "user"), ("user_b", "user"), ("agent_scoped", "user_agent")):
        portal["payload"]["agents"][name] = {
            "display_name": name,
            "role": "Personal assistant",
            "tools": ["shell"],
            "private": {"per": scope},
            "access": {"users": ["@alice:example.org", "@bob:example.org"]},
        }
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)
    client, alice = portal["client"], portal["headers"]["alice"]

    assert _put_service(portal, "alice", "user_a", "notes").status_code == 204
    assert (
        client.put("/api/connections/egress/agents/user_a/notes", json={"secret": "k"}, headers=alice).status_code
        == 204
    )

    assert _get_service(portal, "alice", "user_b", "notes").json() == _NOTES
    row = next(s for s in _listed_services(portal, "alice", "user_b") if s["name"] == "notes")
    assert (row["source"], row["key_configured"]) == ("user", True)
    # Another requester, and an agent with its own scope per agent, have their own stores.
    assert _get_service(portal, "bob", "user_b", "notes").status_code == 404
    assert _get_service(portal, "alice", "agent_scoped", "notes").status_code == 404
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404

    assert _delete_service(portal, "alice", "user_b", "notes").status_code == 204
    assert _get_service(portal, "alice", "user_a", "notes").status_code == 404
