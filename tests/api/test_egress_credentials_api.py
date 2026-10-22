"""Personal egress credentials API authorization and write-only guarantee."""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Never
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import yaml
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs

from mindroom.api import config_lifecycle, connections, egress_credentials, egress_status, main, oauth
from mindroom.api.connection_agents import build_connection_agent_target
from mindroom.config.egress_broker import RESERVED_PLACEHOLDER_NAMES, RESERVED_PLACEHOLDER_PREFIXES, EgressService
from mindroom.config.main import Config
from mindroom.credentials.manager import CredentialsManager, get_runtime_credentials_manager
from mindroom.egress_broker import oauth_source, secrets
from mindroom.egress_broker.audit import AuditLog, AuditRecord
from mindroom.egress_broker.env import egress_agent_scope
from mindroom.egress_broker.oauth_source import resolve_oauth_token
from mindroom.egress_broker.presets import EGRESS_PRESETS
from mindroom.egress_broker.rules import EgressRules, route_request
from mindroom.egress_broker.tokens import WorkerClaims
from mindroom.egress_broker.user_services import (
    MAX_RULES_PER_SERVICE,
    MAX_SERVICE_BYTES,
    PLACEHOLDER_NAME,
    PLACEHOLDER_NAME_WORDS,
    PLACEHOLDER_REFUSED_PREFIX,
    PLACEHOLDER_REFUSED_WORDS,
    PLACEHOLDER_VALUE,
    PLACEHOLDER_VALUE_MAX_LENGTH,
    effective_config,
)
from mindroom.oauth import credential_store as oauth_credential_store
from mindroom.oauth import github as github_oauth
from mindroom.oauth import registry as oauth_registry
from mindroom.oauth.github import LOGIN_LOOKUP_TIMEOUT_SECONDS, github_oauth_provider
from mindroom.oauth.providers import OAuthProvider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.api.conftest import (
    trusted_upstream_env,
    trusted_upstream_jwks,
    trusted_upstream_jwt,
    trusted_upstream_jwt_key,
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
    from collections.abc import Callable, Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


# --- Fixtures ---


def _trust_signed_users(monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Accept the test key's signed identities and resolve every host to a public address; return the key."""
    key = trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: trusted_upstream_jwks(key))
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    return key


def _signed_headers(key: Any, name: str) -> dict[str, str]:  # noqa: ANN401
    """Return the same-origin headers of the signed user `name`."""
    return {
        "X-Trusted-Jwt": trusted_upstream_jwt(
            key,
            user_id=name,
            email=f"{name}@example.org",
            matrix_user_id=f"@{name}:example.org",
        ),
        "Origin": "https://portal.example.org",
    }


@pytest.fixture
def egress_portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> dict[str, Any]:  # noqa: ARG001
    """Serve the real API with signed users and agents that use Agent Keys (shell or python and `egress_credentials`)."""
    key = _trust_signed_users(monkeypatch)
    env = {
        **trusted_upstream_env(),
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MINDROOM_PUBLIC_URL": "https://portal.example.org",
        # A dedicated backend, where a personal agent's worker belongs to one requester.
        "MINDROOM_WORKER_BACKEND": "docker",
        # The broker runs exactly when its port is set; the API never starts it, so any port enables the routes.
        "MINDROOM_EGRESS_BROKER_PORT": "8768",
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
            "personal": _agent_config(
                "Personal Mind",
                "Personal assistant",
                private="user_agent",
                users=("@alice:example.org", "@bob:example.org"),
                tools=("shell", "python", "egress_credentials"),
            ),
            "shared_dev": _agent_config(
                "Shared Dev",
                "Team agent",
                users=("@alice:example.org", "@bob:example.org"),
                tools=("shell", "python", "egress_credentials"),
                worker_scope="shared",
                credential_managers=["@bob:example.org", "@carol:example.org"],
            ),
            "no_shell": _agent_config("Calculator", "Math only", private="user_agent", tools=("calculator",)),
            "other_private": _agent_config(
                "Other Private",
                "Another private agent",
                private="user",
                users=("@mallory:example.org",),
            ),
        },
    }
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    headers = {name: _signed_headers(key, name) for name in ("alice", "bob", "carol", "admin")}
    return {
        "client": TestClient(main.app, base_url="https://portal.example.org"),
        "headers": headers,
        "paths": paths,
        "payload": payload,
    }


def _oauth_services() -> dict[str, Any]:
    """Return services on an agent-scoped (Drive), a requester-scoped (GitHub), and an unknown OAuth provider."""
    rules = [{"host": "www.googleapis.com", "auth": {"type": "bearer"}}]
    return {
        "drive": {"description": "Drive", "oauth_provider": "google_drive", "rules": rules},
        "gh": {"display_name": "GH", "oauth_provider": "github", "rules": rules},
        "ghost": {"description": "Unknown provider", "oauth_provider": "no_such_provider", "rules": rules},
    }


def _use_fake_oauth_providers(monkeypatch: pytest.MonkeyPatch, paths: RuntimePaths) -> dict[str, OAuthProvider]:
    """Register fake agent-scoped Drive and requester-scoped GitHub providers with configured clients; return them."""
    drive = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    github = _fake_provider(
        provider_id="github",
        credential_service="github_oauth",
        client_config_services=("github_oauth_client",),
        requester_scoped_credentials=True,
    )
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (drive, github))
    manager = get_runtime_credentials_manager(paths)
    for client_service in ("test_drive_oauth_client", "github_oauth_client"):
        manager.save_credentials(
            client_service,
            {"client_id": "test-client", "client_secret": "test-secret", "_source": "ui"},
        )
    return {"google_drive": drive, "github": github}


