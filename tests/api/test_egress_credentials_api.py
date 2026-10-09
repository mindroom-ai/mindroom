"""Personal egress credentials API authorization and write-only guarantee."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jwt
import pytest
from fastapi.testclient import TestClient

from mindroom.api import main
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import _publish_config, _runtime_paths, _use_runtime_auth_settings

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