@pytest.fixture
def oauth_egress_portal(egress_portal: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Extend the egress portal with services backed by an agent-scoped and a requester-scoped OAuth provider."""
    paths = egress_portal["paths"]
    providers = _use_fake_oauth_providers(monkeypatch, paths)
    payload = egress_portal["payload"]
    for agent in ("personal", "shared_dev"):
        # The Connections portal lists a provider only for agents that have a tool using it.
        payload["agents"][agent]["tools"].append({"name": "google_drive", "defer": True})
    payload["egress_broker"]["services"].update(_oauth_services())
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    return {**egress_portal, "providers": providers}


@pytest.fixture
def unscoped_portal(egress_portal: dict[str, Any]) -> dict[str, Any]:
    """Add an agent without a worker scope, which everyone using it shares like a shared agent."""
    portal = egress_portal
    portal["payload"]["agents"]["unscoped_dev"] = _agent_config(
        "Unscoped Dev",
        "Team agent without a worker scope",
        users=("@alice:example.org", "@bob:example.org"),
        credential_managers=["@bob:example.org"],
    )
    _republish(portal)
    return portal


@pytest.fixture
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuditLog]:
    """Serve the personal log from a real audit log, as if the broker were running."""
    log = AuditLog(tmp_path / "audit.sqlite3")
    monkeypatch.setattr(egress_credentials, "active_audit_log", lambda: log)
    yield log
    log.close()


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


# --- Helpers ---


def _republish(portal: dict[str, Any]) -> None:
    """Publish the portal's edited config payload and apply it, as a dashboard save and reload do."""
    _publish_config(main.app, portal["paths"], portal["payload"])
    _use_runtime_auth_settings(main.app)


def _restart_with_env(portal: dict[str, Any], **env: str) -> None:
    """Restart the API with `env` added to the portal's process environment, then republish its config."""
    portal["paths"] = replace(portal["paths"], process_env={**portal["paths"].process_env, **env})
    main.initialize_api_app(main.app, portal["paths"])
    _republish(portal)


def _agent_config(
    display_name: str,
    role: str,
    *,
    private: str | None = None,
    users: tuple[str, ...] = ("@alice:example.org",),
    tools: tuple[str, ...] = ("shell", "egress_credentials"),
    **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """Return a config entry for an agent that `users` may use, private per `private` when given.

    By default the agent uses Agent Keys: it has a shell and the `egress_credentials` tool, which opts it in.
    """
    return {
        "display_name": display_name,
        "role": role,
        "tools": list(tools),
        **({"private": {"per": private}} if private else {}),
        **extra,
        "access": {"users": list(users)},
    }


def _key_url(agent: str, service: str) -> str:
    return f"/api/connections/egress/agents/{agent}/{service}"


def _put_key(portal: dict[str, Any], user: str, agent: str, service: str, secret: str = "token") -> Any:  # noqa: ANN401, S107
    return portal["client"].put(_key_url(agent, service), json={"secret": secret}, headers=portal["headers"][user])


def _delete_key(portal: dict[str, Any], user: str, agent: str, service: str) -> Any:  # noqa: ANN401
    return portal["client"].delete(_key_url(agent, service), headers=portal["headers"][user])


def _account_action(portal: dict[str, Any], user: str, agent: str, service: str, action: str) -> Any:  # noqa: ANN401
    """POST the `connect` or `disconnect` action of a service's OAuth account, as `user`."""
    return portal["client"].post(f"{_key_url(agent, service)}/{action}", headers=portal["headers"][user], json={})


def _listing(portal: dict[str, Any], user: str) -> Any:  # noqa: ANN401
    """Return the personal egress listing response for `user`, after checking it succeeded."""
    response = portal["client"].get("/api/connections/egress", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    return response


def _listed_agents(portal: dict[str, Any], user: str) -> dict[str, dict[str, Any]]:
    return {agent["agent_name"]: agent for agent in _listing(portal, user).json()["agents"]}


def _agent_rows(response: Any) -> list[str]:  # noqa: ANN401
    assert response.status_code == 200, response.text
    return [agent["agent_name"] for agent in response.json()["agents"]]


def _egress_row(portal: dict[str, Any], user: str, agent: str, service: str) -> dict[str, Any]:
    return next(item for item in _listed_agents(portal, user)[agent]["services"] if item["name"] == service)


def _state(portal: dict[str, Any], user: str, agent: str) -> Any:  # noqa: ANN401
    """Return the response of the Connections portal's state route for one agent."""
    return portal["client"].get(f"/api/connections/agents/{agent}/state", headers=portal["headers"][user])


def _state_egress(portal: dict[str, Any], user: str, agent: str) -> list[dict[str, Any]] | None:
    """Return the egress rows the Connections portal's state route lists for one agent, after checking it succeeded."""
    response = _state(portal, user, agent)
    assert response.status_code == 200, response.text
    return response.json()["egress_services"]


def _state_services(portal: dict[str, Any], user: str, agent: str) -> list[dict[str, Any]]:
    """Return the egress rows of an agent the user may use egress for, as the portal's state route lists them."""
    services = _state_egress(portal, user, agent)
    assert services is not None
    return services


def _finish_oauth(
    client: TestClient,
    connect: Any,  # noqa: ANN401
    provider: str,
    headers: dict[str, str] | None = None,
) -> None:
    """Check `connect` answered with an authorize URL, then complete it through the shared callback."""
    assert connect.status_code == 200, connect.text
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        f"/api/oauth/{provider}/callback",
        params={"code": "test-code", "state": state},
        headers=headers,
        follow_redirects=False,
    )
    assert callback.status_code in {302, 303, 307}, callback.text


def _connect_account(portal: dict[str, Any], user: str, agent: str, service: str, provider: str) -> None:
    """Run the whole OAuth flow through the egress connect route and the shared callback."""
    connect = _account_action(portal, user, agent, service, "connect")
    _finish_oauth(portal["client"], connect, provider, portal["headers"][user])


def _reuse_url(agent: str, service: str) -> str:
    return f"{_key_url(agent, service)}/oauth-reuse"


def _allow_reuse(portal: dict[str, Any], user: str, agent: str, service: str) -> Any:  # noqa: ANN401
    """Let Agent Keys use the service's connected account, as `user`, confirming the approval bypass."""
    return portal["client"].put(
        _reuse_url(agent, service),
        headers=portal["headers"][user],
        json={"acknowledge_approval_bypass": True},
    )


def _stop_reuse(portal: dict[str, Any], user: str, agent: str, service: str) -> Any:  # noqa: ANN401
    return portal["client"].delete(_reuse_url(agent, service), headers=portal["headers"][user])


def _connect_and_allow(portal: dict[str, Any], user: str, agent: str, service: str, provider: str) -> None:
    """Connect the service's account and let Agent Keys use it."""
    _connect_account(portal, user, agent, service, provider)
    allowed = _allow_reuse(portal, user, agent, service)
    assert allowed.status_code == 204, allowed.text


def _oauth_block(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return the `oauth` block of a Drive service nobody has connected, with `changes` applied."""
    return {
        "provider": "google_drive",
        "display_name": "Test Drive",
        "connected": False,
        "account_label": None,
        "can_connect": True,
        "reset_required": False,
        "service_account": False,
        "unavailable_reason": None,
        "shared_worker_opt_in": False,
        "reuse_enabled": False,
        **changes,
    }


def _opt_in_to_shared_workers(portal: dict[str, Any]) -> None:
    """Let the requester-scoped `gh` service use connected accounts on shared and unscoped workers."""
    portal["payload"]["egress_broker"]["services"]["gh"]["oauth_on_shared_workers"] = True
    _republish(portal)


_NOTES = {
    "description": "Team notes",
    "rules": [{"host": "api.notes.example.com", "auth": {"type": "bearer"}}],
}


# A personal service that signs in with a connected GitHub account, on one of the GitHub preset's hosts.
_MY_GITHUB = {
    "description": "My repositories",
    "rules": [{"host": "api.github.com", "path_prefix": "/repos/alice/", "auth": {"type": "bearer"}}],
    "oauth_provider": "github",
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
    return _listed_agents(portal, user)[agent]["services"]


def _listed_names(portal: dict[str, Any], user: str, agent: str) -> list[str]:
    return [service["name"] for service in _listed_services(portal, user, agent)]


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
    scope: str | None = None,
) -> None:
    log.record(
        AuditRecord(
            at=_LOG_START + timedelta(seconds=seconds),
            kind=kind,
            scope=scope or ("shared" if agent_name == "shared_dev" else "user_agent"),
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


# --- Agents and keys ---


@pytest.mark.parametrize("with_connections_agent", [False, True])
def test_works_with_and_without_connections_agent(
    egress_portal: dict[str, Any],
    *,
    with_connections_agent: bool,
) -> None:
    """The egress routes work with or without MINDROOM_CONNECTIONS_AGENT set; a blank value is unset."""
    _restart_with_env(egress_portal, MINDROOM_CONNECTIONS_AGENT="personal" if with_connections_agent else "")

    listing = _listing(egress_portal, "alice")

    assert _agent_rows(listing) == ["personal", "shared_dev"]
    # Non-admin user should be allowed (personal user gate test)
    assert "Administrator access required" not in listing.text


def test_every_route_is_a_404_while_the_broker_is_off(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a broker port every personal route is a 404 before it authenticates the caller or reads any store."""
    _restart_with_env(egress_portal, MINDROOM_EGRESS_BROKER_PORT="")
    stores_opened: list[object] = []
    monkeypatch.setattr(egress_credentials, "get_runtime_credentials_manager", stores_opened.append)
    routes = sorted(
        (method, route.path)
        for route in main.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/connections/egress")
        for method in route.methods
    )
    assert {("GET", "/api/connections/egress"), ("GET", "/api/connections/egress/presets")} <= set(routes)

    for method, path in routes:
        url = path.format(agent_name="personal", service="github", name="github")
        for headers in (egress_portal["headers"]["alice"], {}):
            response = egress_portal["client"].request(method, url, headers=headers)
            assert (response.status_code, response.json()) == (404, {"detail": "Agent Keys are not enabled"}), path
            assert "no-store" in response.headers["cache-control"]
    assert stores_opened == []


def test_lists_only_usable_agents_that_opted_into_agent_keys(egress_portal: dict[str, Any]) -> None:
    """Only agents the user may use that have shell or python and the `egress_credentials` tool appear."""
    egress_portal["payload"]["agents"]["shell_only"] = _agent_config("Shell Only", "No opt-in", tools=("shell",))
    _republish(egress_portal)
    response = _listing(egress_portal, "alice")
    agent_names = {a["agent_name"] for a in response.json()["agents"]}
    assert agent_names == {"personal", "shared_dev"}
    assert all(len(a["services"]) == 2 for a in response.json()["agents"])
    assert all(s["name"] in ("github", "openai") for a in response.json()["agents"] for s in a["services"])


def test_listing_reports_scope_scope_agents_and_managers(egress_portal: dict[str, Any]) -> None:
    """Every listed agent says how its keys are shared: its scope, the agents of the same store, and who manages it."""
    portal = egress_portal
    agents = portal["payload"]["agents"]
    for name, display_name in (("notes", "Notes"), ("journal", "Journal")):
        agents[name] = _agent_config(display_name, "Private helper", private="user")
    agents["team"] = _agent_config(
        "Team",
        "Team agent without a worker scope",
        users=("@alice:example.org", "@bob:example.org"),
        credential_managers=["@alice:example.org"],
    )
    _republish(portal)

    rows = _listed_agents(portal, "alice")
    assert {name: (row["scope"], row["scope_agents"], row["managers"]) for name, row in rows.items()} == {
        "personal": ("user_agent", [], []),
        "notes": ("user", ["Notes", "Journal"], []),
        "journal": ("user", ["Notes", "Journal"], []),
        "shared_dev": ("shared", [], ["@bob:example.org", "@carol:example.org"]),
        "team": ("unscoped", [], ["@alice:example.org"]),
    }
    assert rows["team"]["shared"] is True
    bob_rows = _listed_agents(portal, "bob")
    # Another user's personal agents are neither listed nor named, and a manager list is the same for every user.
    assert "notes" not in bob_rows
    assert bob_rows["team"]["managers"] == ["@alice:example.org"]
    assert bob_rows["personal"]["scope_agents"] == []


def test_scope_agents_name_only_the_requesters_agents_that_can_use_the_store(egress_portal: dict[str, Any]) -> None:
    """An agent without a shell or the `egress_credentials` tool never uses brokered keys, nor one not the requester's."""
    portal = egress_portal
    agents = portal["payload"]["agents"]
    agents["notes"] = _agent_config("Notes", "Private helper", private="user")
    agents["calc"] = _agent_config(
        "Calc",
        "Private helper without a shell",
        private="user",
        tools=("calculator", "egress_credentials"),
    )
    agents["shell"] = _agent_config("Shell", "Private helper without the tool", private="user", tools=("shell",))
    agents["bobs"] = _agent_config("Bobs", "Private helper of Bob", private="user", users=("@bob:example.org",))
    _republish(portal)

    assert _listed_agents(portal, "alice")["notes"]["scope_agents"] == ["Notes"]


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
    _republish(portal)

    alice = _listed_agents(portal, "alice")
    assert {agent["agent_name"]: agent["services"] for agent in alice.values()} == {"personal": [], "shared_dev": []}
    assert (alice["personal"]["shared"], alice["personal"]["can_manage"]) == (False, True)
    assert (alice["shared_dev"]["shared"], alice["shared_dev"]["can_manage"]) == (True, False)
    assert _listed_agents(portal, "bob")["shared_dev"]["can_manage"] is True


def test_agents_keep_config_order(egress_portal: dict[str, Any]) -> None:
    """Both pages list agents in the order the config declares them, whatever keys are set."""
    portal = egress_portal
    agents = portal["payload"]["agents"]
    portal["payload"]["agents"] = {"shared_dev": agents["shared_dev"], **agents}
    _republish(portal)
    client, alice = portal["client"], portal["headers"]["alice"]

    listed = _agent_rows(_listing(portal, "alice"))
    catalog = _agent_rows(client.get("/api/connections", headers=alice))
    assert listed == ["shared_dev", "personal"]
    # The catalog also lists Alice's private calculator agent, which has no keys to set.
    assert catalog == ["shared_dev", "personal", "no_shell"]

    # Setting a key on the agent declared second, then on the first, never reorders either page. Alice sets her own
    # personal key, so the key lands in the scope the pages read; Bob manages the shared agent.
    for agent, setter in (("personal", "alice"), ("shared_dev", "bob")):
        put = _put_key(portal, setter, agent, "github", "k")
        assert put.status_code == 204, put.text
        assert _agent_rows(_listing(portal, "alice")) == listed
        assert _agent_rows(client.get("/api/connections", headers=alice)) == catalog
    configured = {
        agent["agent_name"]: next(service for service in agent["services"] if service["name"] == "github")["configured"]
        for agent in _listing(portal, "alice").json()["agents"]
    }
    assert configured == {"shared_dev": True, "personal": True}


def test_dashboard_config_save_sorts_services_and_agents_and_ties_follow_service_names(
    egress_portal: dict[str, Any],
) -> None:
    """A dashboard save writes agents and egress services sorted by name, and a rule tie goes to the first name."""
    portal = egress_portal
    client, paths = portal["client"], portal["paths"]
    admin, alice = portal["headers"]["admin"], portal["headers"]["alice"]
    payload = deepcopy(portal["payload"])
    agents = payload["agents"]
    payload["agents"] = {"shared_dev": agents["shared_dev"], **agents}
    clashing_rule = {"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]}
    # Declared against the alphabet, and both match the same request.
    payload["egress_broker"]["services"] = {
        "zeta": clashing_rule,
        "alpha": clashing_rule,
        **payload["egress_broker"]["services"],
    }

    saved = client.put("/api/config/save", json=payload, headers=admin)
    assert saved.status_code == 200, saved.text
    # Read the file back, as a restart or reload does, so the order on disk is what the pages and rules see.
    assert config_lifecycle.load_config_into_app(paths, main.app)

    assert _agent_rows(_listing(portal, "alice")) == ["personal", "shared_dev"]
    assert _agent_rows(client.get("/api/connections", headers=alice)) == ["no_shell", "personal", "shared_dev"]
    on_disk = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert list(on_disk["agents"]) == sorted(payload["agents"])
    assert list(on_disk["egress_broker"]["services"]) == sorted(payload["egress_broker"]["services"])
    route = route_request(
        EgressRules.from_config(Config.validate_with_runtime(on_disk, paths).egress_broker),
        "api.example.com",
        443,
        "/anything",
    )
    assert route.match is not None
    assert route.match.service == "alpha"


def test_requester_sets_own_user_agent_secret(egress_portal: dict[str, Any]) -> None:
    """A requester sets their own secret on a private agent, and another user sees it unconfigured."""
    portal = egress_portal

    # Alice sets her secret
    response = _put_key(portal, "alice", "personal", "github", "alice-token")
    assert response.status_code == 204, response.text

    # Alice sees it configured
    github_service = _egress_row(portal, "alice", "personal", "github")
    assert github_service["configured"] is True
    assert github_service["key_updated_at"] is not None
    assert "alice-token" not in _listing(portal, "alice").text

    # Bob sees it unconfigured (his own scope)
    github_service_bob = _egress_row(portal, "bob", "personal", "github")
    assert github_service_bob["configured"] is False
    assert github_service_bob["key_updated_at"] is None


def test_shared_agent_requires_credential_manager(egress_portal: dict[str, Any]) -> None:
    """A plain user cannot set shared agent secrets; credential managers can."""
    portal = egress_portal

    # Alice (not a manager) cannot set
    response = _put_key(portal, "alice", "shared_dev", "github", "alice-token")
    assert response.status_code == 403, response.text
    assert "Credential management is required" in response.text

    # Bob (manager) can set
    response = _put_key(portal, "bob", "shared_dev", "github", "team-token")
    assert response.status_code == 204, response.text

    # Both see it configured
    for user in ("alice", "bob"):
        assert _egress_row(portal, user, "shared_dev", "github")["configured"] is True


def test_cross_origin_mutation_rejected(egress_portal: dict[str, Any]) -> None:
    """Mutations require same-origin HTTPS requests."""
    client = egress_portal["client"]
    alice_headers = {**egress_portal["headers"]["alice"], "Origin": "https://evil.example.com"}

    response = client.put(_key_url("personal", "github"), json={"secret": "token"}, headers=alice_headers)
    assert response.status_code == 403, response.text

    # GET is allowed
    response = client.get("/api/connections/egress", headers=alice_headers)
    assert response.status_code == 200, response.text


def test_response_never_contains_secret(egress_portal: dict[str, Any]) -> None:
    """Responses never include secret values, only status."""
    portal = egress_portal

    put_response = _put_key(portal, "alice", "personal", "github", "very-secret-token")
    assert put_response.status_code == 204, put_response.text

    listing = _listing(portal, "alice")
    assert "very-secret-token" not in listing.text
    # "secret" key exists in config but secret value never appears
    assert '"secret"' not in listing.text


@pytest.mark.parametrize(
    ("write", "agent", "service"),
    [
        pytest.param(_put_key, "unknown", "github", id="put-unknown-agent"),
        pytest.param(_put_key, "personal", "unknown", id="put-unknown-service"),
        pytest.param(_delete_key, "personal", "unknown", id="delete-unknown-service"),
        pytest.param(_put_key, "no_shell", "github", id="without-shell-or-python"),
        pytest.param(_put_key, "other_private", "github", id="no-access"),
    ],
)
def test_unknown_agent_or_service_404(
    egress_portal: dict[str, Any],
    write: Callable[..., Any],
    agent: str,
    service: str,
) -> None:
    """An unknown agent or service, or an agent the user may not use or that has no shell or python, is a 404."""
    response = write(egress_portal, "alice", agent, service)
    assert response.status_code == 404, response.text


def test_credential_manager_without_use_access_sees_no_egress_anywhere(egress_portal: dict[str, Any]) -> None:
    """A manager who may not use the agent is shown no egress rows and cannot mutate them."""
    client = egress_portal["client"]
    carol_headers = egress_portal["headers"]["carol"]

    catalog = client.get("/api/connections", headers=carol_headers)
    assert catalog.status_code == 200, catalog.text
    shared_dev = next(agent for agent in catalog.json()["agents"] if agent["agent_name"] == "shared_dev")
    assert shared_dev["can_use"] is False
    assert _state_egress(egress_portal, "carol", "shared_dev") is None

    assert _listing(egress_portal, "carol").json()["agents"] == []

    response = _put_key(egress_portal, "carol", "shared_dev", "github")
    assert response.status_code == 404, response.text


# --- Connections portal agent state ---


def _catalog_egress(portal: dict[str, Any], user: str) -> dict[str, list[dict[str, Any]] | None]:
    """Return each catalog agent's egress rows, as the portal loads them from the agent's state."""
    response = portal["client"].get("/api/connections", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    return {
        agent["agent_name"]: _state_egress(portal, user, agent["agent_name"]) for agent in response.json()["agents"]
    }


def test_portal_state_includes_egress_services(egress_portal: dict[str, Any]) -> None:
    """When MINDROOM_CONNECTIONS_AGENT is set, the portal lists the services of eligible agents only."""
    agents = _catalog_egress(egress_portal, "alice")
    assert {"personal", "shared_dev"} <= agents.keys()
    for name, services in agents.items():
        if name in ("personal", "shared_dev"):
            assert {s["name"] for s in services or []} == {"github", "openai"}
        else:
            assert services is None


def test_portal_state_sends_an_empty_list_for_an_eligible_agent_without_services(
    egress_portal: dict[str, Any],
) -> None:
    """An agent the user may use egress for gets a list even when empty, so the portal can link its services page."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["services"] = {}
    _republish(portal)

    agents = _catalog_egress(portal, "alice")

    assert agents["personal"] == []
    assert agents["shared_dev"] == []
    assert {name for name, services in agents.items() if services is None} == set(agents) - {"personal", "shared_dev"}
    # The user of the agent keeps writing services, so the list fills as soon as one exists.
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert [row["name"] for row in _state_services(portal, "alice", "personal")] == ["notes"]


def _storage_dirs(portal: dict[str, Any]) -> set[Path]:
    return {path for path in portal["paths"].storage_root.rglob("*") if path.is_dir()}


def test_portal_state_reads_no_egress_state_while_the_broker_is_off(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a broker port the portal sends no egress rows and reads no egress store or scope directory."""
    _restart_with_env(egress_portal, MINDROOM_EGRESS_BROKER_PORT="")
    services_read: list[object] = []
    read_services = egress_status.effective_config
    monkeypatch.setattr(
        egress_status,
        "effective_config",
        lambda *args: services_read.append(args) or read_services(*args),
    )
    dirs_before = _storage_dirs(egress_portal)

    agents = _catalog_egress(egress_portal, "alice")

    assert {"personal", "shared_dev"} <= agents.keys()
    assert list(agents.values()) == [None] * len(agents)
    assert services_read == []
    assert _storage_dirs(egress_portal) == dirs_before


def test_portal_state_survives_egress_state_that_cannot_be_read(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable egress store hides the agent's egress rows and neither fails its state nor leaks the error."""

    def unreadable(*_args: object) -> Never:
        msg = "internal-store-detail"
        raise OSError(msg)

    monkeypatch.setattr(egress_status, "effective_config", unreadable)

    with capture_logs() as logs:
        responses = [_state(egress_portal, "alice", agent) for agent in ("personal", "shared_dev", "no_shell")]

    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json()["egress_services"] is None
        assert "internal-store-detail" not in response.text
    warnings = [log for log in logs if log["event"] == "connection_state_egress_unavailable"]
    assert {log["agent_name"] for log in warnings} == {"personal", "shared_dev"}
    assert {log["error_type"] for log in warnings} == {"OSError"}


def test_portal_catalog_lists_the_egress_tool_only_while_the_broker_is_on(egress_portal: dict[str, Any]) -> None:
    """An agent that lists the Egress Credentials tool shows it among its tools only while the broker is on."""
    portal = egress_portal

    def personal_tools() -> list[str]:
        response = portal["client"].get("/api/connections", headers=portal["headers"]["alice"])
        assert response.status_code == 200, response.text
        personal = next(agent for agent in response.json()["agents"] if agent["agent_name"] == "personal")
        return [tool["name"] for tool in personal["tools"]]

    listed = personal_tools()
    assert "egress_credentials" in listed
    _restart_with_env(portal, MINDROOM_EGRESS_BROKER_PORT="")
    assert personal_tools() == [name for name in listed if name != "egress_credentials"]


# --- Connected accounts (OAuth) ---


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


def test_status_shows_the_oauth_connection_and_a_stored_key_wins(oauth_egress_portal: dict[str, Any]) -> None:
    """A connected account shows per scope, an explicit key takes over, and removing it falls back."""
    portal = oauth_egress_portal
    before = _egress_row(portal, "alice", "personal", "drive")
    assert before["active_source"] is None
    assert before["configured"] is False
    assert before["key_configured"] is False
    assert before["oauth"] == _oauth_block()

    _connect_and_allow(portal, "alice", "personal", "drive", "google_drive")

    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["active_source"] == "oauth"
    assert row["configured"] is True
    assert row["key_configured"] is False
    assert row["key_updated_at"] is None
    assert row["oauth"]["connected"] is True
    assert row["oauth"]["account_label"] == "alice@example.com"
    assert _egress_row(portal, "bob", "personal", "drive")["oauth"]["connected"] is False

    put = _put_key(portal, "alice", "personal", "drive", "alice-key")
    assert put.status_code == 204, put.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["active_source"] == "key"
    assert row["key_configured"] is True
    assert row["key_updated_at"] is not None
    assert "updated_at" not in row
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
        response = _account_action(portal, user, agent, "drive", "connect")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "google_drive"
        assert body["auth_url"].startswith("https://auth.example.test/google_drive/authorize?")
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize(
    ("action", "masked"),
    [("connect", "Could not start account connection"), ("disconnect", "Could not disconnect account")],
)
def test_connect_and_disconnect_mask_an_internal_oauth_error(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    masked: str,
) -> None:
    """A refusal from the OAuth flow keeps its status, but the route says only its own text and never the detail."""
    portal = oauth_egress_portal
    monkeypatch.setattr(oauth, action, AsyncMock(side_effect=HTTPException(503, "internal-client-secret")))
    response = _account_action(portal, "alice", "personal", "drive", action)
    assert response.status_code == 503
    assert response.json() == {"detail": masked}
    assert "internal-client-secret" not in response.text
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
    response = _account_action(portal, user, agent, service, action)
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

    response = _account_action(portal, "alice", "personal", "drive", "disconnect")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "disconnected", "provider": "google_drive"}
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["connected"] is False
    assert _egress_row(portal, "alice", "personal", "drive")["active_source"] is None
    assert _egress_row(portal, "bob", "personal", "drive")["oauth"]["connected"] is True


def test_shared_agent_connection_is_managed_but_visible_to_users(oauth_egress_portal: dict[str, Any]) -> None:
    """Users of a shared agent see that it is connected and as whom, while the connect action stays with managers."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "shared_dev", "drive", "google_drive")

    manager = _egress_row(portal, "bob", "shared_dev", "drive")["oauth"]
    assert manager["connected"] is True
    assert manager["account_label"] == "alice@example.com"
    assert manager["can_connect"] is True
    user = _egress_row(portal, "alice", "shared_dev", "drive")["oauth"]
    assert user["connected"] is True
    assert user["account_label"] == "alice@example.com"
    assert user["can_connect"] is False


def test_shared_agent_account_label_is_visible_to_every_user(oauth_egress_portal: dict[str, Any]) -> None:
    """Everyone who uses a shared agent sees which account they act as, on both pages."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "shared_dev", "drive", "google_drive")

    for user in ("alice", "bob"):
        assert _egress_row(portal, user, "shared_dev", "drive")["oauth"]["account_label"] == "alice@example.com"
        assert _portal_oauth_view(portal, user, "shared_dev")["account_label"] == "alice@example.com"
    # The label belongs to the agent's own store: nobody's other agent shows it.
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["account_label"] is None


def test_personal_account_label_never_reaches_a_shared_agent(oauth_egress_portal: dict[str, Any]) -> None:
    """A requester-scoped account is refused on a shared agent, so showing labels there never reveals it."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "personal", "gh", "github")
    assert _egress_row(portal, "bob", "personal", "gh")["oauth"]["account_label"] == "alice@example.com"

    for user in ("alice", "bob"):
        shared = _egress_row(portal, user, "shared_dev", "gh")["oauth"]
        assert shared["account_label"] is None
        assert shared["connected"] is False
        assert shared["unavailable_reason"] == "shared_sandbox"
    assert _egress_row(portal, "alice", "personal", "gh")["oauth"]["account_label"] is None


_GITHUB_TOKEN_RESPONSE = {
    "access_token": "gho-user-access",
    "refresh_token": "ghr-user-refresh",
    "expires_in": 28_800,
    "scope": "",
    "token_type": "bearer",
}


def _use_real_github_provider(
    portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,  # noqa: ANN401
) -> list[httpx.Request]:
    """Serve the real GitHub provider, answering its login lookup with `handler`, and record the lookups."""
    drive = portal["providers"]["google_drive"]
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (drive, github_oauth_provider()))
    oauth_registry.clear_oauth_provider_cache()
    lookups: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        lookups.append(request)
        return handler(request)

    monkeypatch.setattr(
        github_oauth,
        "_login_lookup_client",
        lambda: httpx.Client(transport=httpx.MockTransport(record), timeout=LOGIN_LOOKUP_TIMEOUT_SECONDS),
    )
    return lookups


@pytest.mark.parametrize(
    ("answer", "label"),
    [
        (lambda _request: httpx.Response(200, json={"login": "octo", "id": 1, "email": None}), "octo"),
        (lambda _request: httpx.Response(401, json={"message": "Bad credentials"}), None),
        (lambda _request: httpx.Response(200, text="not json"), None),
        (lambda _request: httpx.Response(200, json={"login": "bad login\n<script>"}), None),
        (lambda _request: httpx.Response(200, json=["octo"]), None),
    ],
    ids=["login", "refused", "not_json", "unsafe_login", "not_an_object"],
)
def test_github_connection_label_is_the_login(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,  # noqa: ANN401
    label: str | None,
) -> None:
    """After connecting, GitHub shows the account login; a lookup that fails shows no label and still connects."""
    portal = oauth_egress_portal
    lookups = _use_real_github_provider(portal, monkeypatch, answer)

    with patch(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(return_value=_GITHUB_TOKEN_RESPONSE),
    ):
        _connect_account(portal, "bob", "personal", "gh", "github")

    oauth_view = _egress_row(portal, "bob", "personal", "gh")["oauth"]
    assert oauth_view["connected"] is True
    assert oauth_view["account_label"] == label
    assert [(str(request.url), request.headers["authorization"]) for request in lookups] == [
        ("https://api.github.com/user", "Bearer gho-user-access"),
    ]
    assert _egress_row(portal, "alice", "personal", "gh")["oauth"]["account_label"] is None


def test_github_login_lookup_failure_never_leaks_the_token(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup that cannot even connect still connects the account, and neither responses nor logs carry the token."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        message = "api.github.com unreachable"
        raise httpx.ConnectError(message, request=request)

    portal = oauth_egress_portal
    _use_real_github_provider(portal, monkeypatch, unreachable)
    with (
        capture_logs() as logs,
        patch(
            "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
            new=AsyncMock(return_value=_GITHUB_TOKEN_RESPONSE),
        ),
    ):
        _connect_account(portal, "bob", "personal", "gh", "github")

    row = _egress_row(portal, "bob", "personal", "gh")
    assert row["oauth"]["connected"] is True
    assert row["oauth"]["account_label"] is None
    failures = [entry for entry in logs if entry["event"] == "oauth_account_claims_read_failed"]
    assert [(entry["provider_id"], entry["error_type"]) for entry in failures] == [("github", "ConnectError")]
    assert "gho-user-access" not in json.dumps(logs, default=str)
    assert "gho-user-access" not in json.dumps(row)


def test_requester_scoped_provider_on_a_shared_agent_is_unavailable_without_the_opt_in(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """Everyone's commands share a shared agent's worker, so a GitHub account is neither used nor offered there."""
    portal = oauth_egress_portal
    _connect_account(portal, "bob", "personal", "gh", "github")
    assert _egress_row(portal, "bob", "personal", "gh")["oauth"]["connected"] is True

    for user in ("alice", "bob"):
        row = _egress_row(portal, user, "shared_dev", "gh")
        assert row["oauth"] == _oauth_block(provider="github", can_connect=False, unavailable_reason="shared_sandbox")
        assert row["active_source"] is None
        assert row["configured"] is False
    # Agent-scoped providers keep their shared connection on the same agent.
    assert _egress_row(portal, "bob", "shared_dev", "drive")["oauth"]["unavailable_reason"] is None


def test_requester_scoped_provider_on_a_personal_agent_is_unavailable_on_the_static_runner(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """The static runner serves every user's calls from one process, so a personal agent shares its sandbox there."""
    portal = oauth_egress_portal
    _connect_and_allow(portal, "bob", "personal", "gh", "github")
    assert _egress_row(portal, "bob", "personal", "gh")["oauth"]["connected"] is True

    _restart_with_env(portal, MINDROOM_WORKER_BACKEND="static_runner")

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


def test_service_account_provider_cannot_be_connected(oauth_egress_portal: dict[str, Any]) -> None:
    """A shared Google service account is runtime configuration, never a personal account."""
    portal = oauth_egress_portal
    _restart_with_env(portal, GOOGLE_SERVICE_ACCOUNT_FILE="service-account.json")

    response = _account_action(portal, "alice", "personal", "drive", "connect")
    assert response.status_code == 409, response.text
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["can_connect"] is False


def test_personal_connection_stored_before_a_service_account_can_still_be_disconnected(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """The broker keeps injecting a stored personal token, so the user must be able to revoke it."""
    portal = oauth_egress_portal
    _connect_and_allow(portal, "alice", "personal", "drive", "google_drive")
    _restart_with_env(portal, GOOGLE_SERVICE_ACCOUNT_FILE="service-account.json")
    assert _egress_row(portal, "alice", "personal", "drive")["active_source"] == "oauth"

    response = _account_action(portal, "alice", "personal", "drive", "disconnect")
    assert response.status_code == 200, response.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["oauth"]["connected"] is False
    assert row["oauth"]["service_account"] is True
    assert row["active_source"] is None


def test_portal_state_includes_the_oauth_status(oauth_egress_portal: dict[str, Any]) -> None:
    """The Connections portal shows the same OAuth status as the egress page, the opt-in included."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    tools_only = {service["name"]: service for service in _state_services(portal, "alice", "personal")}["drive"]
    assert (tools_only["oauth"]["connected"], tools_only["oauth"]["reuse_enabled"]) == (True, False)
    assert tools_only["active_source"] is None
    assert tools_only == _egress_row(portal, "alice", "personal", "drive")
    assert _allow_reuse(portal, "alice", "personal", "drive").status_code == 204

    rows = {service["name"]: service for service in _state_services(portal, "alice", "personal")}
    drive = rows["drive"]
    assert drive["active_source"] == "oauth"
    assert drive["oauth"]["connected"] is True
    assert drive["oauth"]["account_label"] == "alice@example.com"
    assert drive == _egress_row(portal, "alice", "personal", "drive")
    assert rows["github"]["oauth"] is None


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
    assert alice_shared["account_label"] == "alice@example.com"
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
    _restart_with_env(portal, GOOGLE_SERVICE_ACCOUNT_FILE="service-account.json")

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

    put = _put_key(portal, "alice", "personal", "drive", "alice-key")
    assert put.status_code == 204, put.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["active_source"] == "key"
    assert row["oauth"]["connected"] is False


def test_service_account_keeps_reporting_a_personal_connection_the_broker_still_uses(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """A stored personal token is injected whatever the service account, so the status matches the broker."""
    portal = oauth_egress_portal
    _connect_and_allow(portal, "alice", "personal", "drive", "google_drive")
    _restart_with_env(portal, GOOGLE_SERVICE_ACCOUNT_FILE="service-account.json")

    row = _egress_row(portal, "alice", "personal", "drive")
    assert row["oauth"] == _oauth_block(
        connected=True,
        account_label="alice@example.com",
        can_connect=False,
        service_account=True,
        reuse_enabled=True,
    )
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
    assert isinstance(token, secrets.Secret)


def test_a_personal_connection_beside_a_service_account_stays_visible_to_allow_stop_and_remove(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page shows a personal connection stored before a service account, so the user can allow or remove it.

    Without the opt-in the broker and the status behind placeholders still never read it; allowing and stopping it
    flips only whether Agent Keys uses it.
    """
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    _restart_with_env(portal, GOOGLE_SERVICE_ACCOUNT_FILE="service-account.json")
    tools_only = _oauth_block(
        connected=True,
        account_label="alice@example.com",
        can_connect=False,
        service_account=True,
    )
    config = Config.model_validate(portal["payload"], context={"runtime_paths": portal["paths"]})
    target = _broker_target(portal, "@alice:example.org", "personal")

    def never(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the broker must not read a connection Agent Keys may not use")

    before = _egress_row(portal, "alice", "personal", "drive")
    with monkeypatch.context() as broker_reads_nothing:
        broker_reads_nothing.setattr(oauth_source, "load_oauth_credentials_snapshot_sync", never)
        broker_reads_nothing.setattr(oauth_source, "refresh_oauth_credentials_blocking", never)
        token = _broker_reads(portal, "@alice:example.org", "personal", "drive", "google_drive")
        placeholder_status = oauth_source.oauth_status(
            "google_drive",
            target,
            service="drive",
            config=config,
            runtime_paths=portal["paths"],
            credentials_manager=get_runtime_credentials_manager(portal["paths"]),
        )
    allowed = _allow_reuse(portal, "alice", "personal", "drive")
    after_allow = _egress_row(portal, "alice", "personal", "drive")
    stopped = _stop_reuse(portal, "alice", "personal", "drive")
    after_stop = _egress_row(portal, "alice", "personal", "drive")

    assert (before["oauth"], before["active_source"]) == (tools_only, None)
    assert token == secrets.SecretMissing(provider="google_drive", reuse_off=True)
    assert placeholder_status is not None
    assert (placeholder_status.connected, placeholder_status.reuse_enabled) == (False, False)
    assert (allowed.status_code, stopped.status_code) == (204, 204), (allowed.text, stopped.text)
    assert (after_allow["oauth"], after_allow["active_source"]) == ({**tools_only, "reuse_enabled": True}, "oauth")
    assert (after_stop["oauth"], after_stop["active_source"]) == (tools_only, None)


def test_listing_never_refreshes_tokens(oauth_egress_portal: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing and the portal's agent state read stored state, so they never wait on a provider's token endpoint."""
    portal = oauth_egress_portal
    _connect_account(portal, "alice", "personal", "drive", "google_drive")

    def refresh_must_not_run(*_args: object, **_kwargs: object) -> None:
        msg = "A listing must not refresh tokens"
        raise AssertionError(msg)

    monkeypatch.setattr(oauth, "refresh_oauth_credentials", refresh_must_not_run)
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["connected"] is True
    drive = next(row for row in _state_services(portal, "alice", "personal") if row["name"] == "drive")
    assert drive["oauth"]["connected"] is True


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
    degraded = _oauth_block(can_connect=False)
    listing = _listing(portal, "alice")
    state = _state(portal, "alice", "personal")
    assert state.status_code == 200, state.text
    assert "internal-client-secret" not in listing.text + state.text
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"] == degraded
    assert _egress_row(portal, "alice", "personal", "gh")["oauth"]["can_connect"] is True
    rows = {service["name"]: service for service in state.json()["egress_services"]}
    assert rows["drive"]["oauth"] == degraded
    assert rows["gh"]["oauth"]["can_connect"] is True


def test_portal_routes_never_load_egress_oauth_status(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an agent's state reads egress OAuth state; the catalog, status, connect and disconnect do not."""
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
    assert client.get("/api/connections", headers=headers).status_code == 200
    assert calls == []

    assert client.get("/api/connections/agents/personal/state", headers=headers).status_code == 200
    assert calls == ["personal"]


def test_listing_accounts_never_connected_creates_no_credential_store(oauth_egress_portal: dict[str, Any]) -> None:
    """The listing and the portal's agent state read accounts nobody connected without leaving empty stores behind."""
    portal = oauth_egress_portal
    for user in ("alice", "bob"):
        _listing(portal, user)
        for agent in ("personal", "shared_dev"):
            assert _state(portal, user, agent).status_code == 200
    assert list(portal["paths"].storage_root.rglob("*.sqlite3")) == []


def test_key_status_is_read_off_the_event_loop(
    oauth_egress_portal: dict[str, Any],
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """Reading a key's status decrypts a credential file, so the async listing runs it in a thread."""
    portal = oauth_egress_portal
    on_loop = calls_on_event_loop(secrets, "load_egress_document")
    _listing(portal, "alice")
    _state_services(portal, "alice", "personal")
    assert on_loop
    assert not any(on_loop)


def test_listing_and_state_read_no_oauth_client_on_the_event_loop(
    oauth_egress_portal: dict[str, Any],
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """The OAuth client is a credential file, read in a thread, and not at all for an account never connected."""
    portal = oauth_egress_portal
    on_loop = calls_on_event_loop(CredentialsManager, "load_credentials")
    usability_reads = calls_on_event_loop(OAuthProvider, "client_config")
    _listing(portal, "alice")
    _state_services(portal, "alice", "personal")
    assert on_loop
    assert not any(on_loop)
    assert usability_reads == []

    _connect_account(portal, "alice", "personal", "drive", "google_drive")
    on_loop.clear()
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["connected"] is True
    _state_services(portal, "alice", "personal")
    assert on_loop
    assert not any(on_loop)


def require_concurrent_calls(monkeypatch: pytest.MonkeyPatch, module: object, name: str, expected: int) -> list[str]:
    """Make each call of `module.name`, an `egress_service_status`, wait until `expected` calls have started.

    Return the names of the services they load. Calls that run one after another never get there, so the first one
    fails after a short wait.
    """
    real = getattr(module, name)
    started: list[str] = []
    all_started: list[asyncio.Event] = []

    async def gated(manager: object, target: object, service: object, service_name: str, oauth_part: object) -> object:
        if not all_started:
            all_started.append(asyncio.Event())
        started.append(service_name)
        if len(started) == expected:
            all_started[0].set()
        async with asyncio.timeout(5):
            await all_started[0].wait()
        return await real(manager, target, service, service_name, oauth_part)

    monkeypatch.setattr(module, name, gated)
    return started


def test_listing_loads_the_rows_of_every_agent_at_once(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row awaits several thread hops, so rows one after another would keep a busy server's page loading."""
    started = require_concurrent_calls(monkeypatch, egress_credentials, "egress_service_status", expected=4)
    agents = _listed_agents(egress_portal, "alice")
    assert list(agents) == ["personal", "shared_dev"]
    assert [[row["name"] for row in agent["services"]] for agent in agents.values()] == [["github", "openai"]] * 2
    assert sorted(started) == ["github", "github", "openai", "openai"]


def track_concurrent_calls(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> list[int]:
    """Hold each call of the async `module.name` briefly and return `[running, peak]` concurrent calls."""
    real = getattr(module, name)
    counts = [0, 0]

    async def tracked(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        counts[0] += 1
        counts[1] = max(counts)
        try:
            await asyncio.sleep(0.02)
            return await real(*args, **kwargs)
        finally:
            counts[0] -= 1

    monkeypatch.setattr(module, name, tracked)
    return counts


@pytest.mark.asyncio
async def test_overlapping_listings_share_one_limit_on_status_loads(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two listings on one loop load at most `_STATUS_LOAD_LIMIT` service statuses at once between them, in order."""
    monkeypatch.setattr(egress_status, "_STATUS_LOAD_LIMIT", 3)
    counts = track_concurrent_calls(monkeypatch, egress_credentials, "egress_service_status")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://portal.example.org") as client:
        responses = await asyncio.gather(
            *(
                client.get("/api/connections/egress", headers=egress_portal["headers"][user])
                for user in ("alice", "bob")
            ),
        )
    for response in responses:
        assert response.status_code == 200, response.text
        agents = response.json()["agents"]
        assert [agent["agent_name"] for agent in agents] == ["personal", "shared_dev"]
        assert [[row["name"] for row in agent["services"]] for agent in agents] == [["github", "openai"]] * 2
    assert counts == [0, 3]


@pytest.mark.asyncio
async def test_a_failing_row_cancels_the_rows_still_loading(
    egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first failing row fails the listing with its own error, only after every other row stopped loading."""
    blocked: set[str] = set()
    others_blocked: list[asyncio.Event] = []
    cancelled: list[str] = []
    real_load = egress_credentials._load_egress_agents
    unfinished_when_failed: list[set[str]] = []

    async def status(_manager: object, target: Any, _service: object, name: str, _oauth: object) -> Never:  # noqa: ANN401
        if not others_blocked:
            others_blocked.append(asyncio.Event())
        row = f"{target.routing_agent_name}/{name}"
        if row == "shared_dev/openai":
            await others_blocked[0].wait()
            msg = "store unreadable"
            raise RuntimeError(msg)
        blocked.add(row)
        if len(blocked) == 3:
            others_blocked[0].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(row)
            raise
        finally:
            blocked.discard(row)
        raise AssertionError

    async def load(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            return await real_load(*args, **kwargs)
        except Exception:
            unfinished_when_failed.append(set(blocked))
            raise

    monkeypatch.setattr(egress_credentials, "egress_service_status", status)
    monkeypatch.setattr(egress_credentials, "_load_egress_agents", load)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://portal.example.org") as client:
        with pytest.raises(RuntimeError, match="store unreadable"):
            await client.get("/api/connections/egress", headers=egress_portal["headers"]["alice"])
    assert unfinished_when_failed == [set()]
    assert sorted(cancelled) == ["personal/github", "personal/openai", "shared_dev/github"]


@pytest.mark.asyncio
async def test_load_concurrently_keeps_order_and_raises_the_first_failure() -> None:
    """Results keep the loads' order; a failure is raised as itself once the loads still running are cancelled."""

    async def after(delay: float, value: int) -> int:
        await asyncio.sleep(delay)
        return value

    assert await egress_status.load_concurrently(
        [lambda: after(0.02, 1), lambda: after(0, 2), lambda: after(0.01, 3)],
        limited=True,
    ) == [1, 2, 3]

    cancelled: list[str] = []

    async def stuck() -> int:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("stuck")
            raise
        raise AssertionError

    async def failing() -> int:
        await asyncio.sleep(0)
        msg = "boom"
        raise ValueError(msg) from OSError("disk")

    with pytest.raises(ValueError, match="boom") as raised:
        await egress_status.load_concurrently([stuck, failing])
    assert cancelled == ["stuck"]
    assert isinstance(raised.value.__cause__, OSError)


def test_disconnecting_a_requester_scoped_account_leaves_other_users_connected(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """On a shared agent with the opt-in each user's GitHub connection is their own."""
    portal = oauth_egress_portal
    _opt_in_to_shared_workers(portal)
    _connect_account(portal, "alice", "shared_dev", "gh", "github")
    _connect_account(portal, "bob", "shared_dev", "gh", "github")

    response = _account_action(portal, "alice", "shared_dev", "gh", "disconnect")
    assert response.status_code == 200, response.text
    assert _egress_row(portal, "alice", "shared_dev", "gh")["oauth"]["connected"] is False
    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["connected"] is True


def test_plain_user_of_a_shared_agent_connects_their_own_requester_scoped_account(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """GitHub-style connections belong to the requester, so with the opt-in connecting needs no management."""
    portal = oauth_egress_portal
    _opt_in_to_shared_workers(portal)

    before = _egress_row(portal, "alice", "shared_dev", "gh")
    assert before["can_manage"] is False
    assert before["oauth"]["can_connect"] is True
    _connect_and_allow(portal, "alice", "shared_dev", "gh", "github")

    alice = _egress_row(portal, "alice", "shared_dev", "gh")
    assert alice["oauth"]["connected"] is True
    assert alice["oauth"]["account_label"] == "alice@example.com"
    assert alice["active_source"] == "oauth"
    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["connected"] is False

    # The API key stays a shared credential that only managers may set.
    put = _put_key(portal, "alice", "shared_dev", "gh", "alice-key")
    assert put.status_code == 403, put.text

    disconnect = _account_action(portal, "alice", "shared_dev", "gh", "disconnect")
    assert disconnect.status_code == 200, disconnect.text
    assert _egress_row(portal, "alice", "shared_dev", "gh")["oauth"]["connected"] is False


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_agent_scoped_provider_on_a_shared_agent_still_needs_management(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """A shared connection is shared state: plain users cannot connect it and are not offered the action."""
    portal = oauth_egress_portal
    assert _egress_row(portal, "alice", "shared_dev", "drive")["oauth"]["can_connect"] is False
    response = _account_action(portal, "alice", "shared_dev", "drive", action)
    assert response.status_code == 403, response.text


def _broker_target(portal: dict[str, Any], requester_id: str, agent: str) -> ResolvedWorkerTarget:
    """Return the target the broker rebuilds from the proxy token of a tool call by `requester_id` on `agent`.

    It is built from the agent's worker scope and a tool call's identity, independently of the API's own target.
    """
    config = Config.model_validate(portal["payload"], context={"runtime_paths": portal["paths"]})
    scope = egress_agent_scope(config, agent)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )
    target = resolve_worker_target(None if scope == "unscoped" else scope, agent, identity)
    # An unscoped call's token names the worker it was routed to.
    claims = WorkerClaims.from_worker_target(replace(target, worker_key=target.worker_key or "v1:local:unscoped:w"))
    assert claims is not None
    return claims.to_worker_target()


def _broker_reads(portal: dict[str, Any], requester_id: str, agent: str, service: str, provider: str) -> object:
    """Return what the broker resolves for `service` in a tool call by `requester_id` on `agent`."""
    return resolve_oauth_token(
        service=service,
        provider_id=provider,
        config=Config.model_validate(portal["payload"], context={"runtime_paths": portal["paths"]}),
        runtime_paths=portal["paths"],
        credentials_manager=get_runtime_credentials_manager(portal["paths"]),
        worker_target=_broker_target(portal, requester_id, agent),
    )


def _add_agent(portal: dict[str, Any], name: str, **agent: Any) -> None:  # noqa: ANN401
    """Add an agent with Agent Keys and the Drive tool that Alice and Bob may use, then republish."""
    portal["payload"]["agents"][name] = _agent_config(
        name.title(),
        "Round trip agent",
        users=("@alice:example.org", "@bob:example.org"),
        tools=("shell", "egress_credentials"),
        **agent,
    )
    portal["payload"]["agents"][name]["tools"].append({"name": "google_drive", "defer": True})
    _republish(portal)


@pytest.mark.parametrize(
    ("agent", "agent_config", "user", "service", "provider", "also_reads"),
    [
        ("workspace", {"private": "user"}, "alice", "drive", "google_drive", None),
        ("personal", None, "alice", "drive", "google_drive", None),
        ("shared_dev", None, "bob", "drive", "google_drive", "alice"),
        ("unscoped", {"credential_managers": ["@bob:example.org"]}, "bob", "drive", "google_drive", "alice"),
        ("personal", None, "alice", "gh", "github", None),
        ("shared_dev", None, "alice", "gh", "github", None),
    ],
    ids=["user", "user_agent", "shared", "unscoped", "github-user_agent", "github-shared-opted-in"],
)
def test_an_opt_in_written_by_the_api_is_the_one_the_broker_reads(
    oauth_egress_portal: dict[str, Any],
    agent: str,
    agent_config: dict[str, Any] | None,
    user: str,
    service: str,
    provider: str,
    also_reads: str | None,
) -> None:
    """Allowing an account on the page unlocks exactly the store the broker reads for a tool call in that scope.

    Before, the broker answers that Agent Keys may not use the account; after, it gets the token, and so does everyone
    who shares an agent's own account; stopping it locks the account again.
    """
    portal = oauth_egress_portal
    if agent_config is not None:
        _add_agent(portal, agent, **agent_config)
    if service == "gh" and agent == "shared_dev":
        _opt_in_to_shared_workers(portal)
    requester = f"@{user}:example.org"
    requester_owned = provider == "github"
    off = secrets.SecretMissing(provider=provider, reuse_off=True, requester_owned=requester_owned)
    _connect_account(portal, user, agent, service, provider)

    before = _broker_reads(portal, requester, agent, service, provider)
    allowed = _allow_reuse(portal, user, agent, service)
    after = _broker_reads(portal, requester, agent, service, provider)
    row = _egress_row(portal, user, agent, service)
    other = (
        None if also_reads is None else _broker_reads(portal, f"@{also_reads}:example.org", agent, service, provider)
    )
    stopped = _stop_reuse(portal, user, agent, service)
    locked = _broker_reads(portal, requester, agent, service, provider)

    assert before == off
    assert (allowed.status_code, stopped.status_code) == (204, 204), (allowed.text, stopped.text)
    assert isinstance(after, secrets.Secret)
    assert (row["oauth"]["reuse_enabled"], row["active_source"]) == (True, "oauth")
    assert other is None or other == after
    assert locked == off


def test_allowing_an_account_requires_acknowledging_the_approval_bypass(oauth_egress_portal: dict[str, Any]) -> None:
    """Commands act as the account without approval prompts, so enabling needs that confirmation in the body (422)."""
    portal = oauth_egress_portal
    url = _reuse_url("personal", "drive")
    headers = portal["headers"]["alice"]

    responses = [
        portal["client"].put(url, headers=headers),
        portal["client"].put(url, headers=headers, json={}),
        portal["client"].put(url, headers=headers, json={"acknowledge_approval_bypass": False}),
        portal["client"].put(url, headers=headers, json={"acknowledge_approval_bypass": True, "agent_name": "x"}),
    ]

    assert [response.status_code for response in responses] == [422] * 4
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["reuse_enabled"] is False


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize(
    ("user", "agent", "service", "expected"),
    [
        ("alice", "personal", "drive", 204),
        ("bob", "shared_dev", "drive", 204),
        ("alice", "personal", "gh", 204),
        ("alice", "shared_dev", "drive", 403),
        ("alice", "shared_dev", "github", 403),
        ("bob", "shared_dev", "gh", 409),
        ("carol", "shared_dev", "drive", 404),
        ("alice", "no_shell", "drive", 404),
        ("alice", "other_private", "drive", 404),
        ("alice", "unknown", "drive", 404),
        ("alice", "personal", "unknown", 404),
        ("alice", "personal", "github", 404),
        ("alice", "personal", "ghost", 404),
    ],
)
def test_allowing_and_stopping_an_account_use_the_account_route_gates(
    oauth_egress_portal: dict[str, Any],
    method: str,
    user: str,
    agent: str,
    service: str,
    expected: int,
) -> None:
    """Whoever may connect or disconnect the account may allow or stop it; a shared sandbox's refusal is a 409."""
    portal = oauth_egress_portal
    body = {"json": {"acknowledge_approval_bypass": True}} if method == "put" else {}

    response = getattr(portal["client"], method)(_reuse_url(agent, service), headers=portal["headers"][user], **body)

    assert response.status_code == expected, response.text
    assert "no-store" in response.headers["cache-control"]


def test_a_requester_scoped_account_on_a_shared_agent_is_each_users_own_to_allow(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """With the opt-in a plain user allows their own GitHub account there, which leaves everyone else's alone."""
    portal = oauth_egress_portal
    _opt_in_to_shared_workers(portal)
    _connect_account(portal, "alice", "shared_dev", "gh", "github")
    _connect_account(portal, "bob", "shared_dev", "gh", "github")

    allowed = _allow_reuse(portal, "alice", "shared_dev", "gh")

    assert allowed.status_code == 204, allowed.text
    assert _egress_row(portal, "alice", "shared_dev", "gh")["active_source"] == "oauth"
    bob = _egress_row(portal, "bob", "shared_dev", "gh")
    assert (bob["oauth"]["connected"], bob["oauth"]["reuse_enabled"], bob["active_source"]) == (True, False, None)


@pytest.mark.parametrize("method", ["put", "delete"])
def test_allowing_and_stopping_an_account_reject_other_origins(
    oauth_egress_portal: dict[str, Any],
    method: str,
) -> None:
    """Cross-site requests and query selectors cannot change whether Agent Keys uses an account."""
    portal = oauth_egress_portal
    url = _reuse_url("personal", "drive")
    headers = portal["headers"]["alice"]
    body = {"json": {"acknowledge_approval_bypass": True}} if method == "put" else {}
    send = getattr(portal["client"], method)
    no_origin = {key: value for key, value in headers.items() if key != "Origin"}

    assert send(url, headers={**headers, "Origin": "https://evil.example.org"}, **body).status_code == 403
    assert send(url, headers=no_origin, **body).status_code == 403
    assert send(f"{url}?agent_name=other", headers=headers, **body).status_code == 400
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["reuse_enabled"] is False


def test_disconnecting_keeps_the_opt_in_so_the_broker_offers_a_connect_link(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """Disconnect removes the connection, not the opt-in: the broker then offers to connect an account again."""
    portal = oauth_egress_portal
    _connect_and_allow(portal, "alice", "personal", "drive", "google_drive")

    disconnect = _account_action(portal, "alice", "personal", "drive", "disconnect")
    missing = _broker_reads(portal, "@alice:example.org", "personal", "drive", "google_drive")

    assert disconnect.status_code == 200, disconnect.text
    row = _egress_row(portal, "alice", "personal", "drive")
    assert (row["oauth"]["connected"], row["oauth"]["reuse_enabled"], row["active_source"]) == (False, True, None)
    assert isinstance(missing, secrets.SecretMissing)
    assert (missing.provider, missing.reuse_off) == ("google_drive", False)
    assert missing.connect_url is not None


def test_deleting_a_service_of_ones_own_removes_its_opt_in(oauth_egress_portal: dict[str, Any]) -> None:
    """A service saved again under the same name starts without the account allowed."""
    portal = oauth_egress_portal
    assert _put_service(portal, "alice", "personal", "mygh", _MY_GITHUB).status_code == 204
    _connect_and_allow(portal, "alice", "personal", "mygh", "github")

    deleted = _delete_service(portal, "alice", "personal", "mygh")
    assert deleted.status_code == 204, deleted.text
    assert _put_service(portal, "alice", "personal", "mygh", _MY_GITHUB).status_code == 204

    row = _egress_row(portal, "alice", "personal", "mygh")
    assert (row["oauth"]["connected"], row["oauth"]["reuse_enabled"], row["active_source"]) == (True, False, None)


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_are_refused_where_the_broker_refuses_the_account(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """The status offers nothing in a shared sandbox, so the routes answer 409 with a readable string, not a flow."""
    portal = oauth_egress_portal

    response = _account_action(portal, "bob", "shared_dev", "gh", action)

    assert response.status_code == 409, response.text
    assert isinstance(response.json()["detail"], str)
    assert "share" in response.json()["detail"]
    assert "no-store" in response.headers["cache-control"]
    assert _egress_row(portal, "bob", "shared_dev", "gh")["oauth"]["can_connect"] is False
    # Agent-scoped providers on the same agent stay connectable, and a personal agent's own GitHub account too.
    drive = _account_action(portal, "bob", "shared_dev", "drive", action)
    assert drive.status_code == 200, drive.text
    personal = _account_action(portal, "bob", "personal", "gh", action)
    assert personal.status_code == 200, personal.text


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_are_refused_on_the_static_runner_until_the_service_opts_in(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """A personal agent shares its sandbox on the static runner, so its requester-scoped account is refused there."""
    portal = oauth_egress_portal
    _restart_with_env(portal, MINDROOM_WORKER_BACKEND="static_runner")

    refused = _account_action(portal, "alice", "personal", "gh", action)
    assert refused.status_code == 409, refused.text
    assert isinstance(refused.json()["detail"], str)

    _opt_in_to_shared_workers(portal)
    allowed = _account_action(portal, "alice", "personal", "gh", action)
    assert allowed.status_code == 200, allowed.text


@pytest.mark.parametrize("failure", ["unsupported backend", "other"])
def test_an_unreadable_worker_backend_degrades_one_service_without_failing_the_pages(
    oauth_egress_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The shared-sandbox check can fail on its own, and that spares the listing and the agent state but that service."""
    portal = oauth_egress_portal
    if failure == "unsupported backend":
        _restart_with_env(portal, MINDROOM_WORKER_BACKEND="no-such-backend")
    else:
        real = egress_status.shared_worker_oauth

        def broken(provider: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if provider.requester_scoped_credentials:
                msg = "internal-backend-detail"
                raise RuntimeError(msg)
            return real(provider, *args, **kwargs)

        monkeypatch.setattr(egress_status, "shared_worker_oauth", broken)

    listing = _listing(portal, "alice")
    state = _state(portal, "alice", "personal")

    assert state.status_code == 200, state.text
    assert "internal-backend-detail" not in listing.text + state.text
    gh = _egress_row(portal, "alice", "personal", "gh")["oauth"]
    assert (gh["connected"], gh["can_connect"], gh["unavailable_reason"]) == (False, False, None)
    assert _egress_row(portal, "alice", "personal", "drive")["oauth"]["can_connect"] is True
    assert _egress_row(portal, "alice", "personal", "github")["oauth"] is None


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_answer_503_when_the_worker_backend_cannot_be_read(
    oauth_egress_portal: dict[str, Any],
    action: str,
) -> None:
    """Without the sandbox kind neither action can be judged safe, so both fail closed with a plain 503."""
    portal = oauth_egress_portal
    _restart_with_env(portal, MINDROOM_WORKER_BACKEND="no-such-backend")

    response = _account_action(portal, "alice", "personal", "gh", action)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Account connection is unavailable right now"


# --- User-defined services ---


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
    row = _egress_row(portal, "alice", "personal", "my-github")
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
    put = _put_key(portal, "alice", "personal", "github", "alice-token")
    assert put.status_code == 204, put.text
    assert _delete_service(portal, "alice", "personal", "github").status_code == 409
    row = _egress_row(portal, "alice", "personal", "github")
    assert row["key_configured"] is True


def test_an_entry_shadowed_by_a_new_config_service_can_be_removed_and_keeps_the_key(
    egress_portal: dict[str, Any],
) -> None:
    """When the administrator later defines a service under a user entry's name, the entry is ignored but removable."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert _put_key(portal, "alice", "personal", "notes", "alice-token").status_code == 204

    portal["payload"]["egress_broker"]["services"]["notes"] = {
        "description": "Administrator notes",
        "rules": [{"host": "notes.admin.example.com", "auth": {"type": "bearer"}}],
    }
    _republish(portal)

    row = _egress_row(portal, "alice", "personal", "notes")
    assert (row["source"], row["description"], row["key_configured"]) == ("config", "Administrator notes", True)
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404
    assert _put_service(portal, "alice", "personal", "notes").status_code == 409

    assert _delete_service(portal, "alice", "personal", "notes").status_code == 204
    row = _egress_row(portal, "alice", "personal", "notes")
    assert (row["source"], row["key_configured"]) == ("config", True)
    # With the entry gone the name is a plain config service again.
    assert _delete_service(portal, "alice", "personal", "notes").status_code == 409


_LIMIT = {"restrict_only": True, "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/a"}]}


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
        # The operator-only flag is rejected by name, so a user cannot even send it as false.
        (
            {**_NOTES, "oauth_on_shared_workers": True},
            "oauth_on_shared_workers can only be set by an administrator in config.yaml",
        ),
        (
            {**_NOTES, "oauth_on_shared_workers": False},
            "oauth_on_shared_workers can only be set by an administrator in config.yaml",
        ),
        # User-only rules on placeholders and OAuth providers, which never quote the refused value.
        ({**_NOTES, "placeholder_env": {"LD_PRELOAD": "evil"}}, "LD_PRELOAD is not allowed as a placeholder"),
        (
            {**_NOTES, "placeholder_env": {"NOTES_TOKEN": "two words"}},
            "The value of NOTES_TOKEN must be 1 to 256 letters",
        ),
        ({**_NOTES, "oauth_provider": "no_such_provider"}, "unknown oauth_provider 'no_such_provider'"),
        # A limit never sends a credential, so a login or placeholders on it are refused.
        ({**_LIMIT, "oauth_provider": "github"}, "cannot set oauth_provider"),
        ({**_LIMIT, "placeholder_env": {"GH_TOKEN": "mindroom-brokered"}}, "cannot set placeholder_env"),
    ],
)
def test_invalid_user_service_is_422_with_the_validators_message(
    egress_portal: dict[str, Any],
    body: dict[str, Any],
    fragment: str,
) -> None:
    """Config and user-only validation errors reach the user as a readable 422 and nothing is stored."""
    response = _put_service(egress_portal, "alice", "personal", "notes", body)
    assert response.status_code == 422, response.text
    assert fragment in response.json()["detail"]
    assert "two words" not in response.json()["detail"]
    assert _get_service(egress_portal, "alice", "personal", "notes").status_code == 404


@pytest.mark.parametrize(
    "body",
    [["rules"], "rules", 3, None],
    ids=["list", "string", "number", "null"],  # `json=None` sends the JSON text `null`.
)
def test_a_service_body_that_is_not_an_object_is_a_string_422_with_private_headers(
    egress_portal: dict[str, Any],
    body: object,
) -> None:
    """Like every other refusal of the save, a body of the wrong shape answers with a readable string detail."""
    portal = egress_portal
    response = portal["client"].put(
        _service_url("personal", "notes"),
        json=body,
        headers=portal["headers"]["alice"],
    )
    assert response.status_code == 422, (body, response.text)
    assert response.json()["detail"] == "A service must be a JSON object"
    assert response.headers["cache-control"] == "private, no-store"
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404


def test_bad_service_names_are_422(egress_portal: dict[str, Any]) -> None:
    """Names the store reserves or the pattern forbids are refused."""
    portal = egress_portal
    for name in ("_services", "Notes", "x_oauth"):
        response = _put_service(portal, "alice", "personal", name)
        assert response.status_code == 422, (name, response.text)


def test_user_service_caps_are_422(egress_portal: dict[str, Any]) -> None:
    """At most 50 rules per service and 50 services per scope; replacing a service at the cap still works."""
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
    """A shared agent's own services cannot use an OAuth connection; a personal agent's can."""
    portal = egress_portal
    with_oauth = _MY_GITHUB
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
    _republish(portal)

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
    alice_row = _egress_row(portal, "alice", "shared_dev", "notes")
    bob_row = _egress_row(portal, "bob", "shared_dev", "notes")
    assert (alice_row["source"], alice_row["can_manage"], alice_row["is_shared"]) == ("user", False, True)
    assert (bob_row["source"], bob_row["can_manage"], bob_row["is_shared"]) == ("user", True, True)

    assert _delete_service(portal, "alice", "shared_dev", "notes").status_code == 403
    assert _get_service(portal, "alice", "shared_dev", "notes").status_code == 200
    assert _delete_service(portal, "bob", "shared_dev", "notes").status_code == 204
    assert "notes" not in _listed_names(portal, "alice", "shared_dev")
    # A manager who may not use the agent has no access at all, as for keys.
    assert _put_service(portal, "carol", "shared_dev", "notes").status_code == 404
    assert _get_service(portal, "carol", "shared_dev", "notes").status_code == 404


def test_a_non_manager_cannot_write_services_or_keys_of_an_unscoped_agent(unscoped_portal: dict[str, Any]) -> None:
    """An unscoped agent is shared by every user, so only its credential managers change services and keys."""
    portal = unscoped_portal

    agent = _listed_agents(portal, "alice")["unscoped_dev"]
    assert (agent["shared"], agent["can_manage"]) == (True, False)
    for response in (
        _put_service(portal, "alice", "unscoped_dev", "notes"),
        _delete_service(portal, "alice", "unscoped_dev", "notes"),
        _put_key(portal, "alice", "unscoped_dev", "github", "x"),
        _delete_key(portal, "alice", "unscoped_dev", "github"),
    ):
        assert response.status_code == 403, response.text
        assert "Credential management is required" in response.text
    assert _get_service(portal, "alice", "unscoped_dev", "notes").status_code == 404

    assert _put_service(portal, "bob", "unscoped_dev", "notes").status_code == 204
    assert _put_key(portal, "bob", "unscoped_dev", "github", "x").status_code == 204
    assert _get_service(portal, "alice", "unscoped_dev", "notes").json() == _NOTES
    assert _delete_service(portal, "alice", "unscoped_dev", "notes").status_code == 403


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
    """Alice's services on a private agent are invisible to Bob's listing, portal state, reads, and writes."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204

    assert _listed_names(portal, "alice", "personal") == ["github", "openai", "notes"]
    assert _listed_names(portal, "bob", "personal") == ["github", "openai"]
    assert _listed_names(portal, "alice", "shared_dev") == ["github", "openai"]
    for user, expected in (("alice", True), ("bob", False)):
        assert ("notes" in {s["name"] for s in _state_services(portal, user, "personal")}) is expected

    assert _get_service(portal, "bob", "personal", "notes").status_code == 404
    assert _delete_service(portal, "bob", "personal", "notes").status_code == 404
    key = _put_key(portal, "bob", "personal", "notes", "bob-token")
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
    _republish(portal)

    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert _get_service(portal, "bob", "personal", "notes").json() == _NOTES
    assert "notes" in _listed_names(portal, "bob", "personal")


def test_user_scope_services_and_keys_are_shared_by_the_requesters_user_scope_agents(
    egress_portal: dict[str, Any],
) -> None:
    """A `user` scope is one store per requester, so a service saved on one such agent is the service of them all."""
    portal = egress_portal
    for name, scope in (("user_a", "user"), ("user_b", "user"), ("agent_scoped", "user_agent")):
        portal["payload"]["agents"][name] = _agent_config(
            name,
            "Personal assistant",
            private=scope,
            users=("@alice:example.org", "@bob:example.org"),
        )
    _republish(portal)

    assert _put_service(portal, "alice", "user_a", "notes").status_code == 204
    assert _put_key(portal, "alice", "user_a", "notes", "k").status_code == 204

    assert _get_service(portal, "alice", "user_b", "notes").json() == _NOTES
    row = _egress_row(portal, "alice", "user_b", "notes")
    assert (row["source"], row["key_configured"]) == ("user", True)
    # Another requester, and an agent with its own scope per agent, have their own stores.
    assert _get_service(portal, "bob", "user_b", "notes").status_code == 404
    assert _get_service(portal, "alice", "agent_scoped", "notes").status_code == 404
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404

    assert _delete_service(portal, "alice", "user_b", "notes").status_code == 204
    assert _get_service(portal, "alice", "user_a", "notes").status_code == 404


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

    assert {(s["name"], s["source"]) for s in _state_services(portal, "alice", "personal")} == {
        ("github", "config"),
        ("openai", "config"),
        ("notes", "user"),
    }


def test_keys_follow_user_service_names(egress_portal: dict[str, Any]) -> None:
    """A key can be set for a user service; deleting the service removes the key and a new one starts without it."""
    portal = egress_portal
    assert _put_key(portal, "alice", "personal", "notes", "alice-token").status_code == 404

    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    put = _put_key(portal, "alice", "personal", "notes", "alice-token")
    assert put.status_code == 204, put.text
    row = _egress_row(portal, "alice", "personal", "notes")
    assert (row["configured"], row["key_configured"], row["active_source"]) == (True, True, "key")
    assert "alice-token" not in _listing(portal, "alice").text

    assert _delete_key(portal, "alice", "personal", "notes").status_code == 204
    row = _egress_row(portal, "alice", "personal", "notes")
    assert row["key_configured"] is False

    assert _put_key(portal, "alice", "personal", "notes", "second").status_code == 204
    assert _delete_service(portal, "alice", "personal", "notes").status_code == 204
    assert _put_key(portal, "alice", "personal", "notes", "third").status_code == 404
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    row = _egress_row(portal, "alice", "personal", "notes")
    assert row["key_configured"] is False


def test_user_service_shares_a_key_gate_with_config_services(egress_portal: dict[str, Any]) -> None:
    """Keys of a shared agent's user service need management like its config services' keys."""
    portal = egress_portal
    assert _put_service(portal, "bob", "shared_dev", "notes").status_code == 204
    assert _put_key(portal, "alice", "shared_dev", "notes", "x").status_code == 403
    assert _put_key(portal, "bob", "shared_dev", "notes", "x").status_code == 204
    assert _listed_services(portal, "alice", "shared_dev")[-1]["key_configured"] is True


def test_user_service_can_connect_its_providers_account(oauth_egress_portal: dict[str, Any]) -> None:
    """A personal service whose OAuth provider is requester-scoped connects and disconnects like a config service."""
    portal = oauth_egress_portal
    assert _put_service(portal, "alice", "personal", "mygh", _MY_GITHUB).status_code == 204

    _connect_and_allow(portal, "alice", "personal", "mygh", "github")
    row = _egress_row(portal, "alice", "personal", "mygh")
    assert row["source"] == "user"
    assert (row["oauth"]["provider"], row["oauth"]["connected"], row["active_source"]) == ("github", True, "oauth")

    disconnect = _account_action(portal, "alice", "personal", "mygh", "disconnect")
    assert disconnect.status_code == 200, disconnect.text
    assert _egress_row(portal, "alice", "personal", "mygh")["oauth"]["connected"] is False
    # Bob has no such service, so the account routes do not exist for him.
    for action in ("connect", "disconnect"):
        response = _account_action(portal, "bob", "personal", "mygh", action)
        assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    ("rules", "refused"),
    [
        ([{"host": "api.notes.example.com", "auth": {"type": "bearer"}}], "api.notes.example.com"),
        ([{"host": "*.github.com", "auth": {"type": "bearer"}}], "*.github.com"),
        (
            [
                {"host": "api.github.com", "auth": {"type": "bearer"}},
                {"host": "github.com.notes.example.com", "auth": {"type": "bearer"}},
            ],
            "github.com.notes.example.com",
        ),
    ],
)
def test_a_user_service_cannot_send_its_account_off_its_providers_hosts(
    egress_portal: dict[str, Any],
    rules: list[dict[str, Any]],
    refused: str,
) -> None:
    """A connected account goes only to its provider's preset hosts; an API key may still go to any host."""
    portal = egress_portal
    body = {"description": "Notes", "rules": rules, "oauth_provider": "github"}

    response = _put_service(portal, "alice", "personal", "notes", body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "oauth_provider 'github' can only be used on api.github.com, uploads.github.com, github.com, "
        f"so these hosts cannot be used with it: {refused}"
    )
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404
    keyed = {**body, "oauth_provider": None}
    assert _put_service(portal, "alice", "personal", "notes", keyed).status_code == 204
    assert _put_service(portal, "alice", "personal", "plain", _NOTES).status_code == 204


def test_a_provider_without_a_preset_is_refused_for_user_services(egress_portal: dict[str, Any]) -> None:
    """A registered provider that no preset is bound to, such as google_cloud, cannot sign in a user service."""
    portal = egress_portal
    response = _put_service(portal, "alice", "personal", "notes", {**_NOTES, "oauth_provider": "google_cloud"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "oauth_provider 'google_cloud' has no built-in preset, so a service of your own cannot use it; use an API key"
    )
    assert _get_service(portal, "alice", "personal", "notes").status_code == 404


def test_a_stored_account_off_its_providers_hosts_is_neither_reported_nor_connectable(
    oauth_egress_portal: dict[str, Any],
) -> None:
    """A stored service that would send a connected account to another host loses the account when loaded."""
    portal = oauth_egress_portal
    _store_raw_entries(portal, {"mygh": _MY_GITHUB, "notes": {**_NOTES, "oauth_provider": "github"}})
    _connect_account(portal, "alice", "personal", "mygh", "github")

    notes = _egress_row(portal, "alice", "personal", "notes")
    assert (notes["oauth"], notes["configured"], notes["active_source"]) == (None, False, None)
    assert _egress_row(portal, "alice", "personal", "mygh")["oauth"]["connected"] is True
    assert _account_action(portal, "alice", "personal", "notes", "connect").status_code == 404


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
    notes = _egress_row(portal, "alice", "shared_dev", "notes")
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
    notes = _egress_row(portal, "alice", "shared_dev", "notes")
    assert notes["key_configured"] is False


def test_a_restrict_only_limit_is_saved_listed_and_never_takes_a_key(egress_portal: dict[str, Any]) -> None:
    """A manager limits a shared agent's GitHub in place: the limit saves without auth or key and narrows the config row."""
    portal = egress_portal
    client, admin = portal["client"], portal["headers"]["admin"]
    # The limit editor sends restrict_to_rules with restrict_only, which implies it anyway.
    limit = {
        "display_name": "GitHub (o/a)",
        "restrict_only": True,
        "restrict_to_rules": True,
        "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/a"}],
    }

    saved = _put_service(portal, "bob", "shared_dev", "github-limited", limit)
    assert saved.status_code == 204, saved.text
    assert _get_service(portal, "bob", "shared_dev", "github-limited").json() == limit
    rows = {service["name"]: service for service in _listed_services(portal, "alice", "shared_dev")}
    assert rows["github-limited"]["restrict_only"] is True
    assert rows["github-limited"]["configured"] is False
    assert rows["github"]["restrict_only"] is False
    panel = client.get("/api/egress-broker/services", params={"agent_name": "shared_dev"}, headers=admin)
    assert {s["name"]: s["restrict_only"] for s in panel.json()["services"]} == {
        "github": False,
        "openai": False,
        "github-limited": True,
    }

    # Neither page stores a key for it: the broker would never send one.
    personal_put = _put_key(portal, "bob", "shared_dev", "github-limited", "never-used")
    assert personal_put.status_code == 422
    assert personal_put.json()["detail"] == egress_status.RESTRICT_ONLY_KEY_REFUSAL
    admin_put = client.put(
        "/api/egress-broker/services/github-limited/secret?agent_name=shared_dev",
        json={"secret": "never-used"},
        headers=admin,
    )
    assert admin_put.status_code == 422
    assert admin_put.json()["detail"] == egress_status.RESTRICT_ONLY_KEY_REFUSAL
    assert _stored_key(portal, "github-limited", "shared_dev") is None

    # The scope's rules keep the config service's credential on the listed paths and refuse the rest.
    manager = get_runtime_credentials_manager(portal["paths"])
    config = Config.validate_with_runtime(portal["payload"], portal["paths"])
    target = _scope_target(portal, "shared_dev")
    rules = EgressRules(
        operator=config.egress_broker,
        effective=effective_config(config.egress_broker, manager, target),
    )
    allowed = route_request(rules, "api.github.com", 443, "/repos/o/a/pulls")
    assert allowed.match is not None
    assert allowed.match.service == "github"
    assert route_request(rules, "api.github.com", 443, "/repos/o/b").restricted_by == "github-limited"


def test_user_services_are_loaded_off_the_event_loop(
    oauth_egress_portal: dict[str, Any],
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """Reading a scope's services touches the credential store, so async routes do it in a thread."""
    portal = oauth_egress_portal
    assert _put_service(portal, "alice", "personal", "mygh", _MY_GITHUB).status_code == 204
    on_loop = calls_on_event_loop(egress_status, "effective_config")
    client, headers = portal["client"], portal["headers"]["alice"]
    _listing(portal, "alice")
    _state_services(portal, "alice", "personal")
    connect = client.post("/api/connections/egress/agents/personal/mygh/connect", headers=headers, json={})
    assert connect.status_code == 200, connect.text
    assert client.put(_service_url("personal", "second"), json=_NOTES, headers=headers).status_code == 204
    assert on_loop
    assert not any(on_loop)


# --- Inactive entries ---


def test_active_services_are_not_listed_as_inactive(egress_portal: dict[str, Any]) -> None:
    """Every agent object carries inactive_services, empty while all of the scope's entries are in use."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    for agent in ("personal", "shared_dev"):
        assert _listed_agents(portal, "alice")[agent]["inactive_services"] == []


def test_a_shadowed_entry_is_listed_with_its_reason_and_deleted_keeping_the_key(egress_portal: dict[str, Any]) -> None:
    """An entry an administrator's service now shadows is shown as inactive; deleting it leaves the config key."""
    portal = egress_portal
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert _put_key(portal, "alice", "personal", "notes", "k").status_code == 204
    portal["payload"]["egress_broker"]["services"]["notes"] = {
        "description": "Administrator notes",
        "rules": [{"host": "notes.admin.example.com", "auth": {"type": "bearer"}}],
    }
    _republish(portal)

    agent = _listed_agents(portal, "alice")["personal"]
    assert agent["inactive_services"] == [{"name": "notes", "reason": "shadowed"}]
    assert [(s["name"], s["source"]) for s in agent["services"] if s["name"] == "notes"] == [("notes", "config")]

    deleted = _delete_service(portal, "alice", "personal", "notes")
    assert deleted.status_code == 204, deleted.text
    assert _listed_agents(portal, "alice")["personal"]["inactive_services"] == []
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

    agent = _listed_agents(portal, "alice")["personal"]
    assert agent["inactive_services"] == [
        {"name": "odd", "reason": "invalid"},
        {"name": "stale", "reason": "invalid"},
    ]
    assert [s["name"] for s in agent["services"]] == ["github", "openai", "notes"]
    assert _get_service(portal, "alice", "personal", "stale").status_code == 404

    deleted = _delete_service(portal, "alice", "personal", "stale")
    assert deleted.status_code == 204, deleted.text
    assert _listed_agents(portal, "alice")["personal"]["inactive_services"] == [{"name": "odd", "reason": "invalid"}]
    assert _stored_key(portal, "stale") is None
    assert _delete_service(portal, "alice", "personal", "stale").status_code == 404
    assert _delete_service(portal, "alice", "personal", "odd").status_code == 204
    assert _get_service(portal, "alice", "personal", "notes").json() == _NOTES
    assert _listed_agents(portal, "alice")["personal"]["inactive_services"] == []


def test_inactive_entries_never_reach_another_requester(egress_portal: dict[str, Any]) -> None:
    """Inactive entries live in one requester's scope, so another requester's listing never names them."""
    portal = egress_portal
    _store_raw_entries(portal, {"stale": {"rules": []}})

    assert _listed_agents(portal, "alice")["personal"]["inactive_services"] == [{"name": "stale", "reason": "invalid"}]
    assert _listed_agents(portal, "bob")["personal"]["inactive_services"] == []
    assert _delete_service(portal, "bob", "personal", "stale").status_code == 404
    assert _listed_agents(portal, "alice")["personal"]["inactive_services"] == [{"name": "stale", "reason": "invalid"}]


def test_inactive_entries_of_a_shared_agent_are_listed_for_its_users_and_deleted_by_managers(
    egress_portal: dict[str, Any],
) -> None:
    """On a shared agent everyone sees the same entries, but only a manager may delete them."""
    portal = egress_portal
    _store_raw_entries(portal, {"stale": {"rules": []}}, agent="shared_dev", requester="@bob:example.org")

    for user in ("alice", "bob"):
        assert _listed_agents(portal, user)["shared_dev"]["inactive_services"] == [
            {"name": "stale", "reason": "invalid"},
        ]
    assert _delete_service(portal, "alice", "shared_dev", "stale").status_code == 403
    assert _delete_service(portal, "bob", "shared_dev", "stale").status_code == 204
    assert _listed_agents(portal, "alice")["shared_dev"]["inactive_services"] == []


def test_inactive_entries_count_toward_the_cap_until_deleted(egress_portal: dict[str, Any]) -> None:
    """A scope full of ignored entries cannot take another service, so the user must be able to delete them."""
    portal = egress_portal
    _store_raw_entries(portal, {f"stale-{index}": {"rules": []} for index in range(50)})

    refused = _put_service(portal, "alice", "personal", "notes")
    assert refused.status_code == 422, refused.text
    assert "at most 50" in refused.json()["detail"]

    assert _delete_service(portal, "alice", "personal", "stale-0").status_code == 204
    assert _put_service(portal, "alice", "personal", "notes").status_code == 204
    assert len(_listed_agents(portal, "alice")["personal"]["inactive_services"]) == 49


# --- Personal request log ---


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
    mixed_log: AuditLog,
) -> None:
    """Filtering by an agent that several users share returns only the caller's rows unless they manage it."""
    portal = egress_portal
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=shared_dev")) == ["/alice/shared"]
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=personal")) == [
        "/alice/personal/new",
        "/alice/personal/old",
    ]
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=no_such_agent")) == []
    assert _log_paths(_get_logs(portal, "admin", "?agent_name=no_such_agent")) == []
    # An empty filter, as a form sends it, means no filter.
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=&limit=2")) == ["/alice/personal/new", "/alice/shared"]
    # Carol is listed as a manager of shared_dev but may not use it, so like its keys its full log stays closed.
    _record_request(mixed_log, "@carol:example.org", "shared_dev", 7, path="/carol/shared")
    assert _log_paths(_get_logs(portal, "carol", "?agent_name=shared_dev")) == ["/carol/shared"]


def test_personal_log_shows_a_shared_agents_managers_every_requesters_rows(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,
) -> None:
    """A credential manager or administrator of a shared agent sees everyone's requests through it."""
    portal = egress_portal
    everyone = ["/nobody/shared", "/bob/shared", "/alice/shared"]
    bob = _get_logs(portal, "bob", "?agent_name=shared_dev")
    assert _log_paths(bob) == everyone
    assert [record["requester_id"] for record in bob.json()["records"]] == [
        None,
        "@bob:example.org",
        "@alice:example.org",
    ]
    assert _log_paths(_get_logs(portal, "admin", "?agent_name=shared_dev")) == everyone
    # Without an agent, or on their personal agent, a manager still sees only their own rows.
    assert _log_paths(_get_logs(portal, "bob")) == ["/bob/shared", "/bob/personal"]
    assert _log_paths(_get_logs(portal, "bob", "?agent_name=personal")) == ["/bob/personal"]
    assert _log_paths(_get_logs(portal, "admin")) == []
    # Rows the agent logged in another scope, before it was shared, stay with their requesters.
    _record_request(mixed_log, "@alice:example.org", "shared_dev", 7, path="/alice/private-era", scope="user_agent")
    assert _log_paths(_get_logs(portal, "bob", "?agent_name=shared_dev")) == everyone
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=shared_dev")) == ["/alice/private-era", "/alice/shared"]


def test_personal_log_full_view_is_per_agent_and_covers_unscoped_agents(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,
) -> None:
    """Managing one agent opens no other agent's log, and an unscoped agent's managers see all of its rows."""
    portal = egress_portal
    portal["payload"]["agents"]["team_ops"] = _agent_config(
        "Team Ops",
        "Unscoped team agent",
        users=("@alice:example.org", "@bob:example.org"),
        credential_managers=["@alice:example.org"],
    )
    _republish(portal)
    _record_request(mixed_log, "@alice:example.org", "team_ops", 7, path="/alice/ops", scope="unscoped")
    _record_request(mixed_log, "@bob:example.org", "team_ops", 8, path="/bob/ops", scope="unscoped")

    # Alice manages team_ops, so she sees Bob's rows there, but on shared_dev, which Bob manages, only her own.
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=team_ops")) == ["/bob/ops", "/alice/ops"]
    assert _log_paths(_get_logs(portal, "alice", "?agent_name=shared_dev")) == ["/alice/shared"]
    assert _log_paths(_get_logs(portal, "bob", "?agent_name=team_ops")) == ["/bob/ops"]
    assert _log_paths(_get_logs(portal, "bob", "?agent_name=shared_dev")) == [
        "/nobody/shared",
        "/bob/shared",
        "/alice/shared",
    ]


@pytest.mark.parametrize("agent_name", ["personal", "other_private"])
def test_personal_log_never_shows_other_requesters_rows_on_a_personal_agent(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,
    agent_name: str,
) -> None:
    """A personal agent's rows stay with their requester, even for an administrator, who may manage every agent."""
    portal = egress_portal
    _record_request(mixed_log, "@mallory:example.org", "other_private", 7, path="/mallory/private", scope="user")
    _record_request(mixed_log, "@admin:example.org", agent_name, 8, path="/admin/own")

    assert _log_paths(_get_logs(portal, "admin", f"?agent_name={agent_name}")) == ["/admin/own"]
    assert _log_paths(_get_logs(portal, "bob", "?agent_name=personal")) == ["/bob/personal"]


@pytest.mark.parametrize(
    "query",
    ["?requester_id=@bob:example.org", "?host=api.github.com", "?service=github", "?x=1"],
)
def test_personal_log_ignores_every_other_filter(
    egress_portal: dict[str, Any],
    mixed_log: AuditLog,  # noqa: ARG001
    query: str,
) -> None:
    """Filters that would select another requester, or anything the page does not offer, are refused."""
    response = _get_logs(egress_portal, "alice", query)
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
    _republish(portal)

    # Bob now acts as Alice: the rows logged for the canonical id are his, and the alias id has none of its own.
    assert _log_paths(_get_logs(portal, "bob")) == ["/alice/personal/new", "/alice/shared", "/alice/personal/old"]
    assert _log_paths(_get_logs(portal, "alice")) == _log_paths(_get_logs(portal, "bob"))
    assert _log_paths(_get_logs(portal, "carol")) == []


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


# --- Rule summaries and presets ---


def test_listing_summarizes_where_each_service_applies_and_how_it_sends_the_key(egress_portal: dict[str, Any]) -> None:
    """Rules show host, port, path prefix, auth type, and header or parameter name, in rule order; nothing else."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["services"]["drive"] = {"preset": "google_drive"}
    _republish(portal)
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

    assert rows["github"]["rules"] == [
        {"host": "api.github.com", "port": None, "path_prefix": "/", "auth_type": "bearer", "auth_name": None},
    ]
    assert rows["openai"]["rules"] == [
        {
            "host": "api.openai.com",
            "port": None,
            "path_prefix": "/",
            "auth_type": "header",
            "auth_name": "Authorization",
        },
    ]
    assert rows["drive"]["rules"] == [
        {
            "host": "www.googleapis.com",
            "port": None,
            "path_prefix": "/drive/",
            "auth_type": "bearer",
            "auth_name": None,
        },
        {
            "host": "www.googleapis.com",
            "port": None,
            "path_prefix": "/upload/drive/",
            "auth_type": "bearer",
            "auth_name": None,
        },
    ]
    # A header with a value template is a custom template; only its header name shows, never the template text.
    assert rows["own"]["rules"] == [
        {
            "host": "api.own.example.com",
            "port": 8443,
            "path_prefix": "/v1/",
            "auth_type": "template",
            "auth_name": "X-Own-Key",
        },
        {"host": "*.own.example.com", "port": None, "path_prefix": "/", "auth_type": "basic", "auth_name": None},
    ]
    listing = _listing(portal, "alice").text
    for hidden in ("Token-", "{secret}", "bot-user", "x-access-token"):
        assert hidden not in listing
    # Another requester's private services never show up in the summaries.
    assert "own" not in {service["name"] for service in _listed_services(portal, "bob", "personal")}
    state_rows = {row["name"]: row for row in _state_services(portal, "alice", "personal")}
    assert state_rows["own"]["rules"] == rows["own"]["rules"]


def test_query_auth_and_restrict_only_rules_are_summarized(egress_portal: dict[str, Any]) -> None:
    """A query rule names its parameter, even with a template; a restrict-only service's rules send nothing."""
    portal = egress_portal
    query = {
        "rules": [
            {"host": "api.query.example.com", "auth": {"type": "query", "name": "api_key"}},
            {"host": "v2.query.example.com", "auth": {"type": "query", "name": "key", "template": "k-{secret}"}},
        ],
    }
    limit = {
        "restrict_only": True,
        "rules": [
            {"host": "api.github.com", "path_prefix": "/repos/o/a"},
            {"host": "api.github.com", "path_prefix": "/repos/o/b", "auth": {"type": "header", "name": "X-Ignored"}},
        ],
    }
    assert _put_service(portal, "alice", "personal", "query", query).status_code == 204
    assert _put_service(portal, "alice", "personal", "github-limited", limit).status_code == 204

    rows = {service["name"]: service for service in _listed_services(portal, "alice", "personal")}

    assert [(rule["auth_type"], rule["auth_name"]) for rule in rows["query"]["rules"]] == [
        ("query", "api_key"),
        ("query", "key"),
    ]
    assert [(rule["auth_type"], rule["auth_name"]) for rule in rows["github-limited"]["rules"]] == [
        (None, None),
        (None, None),
    ]


def test_listing_names_the_placeholder_variables_each_service_sets(egress_portal: dict[str, Any]) -> None:
    """Each service lists its placeholder variables by name, preset ones included, so the editor can warn about a clash."""
    portal = egress_portal
    portal["payload"]["egress_broker"]["services"]["github_cli"] = {"preset": "github"}
    portal["payload"]["egress_broker"]["services"]["billing"] = {
        "description": "Billing API",
        "rules": [{"host": "billing.example.com", "auth": {"type": "bearer"}}],
        "placeholder_env": {"BILLING_API_KEY": "mindroom-brokered", "BILLING_TOKEN": "mindroom-brokered"},
    }
    _republish(portal)
    own = {**_NOTES, "placeholder_env": {"NOTES_API_KEY": "mindroom-brokered"}}
    assert _put_service(portal, "alice", "personal", "notes", own).status_code == 204

    rows = {service["name"]: service for service in _listed_services(portal, "alice", "personal")}

    assert rows["github"]["placeholder_names"] == []
    assert rows["github_cli"]["placeholder_names"] == ["GH_TOKEN", "GITHUB_TOKEN"]
    assert rows["billing"]["placeholder_names"] == ["BILLING_API_KEY", "BILLING_TOKEN"]
    assert rows["notes"]["placeholder_names"] == ["NOTES_API_KEY"]
    listing = _listing(portal, "alice").text
    assert "mindroom-brokered" not in listing
    # The portal's agent state carries the same names for its egress rows.
    state_rows = {row["name"]: row for row in _state_services(portal, "alice", "personal")}
    assert state_rows["billing"]["placeholder_names"] == ["BILLING_API_KEY", "BILLING_TOKEN"]
    # Another requester never sees the names of a service defined in alice's own scope.
    assert "notes" not in {service["name"] for service in _listed_services(portal, "bob", "personal")}


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
    # The account login select offers the registry's providers, the ones a saved service is checked against.
    providers = response.json()["oauth_providers"]
    assert {"id": "github", "display_name": "GitHub"} in providers
    assert {"google_drive", "google_gmail", "atlassian"} <= {provider["id"] for provider in providers}
    assert all(set(provider) == {"id", "display_name"} for provider in providers)


def test_presets_serve_the_user_service_validation_the_server_enforces(egress_portal: dict[str, Any]) -> None:
    """The validation the editor applies is the constants the server enforces, with no copy of them."""
    response = egress_portal["client"].get("/api/connections/egress/presets", headers=egress_portal["headers"]["alice"])

    assert response.status_code == 200, response.text
    assert response.json()["validation"] == {
        "max_rules_per_service": MAX_RULES_PER_SERVICE,
        "max_service_bytes": MAX_SERVICE_BYTES,
        "placeholder": {
            "name_pattern": PLACEHOLDER_NAME.pattern,
            # The word lists keep the order of the server's messages; the sets are sorted.
            "credential_words": list(PLACEHOLDER_NAME_WORDS),
            "refused_words": list(PLACEHOLDER_REFUSED_WORDS),
            "refused_prefix": PLACEHOLDER_REFUSED_PREFIX,
            "reserved_names": sorted(RESERVED_PLACEHOLDER_NAMES),
            "reserved_prefixes": sorted(RESERVED_PLACEHOLDER_PREFIXES),
            "value_pattern": PLACEHOLDER_VALUE.pattern,
            "value_max_length": PLACEHOLDER_VALUE_MAX_LENGTH,
        },
    }


def test_dashboard_presets_are_the_personal_presets(egress_portal: dict[str, Any]) -> None:
    """The dashboard editor gets the same presets, account logins, and validation as the personal page."""
    client, headers = egress_portal["client"], egress_portal["headers"]

    dashboard = client.get("/api/egress-broker/presets", headers=headers["admin"])
    personal = client.get("/api/connections/egress/presets", headers=headers["alice"])

    assert dashboard.status_code == 200, dashboard.text
    assert personal.status_code == 200, personal.text
    assert dashboard.json() == personal.json()


def test_served_placeholder_rules_give_the_answers_of_the_server_checks() -> None:
    """Each served placeholder rule refuses what the server refuses, so an editor that checks them agrees with a save."""
    placeholder = egress_status.presets_response({}).validation.placeholder

    def refused_by_the_config_model(name: str) -> bool:
        try:
            EgressService.model_validate({**_NOTES, "placeholder_env": {name: "mindroom-brokered"}})
        except ValidationError:
            return True
        return False

    assert placeholder.reserved_names
    assert placeholder.reserved_prefixes
    for name in placeholder.reserved_names:
        assert refused_by_the_config_model(name), name
    for prefix in placeholder.reserved_prefixes:
        assert refused_by_the_config_model(f"{prefix}X_TOKEN"), prefix
    assert not refused_by_the_config_model("MY_API_KEY")
    # The server saves `mindroom-brokered` and refuses `two words` (see the 422 cases above).
    value_pattern = re.compile(placeholder.value_pattern)
    assert value_pattern.fullmatch("mindroom-brokered")
    assert not value_pattern.fullmatch("two words")
    name_pattern = re.compile(placeholder.name_pattern)
    assert name_pattern.fullmatch("MY_API_KEY")
    assert not name_pattern.fullmatch("my_key")


def test_frontend_preset_fixture_matches_the_served_presets() -> None:
    """The preset fixture the UI tests load is exactly what the presets routes serve, so the two cannot drift."""
    fixture_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "test" / "fixtures" / "egressPresets.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    served = egress_status.presets_response({}).model_dump(mode="json")

    assert fixture["presets"] == served["presets"]
    assert fixture["validation"] == served["validation"]


def test_presets_need_a_signed_user_and_take_no_query(egress_portal: dict[str, Any]) -> None:
    """The preset list uses the personal auth gate: no identity, no list, and no stray query parameters."""
    client = egress_portal["client"]
    expected = client.get("/api/connections/egress").status_code
    assert expected in {401, 403}
    assert client.get("/api/connections/egress/presets").status_code == expected
    response = client.get("/api/connections/egress/presets?x=1", headers=egress_portal["headers"]["alice"])
    assert response.status_code == 400


# --- Response headers ---


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
