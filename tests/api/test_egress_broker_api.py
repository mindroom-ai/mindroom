"""Tests for egress broker admin API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import jwt
import pytest
import yaml
from fastapi import HTTPException
from fastapi.routing import APIRoute

from mindroom.api import egress_status, oauth
from mindroom.api.connection_agents import build_connection_agent_target
from mindroom.config.egress_broker import EgressService
from mindroom.constants import EXECUTION_ENV_TOOL_NAMES
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker import env, secrets
from mindroom.egress_broker.audit import AuditLog, AuditRecord
from mindroom.egress_broker.user_services import save_user_service
from mindroom.tool_system import sandbox_proxy
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_egress_credentials_api import (
    _finish_oauth,
    _oauth_block,
    _oauth_services,
    _use_fake_oauth_providers,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from pathlib import Path

    from fastapi.testclient import TestClient


# The broker runs exactly when its port is set; the API never starts it, so any port enables the routes.
_BROKER_ON = {"MINDROOM_EGRESS_BROKER_PORT": "8768"}


@pytest.fixture
def egress_config_file(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a config file with egress broker services."""
    config_dir = tmp_path / "egress-test"
    config_dir.mkdir()
    config_data = {
        "administrators": ["@owner:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "test_agent": {
                "display_name": "Test Agent",
                "role": "A test agent",
                "tools": ["shell", "egress_credentials"],
                "instructions": ["Test instruction"],
                "rooms": ["test_room"],
                "worker_scope": "shared",
            },
        },
        "defaults": {"markdown": True},
        "egress_broker": {
            "unmatched_hosts": "passthrough",
            "services": {
                "github": {
                    "display_name": "GitHub",
                    "description": "GitHub API and git over HTTPS",
                    "rules": [
                        {
                            "host": "api.github.com",
                            "auth": {"type": "bearer"},
                        },
                        {
                            "host": "github.com",
                            "auth": {"type": "basic", "username": "x-access-token"},
                        },
                    ],
                    "placeholder_env": {
                        "GH_TOKEN": "mindroom-brokered",
                        "GITHUB_TOKEN": "mindroom-brokered",
                    },
                },
            },
        },
    }
    temp_path = config_dir / "config.yaml"
    temp_path.write_text(yaml.dump(config_data), encoding="utf-8")
    yield temp_path
    temp_path.unlink(missing_ok=True)


def _admin_client(egress_config_file: Path, *, broker_on: bool = True, **env: str) -> TestClient:
    """Serve the admin API for the config file, with the broker enabled unless `broker_on` is false, and `env` added."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=egress_config_file,
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org", **(_BROKER_ON if broker_on else {}), **env},
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)
    return TestClient(main.app, base_url="http://localhost")


@pytest.fixture
def broker_test_client(egress_config_file: Path) -> TestClient:
    """Create a test client with egress broker configuration."""
    return _admin_client(egress_config_file)


@pytest.fixture
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuditLog]:
    """Serve the admin log from a real audit log, as if the broker were running."""
    log = AuditLog(tmp_path / "audit.sqlite3")
    monkeypatch.setattr("mindroom.api.egress_broker.active_audit_log", lambda: log)
    yield log
    log.close()


def _scope_params(agent_name: str | None) -> dict[str, str]:
    """Return the query parameters that select an agent's scope; none selects the global one."""
    return {} if agent_name is None else {"agent_name": agent_name}


def _service(client: TestClient, name: str, agent_name: str | None = None) -> dict[str, Any]:
    response = client.get("/api/egress-broker/services", params=_scope_params(agent_name))
    assert response.status_code == 200, response.text
    return next(service for service in response.json()["services"] if service["name"] == name)


def _audit_record(**fields: Any) -> AuditRecord:  # noqa: ANN401
    """Return a request record from an agent on GitHub, with `fields` overriding any default."""
    defaults: dict[str, Any] = {
        "at": datetime.now(UTC),
        "kind": "request",
        "scope": "shared",
        "agent_name": "test_agent",
        "requester_id": "@user:example.org",
        "method": "GET",
        "host": "api.github.com",
        "path": "/repos",
        "service": "github",
        "status": 200,
        "bytes_up": 1,
        "bytes_down": 1,
        "duration_ms": 1,
    }
    return AuditRecord(**{**defaults, **fields})


def test_services_status_never_returns_secret(broker_test_client: TestClient) -> None:
    """Service status must never leak secret values."""
    # Set a secret for a service
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": "s3cret"},
    )
    assert response.status_code == 204

    # Get services status
    response = broker_test_client.get("/api/egress-broker/services")
    assert response.status_code == 200

    # Check that "s3cret" never appears in the response
    response_text = response.text
    assert "s3cret" not in response_text
    assert "secret" not in response_text.lower() or "configured" in response_text.lower()


def test_put_unknown_service_404(broker_test_client: TestClient) -> None:
    """PUT to unknown service should return 404."""
    response = broker_test_client.put(
        "/api/egress-broker/services/unknown_service/secret",
        json={"secret": "test"},
    )
    assert response.status_code == 404


def test_put_blank_secret_422(broker_test_client: TestClient) -> None:
    """PUT with blank secret should return 422."""
    # Empty string
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": ""},
    )
    assert response.status_code == 422

    # Whitespace only
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": "   "},
    )
    assert response.status_code == 422


def test_put_secret_with_unknown_field_422(broker_test_client: TestClient) -> None:
    """PUT with a field beyond `secret` is refused like the personal route does, not silently accepted."""
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": "s3cret", "unexpected": "value"},
    )
    assert response.status_code == 422


def test_logs_and_ca_409_when_inactive(broker_test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Logs and CA endpoints should return 409 when broker is not running."""
    monkeypatch.setattr("mindroom.api.egress_broker.active_audit_log", lambda: None)
    monkeypatch.setattr("mindroom.api.egress_broker.active_ca_pem", lambda: None)

    # Logs should return 409
    response = broker_test_client.get("/api/egress-broker/logs")
    assert response.status_code == 409
    assert "not running" in response.json()["detail"]

    # CA should return 409
    response = broker_test_client.get("/api/egress-broker/ca.pem")
    assert response.status_code == 409
    assert "not running" in response.json()["detail"]


def test_services_listing_summarizes_rules_with_how_they_send_the_key(broker_test_client: TestClient) -> None:
    """The dashboard sees where each service applies and how it sends the key, never a basic username."""
    response = broker_test_client.get("/api/egress-broker/services")

    assert response.status_code == 200, response.text
    github = next(service for service in response.json()["services"] if service["name"] == "github")
    assert github["rules"] == [
        {"host": "api.github.com", "port": None, "path_prefix": "/", "auth_type": "bearer", "auth_name": None},
        {"host": "github.com", "port": None, "path_prefix": "/", "auth_type": "basic", "auth_name": None},
    ]
    assert "x-access-token" not in response.text


def _client_with_agents(
    egress_config_file: Path,
    agents: dict[str, dict[str, Any]],
    **defaults: Any,  # noqa: ANN401
) -> TestClient:
    """Serve the admin API for the config file with `agents` in place of its agents and `defaults` added."""
    config_data = yaml.safe_load(egress_config_file.read_text(encoding="utf-8"))
    config_data["agents"] = agents
    config_data["defaults"].update(defaults)
    egress_config_file.write_text(yaml.dump(config_data), encoding="utf-8")
    return _admin_client(egress_config_file)


def _agent(tools: list[str], **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return an agent with exactly `tools`, none inherited from the defaults."""
    return {"display_name": "Agent", "role": "", "tools": tools, "include_default_tools": False, **extra}


def _keys_agent(tools: list[str], **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return an agent with exactly `tools` and the `egress_credentials` tool, which opts it into Agent Keys."""
    return _agent([*tools, "egress_credentials"], **extra)


def _key_targets(client: TestClient, agent_name: str | None = None) -> tuple[list[str], bool]:
    response = client.get("/api/egress-broker/services", params=_scope_params(agent_name))
    assert response.status_code == 200, response.text
    return response.json()["shared_agents"], response.json()["has_unscoped_agents"]


def test_services_listing_names_the_agents_whose_keys_the_dashboard_sets(egress_config_file: Path) -> None:
    """The listing names the shared agents that reach the broker by their resolved tools, a preset's included.

    Agents whose keys belong to each requester (`user`, `user_agent`, private) are left out, and so is an agent
    without a command tool or without the `egress_credentials` tool. Every scope the panel selects gets the same answer.
    """
    client = _client_with_agents(
        egress_config_file,
        {
            "preset_team": _keys_agent(["openclaw_compat"], worker_scope="shared"),
            "calculator_team": _keys_agent(["calculator"], worker_scope="shared"),
            "shell_team": _agent(["shell"], worker_scope="shared"),
            "per_user": _keys_agent(["shell"], worker_scope="user"),
            "per_agent": _keys_agent(["python"], worker_scope="user_agent"),
            "mine": _keys_agent(["shell"], private={"per": "user_agent"}),
            "notes": _keys_agent(["calculator"]),
            "coder": _agent(["shell"]),
        },
    )

    assert _key_targets(client) == (["preset_team"], False)
    assert _key_targets(client, "preset_team") == (["preset_team"], False)


def test_services_listing_names_agents_by_the_tools_that_get_the_broker_environment(
    egress_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent with `egress_credentials` uses the broker when it has any tool the sandbox proxy gives the broker env.

    Both read one list, so a tool added there makes its agents eligible without a second edit.
    """
    assert env.EXECUTION_ENV_TOOL_NAMES is sandbox_proxy.EXECUTION_ENV_TOOL_NAMES
    tools = sorted({*EXECUTION_ENV_TOOL_NAMES, "calculator"})
    monkeypatch.setattr(env, "EXECUTION_ENV_TOOL_NAMES", frozenset(tools))
    agents = {f"{tool}_team": _keys_agent([tool], worker_scope="shared") for tool in tools}

    assert _key_targets(_client_with_agents(egress_config_file, agents)) == (sorted(agents), False)


@pytest.mark.parametrize(
    ("agents", "has_personal_agents"),
    [
        ({"per_user": _keys_agent(["shell"], worker_scope="user")}, True),
        ({"per_agent": _keys_agent(["python"], worker_scope="user_agent")}, True),
        ({"mine": _keys_agent(["shell"], private={"per": "user_agent"})}, True),
        (
            {
                "team": _keys_agent(["shell"], worker_scope="shared"),
                "notes": _keys_agent(["calculator"], worker_scope="user"),
            },
            False,
        ),
        ({"coder": _keys_agent(["shell"])}, False),
        ({"per_user": _agent(["shell"], worker_scope="user")}, False),
    ],
    ids=["user", "user_agent", "private", "shared-and-calculator", "unscoped", "user-without-egress-tool"],
)
def test_services_listing_says_whether_an_agent_with_per_user_keys_reaches_the_broker(
    egress_config_file: Path,
    agents: dict[str, dict[str, Any]],
    *,
    has_personal_agents: bool,
) -> None:
    """The listing never names those agents (`user`, `user_agent`, private), but says whether one uses the broker."""
    response = _client_with_agents(egress_config_file, agents).get("/api/egress-broker/services")

    assert response.status_code == 200, response.text
    assert response.json()["has_personal_agents"] is has_personal_agents


def test_services_listing_says_whether_the_personal_page_is_served(
    egress_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The page where each user sets per-user keys is served while users can sign in from Chat or a signed upstream.

    With sign-in from Chat off, trusted upstream auth without JWT does not serve it: the personal API answers 403
    without a verified identity.
    """
    from tests.api.conftest import trusted_upstream_headers  # noqa: PLC0415

    trusted_env = {
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
        "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
        "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
    }
    private_key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(private_key))
    signed_headers = {
        "X-Trusted-User": "owner",
        "X-Trusted-Email": "owner@example.com",
        "X-Trusted-Jwt": _trusted_upstream_jwt(
            private_key,
            email="owner@example.com",
            matrix_user_id="@owner:example.org",
            user_id="owner",
        ),
    }

    no_chat = {"MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "[]"}
    standalone = _admin_client(egress_config_file).get("/api/egress-broker/services")
    no_sign_in = _admin_client(egress_config_file, **no_chat).get("/api/egress-broker/services")
    unsigned = _admin_client(egress_config_file, **trusted_env, **no_chat).get(
        "/api/egress-broker/services",
        headers=trusted_upstream_headers(matrix_user_id="@owner:example.org"),
    )
    signed = _admin_client(
        egress_config_file,
        **_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"),
        **no_chat,
    ).get("/api/egress-broker/services", headers=signed_headers)

    for response in (standalone, no_sign_in, unsigned, signed):
        assert response.status_code == 200, response.text
    assert standalone.json()["personal_page_served"] is True
    assert no_sign_in.json()["personal_page_served"] is False
    assert unsigned.json()["personal_page_served"] is False
    assert signed.json()["personal_page_served"] is True


def test_services_listing_says_whether_an_agent_without_a_scope_reaches_the_broker(egress_config_file: Path) -> None:
    """All agents without a scope share the global keys, so one of them reaching the broker is enough."""
    client = _client_with_agents(egress_config_file, {"coder": _keys_agent(["openclaw_compat"]), "notes": _agent([])})

    assert _key_targets(client) == ([], True)


def test_services_listing_follows_the_default_worker_scope(egress_config_file: Path) -> None:
    """An agent without a worker scope of its own takes the default one, which decides where its keys live."""
    agents = {"inherits": _keys_agent(["shell"]), "team": _keys_agent(["shell"], worker_scope="shared")}

    assert _key_targets(_client_with_agents(egress_config_file, agents, worker_scope="user_agent")) == (["team"], False)
    assert _key_targets(_client_with_agents(egress_config_file, agents, worker_scope="shared")) == (
        ["inherits", "team"],
        False,
    )


def test_routes_require_dashboard_auth() -> None:
    """Every egress-broker route, presets included, is mounted with the dashboard's `verify_user`."""
    from mindroom.api import main  # noqa: PLC0415
    from mindroom.api.auth import verify_user  # noqa: PLC0415

    routes = [route for route in main.app.routes if getattr(route, "path", "").startswith("/api/egress-broker/")]
    assert "/api/egress-broker/presets" in {route.path for route in routes}  # type: ignore[attr-defined]
    for route in routes:
        dependencies = route.dependant.dependencies  # type: ignore[attr-defined]
        assert any(dependency.call is verify_user for dependency in dependencies), route.path  # type: ignore[attr-defined]


def test_every_route_is_a_404_while_the_broker_is_off(
    egress_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a broker port every admin route, presets and the CA included, is a 404 before it reads any store."""
    from mindroom.api import main  # noqa: PLC0415

    client = _admin_client(egress_config_file, broker_on=False)
    scopes_resolved: list[object] = []
    monkeypatch.setattr(
        "mindroom.api.egress_broker.resolve_request_credentials_target",
        lambda *args, **_kwargs: scopes_resolved.append(args),
    )
    routes = sorted(
        (method, route.path)
        for route in main.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/egress-broker/")
        for method in route.methods
    )
    assert {("GET", "/api/egress-broker/presets"), ("GET", "/api/egress-broker/ca.pem")} <= set(routes)

    for method, path in routes:
        response = client.request(method, path.format(name="github"))
        assert (response.status_code, response.json()) == (404, {"detail": "Agent Keys are not enabled"}), path
    assert scopes_resolved == []


@pytest.mark.parametrize("broker_on", [False, True])
def test_tool_catalog_offers_the_egress_tool_only_while_the_broker_is_on(
    egress_config_file: Path,
    *,
    broker_on: bool,
) -> None:
    """The dashboard's tool picker lists the Egress Credentials tool only while the broker is on."""
    response = _admin_client(egress_config_file, broker_on=broker_on).get("/api/tools/")

    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["tools"]}
    assert "shell" in names
    assert ("egress_credentials" in names) is broker_on


def test_logs_return_the_refusal_code(broker_test_client: TestClient, audit_log: AuditLog) -> None:
    """The administrator's log shows why a request was refused, and null for one that was forwarded."""
    audit_log.record(_audit_record(at=datetime(2026, 10, 1, tzinfo=UTC), path="/ok"))
    audit_log.record(
        _audit_record(
            at=datetime(2026, 10, 2, tzinfo=UTC),
            kind="denied",
            path="/nope",
            status=403,
            code="path_not_allowed",
        ),
    )

    response = broker_test_client.get("/api/egress-broker/logs")

    assert response.status_code == 200, response.text
    assert [(row["path"], row["code"]) for row in response.json()["records"]] == [
        ("/nope", "path_not_allowed"),
        ("/ok", None),
    ]


@pytest.mark.parametrize("agent_name", [None, "test_agent"], ids=["global", "agent-scoped"])
def test_delete_secret_flips_status(broker_test_client: TestClient, agent_name: str | None) -> None:
    """DELETE should flip configured status to false, for global and agent-scoped secrets."""
    params = _scope_params(agent_name)
    # Set a secret
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        params=params,
        json={"secret": "test-secret"},
    )
    assert response.status_code == 204

    # Check it's configured, in that scope only
    assert _service(broker_test_client, "github", agent_name)["configured"] is True
    if agent_name is not None:
        assert _service(broker_test_client, "github")["configured"] is False

    # Delete it
    response = broker_test_client.delete("/api/egress-broker/services/github/secret", params=params)
    assert response.status_code == 204

    # Check it's no longer configured
    assert _service(broker_test_client, "github", agent_name)["configured"] is False


def test_logs_filtering_with_real_audit_log(broker_test_client: TestClient, audit_log: AuditLog) -> None:
    """Test /logs endpoint with real AuditLog and filtering."""
    audit_log.record(_audit_record(bytes_up=100, bytes_down=500, duration_ms=50))
    audit_log.record(
        _audit_record(
            agent_name="other_agent",
            method="POST",
            host="api.openai.com",
            path="/chat/completions",
            service="openai",
            bytes_up=200,
            bytes_down=1000,
            duration_ms=100,
        ),
    )

    # Test filtering by agent_name
    response = broker_test_client.get("/api/egress-broker/logs?agent_name=test_agent")
    assert response.status_code == 200
    records = response.json()["records"]
    assert len(records) == 1
    assert records[0]["agent_name"] == "test_agent"
    assert records[0]["host"] == "api.github.com"

    # Test filtering by host
    response = broker_test_client.get("/api/egress-broker/logs?host=api.openai.com")
    assert response.status_code == 200
    records = response.json()["records"]
    assert len(records) == 1
    assert records[0]["host"] == "api.openai.com"

    # Test filtering by service
    response = broker_test_client.get("/api/egress-broker/logs?service=github")
    assert response.status_code == 200
    records = response.json()["records"]
    assert len(records) == 1
    assert records[0]["service"] == "github"

    # Test limit
    response = broker_test_client.get("/api/egress-broker/logs?limit=1")
    assert response.status_code == 200
    records = response.json()["records"]
    assert len(records) == 1


def test_admin_logs_carry_requester_and_filter_by_it(broker_test_client: TestClient, audit_log: AuditLog) -> None:
    """Each row names the requester whose worker token made it, and the filter matches that ID exactly."""
    for requester_id, path in (
        ("@alice:example.org", "/a"),
        ("@bob:example.org", "/b"),
        ("@alice:example.org", "/c"),
        (None, "/d"),
    ):
        audit_log.record(_audit_record(requester_id=requester_id, path=path))

    everyone = broker_test_client.get("/api/egress-broker/logs").json()["records"]
    alice = broker_test_client.get("/api/egress-broker/logs", params={"requester": "@alice:example.org"})
    nobody = broker_test_client.get("/api/egress-broker/logs", params={"requester": "@ali"})
    both = broker_test_client.get(
        "/api/egress-broker/logs",
        params={"requester": "@alice:example.org", "agent_name": "test_agent", "host": "api.github.com"},
    )

    assert sorted((row["path"], row["requester_id"]) for row in everyone) == [
        ("/a", "@alice:example.org"),
        ("/b", "@bob:example.org"),
        ("/c", "@alice:example.org"),
        ("/d", None),
    ]
    assert alice.status_code == 200, alice.text
    assert [(row["path"], row["requester_id"]) for row in alice.json()["records"]] == [
        ("/c", "@alice:example.org"),
        ("/a", "@alice:example.org"),
    ]
    assert nobody.json()["records"] == []
    assert [row["path"] for row in both.json()["records"]] == ["/c", "/a"]


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_credentials_list_omits_egress_secrets(broker_test_client: TestClient, agent_name: str | None) -> None:
    """The Credentials tab lists only services its status route serves, so egress secrets stay out."""
    params = _scope_params(agent_name)
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        params=params,
        json={"secret": "s3cret"},
    )
    assert response.status_code == 204
    response = broker_test_client.post(
        "/api/credentials/openai/api-key",
        params=params,
        json={"service": "openai", "api_key": "sk-test"},
    )
    assert response.status_code == 200

    response = broker_test_client.get("/api/credentials/list", params=params)
    assert response.status_code == 200
    services = response.json()
    assert "openai" in services
    assert "egress_github" not in services
    for service in services:
        status = broker_test_client.get(f"/api/credentials/{service}/status", params=params)
        assert status.status_code == 200, service


def _save_own_service(name: str, agent_name: str | None = None) -> None:
    """Store a service in a scope's own store, the way the personal services route does."""
    from mindroom.api import main  # noqa: PLC0415

    config = main._app_context(main.app).runtime_config
    paths = main._app_runtime_paths(main.app)
    manager = get_runtime_credentials_manager(paths)
    target = None
    if agent_name is not None:
        target = build_connection_agent_target(config, paths, "@owner:example.org", agent_name)
    service = EgressService.model_validate({"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]})
    save_user_service(
        manager,
        target,
        name,
        service,
        config_services=config.egress_broker.services,
        oauth_providers=(),
    )


def _listed(client: TestClient, agent_name: str | None = None) -> list[tuple[str, str, bool]]:
    params = _scope_params(agent_name)
    response = client.get("/api/egress-broker/services", params=params)
    assert response.status_code == 200, response.text
    return [(s["name"], s["source"], s["configured"]) for s in response.json()["services"]]


def test_dashboard_lists_a_scopes_own_services_and_accepts_their_keys(broker_test_client: TestClient) -> None:
    """The panel shows each scope's own services after the config's, read-only, and takes keys for them."""
    client = broker_test_client
    assert _listed(client) == [("github", "config", False)]
    _save_own_service("everyone")
    _save_own_service("team", "test_agent")
    assert _listed(client) == [("github", "config", False), ("everyone", "user", False)]
    assert _listed(client, "test_agent") == [("github", "config", False), ("team", "user", False)]

    put = client.put("/api/egress-broker/services/everyone/secret", json={"secret": "k"})
    assert put.status_code == 204, put.text
    put = client.put("/api/egress-broker/services/team/secret?agent_name=test_agent", json={"secret": "k"})
    assert put.status_code == 204, put.text
    assert _listed(client) == [("github", "config", False), ("everyone", "user", True)]
    assert _listed(client, "test_agent") == [("github", "config", False), ("team", "user", True)]

    # A name another scope owns is unknown here, for writes and deletes alike.
    assert client.put("/api/egress-broker/services/team/secret", json={"secret": "k"}).status_code == 404
    assert client.delete("/api/egress-broker/services/team/secret").status_code == 404
    assert (
        client.put(
            "/api/egress-broker/services/everyone/secret?agent_name=test_agent",
            json={"secret": "k"},
        ).status_code
        == 404
    )

    assert client.delete("/api/egress-broker/services/everyone/secret").status_code == 204
    assert client.delete("/api/egress-broker/services/team/secret?agent_name=test_agent").status_code == 204
    assert _listed(client) == [("github", "config", False), ("everyone", "user", False)]
    assert _listed(client, "test_agent") == [("github", "config", False), ("team", "user", False)]


def test_dashboard_reads_a_scopes_own_services_off_the_event_loop(
    broker_test_client: TestClient,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """The async listing reads a scope's own services from the credential store in a thread."""
    _save_own_service("everyone")
    on_loop = calls_on_event_loop(egress_status, "effective_config")
    assert _listed(broker_test_client) == [("github", "config", False), ("everyone", "user", False)]
    assert on_loop
    assert not any(on_loop)


@pytest.fixture
def oauth_broker_client(egress_config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Serve the admin API with services backed by an agent-scoped and a requester-scoped OAuth provider."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    config = yaml.safe_load(egress_config_file.read_text(encoding="utf-8"))
    config["egress_broker"]["services"].update(_oauth_services())
    egress_config_file.write_text(yaml.dump(config), encoding="utf-8")
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=egress_config_file,
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org", **_BROKER_ON},
    )
    _use_fake_oauth_providers(monkeypatch, runtime_paths)
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)
    return TestClient(main.app, base_url="http://localhost")


def _connect(client: TestClient, name: str, provider: str, agent_name: str | None = None) -> None:
    """Run the whole OAuth flow through the admin connect route and the shared callback."""
    response = client.post(f"/api/egress-broker/services/{name}/connect", params=_scope_params(agent_name))
    _finish_oauth(client, response, provider)


def test_services_status_has_key_and_oauth_parts(oauth_broker_client: TestClient) -> None:
    """Each service reports its key and OAuth sources; plain services carry no OAuth block."""
    github = _service(oauth_broker_client, "github")
    assert github["oauth"] is None
    assert github["active_source"] is None
    assert github["key_configured"] is False
    drive = _service(oauth_broker_client, "drive")
    assert drive["oauth"] == _oauth_block()
    assert _service(oauth_broker_client, "ghost")["oauth"] is None


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_connect_stores_the_account_and_a_key_takes_precedence(
    oauth_broker_client: TestClient,
    agent_name: str | None,
) -> None:
    """A connected account shows in the scope it was connected for, and an explicit key takes over."""
    params = _scope_params(agent_name)
    _connect(oauth_broker_client, "drive", "google_drive", agent_name)

    drive = _service(oauth_broker_client, "drive", agent_name)
    assert drive["active_source"] == "oauth"
    assert drive["configured"] is True
    assert drive["key_configured"] is False
    assert drive["key_updated_at"] is None
    assert drive["oauth"]["connected"] is True
    assert drive["oauth"]["account_label"] == "alice@example.com"
    if agent_name is not None:
        assert _service(oauth_broker_client, "drive")["oauth"]["connected"] is False

    response = oauth_broker_client.put("/api/egress-broker/services/drive/secret", params=params, json={"secret": "k"})
    assert response.status_code == 204
    drive = _service(oauth_broker_client, "drive", agent_name)
    assert drive["active_source"] == "key"
    assert drive["key_configured"] is True
    assert drive["key_updated_at"] is not None
    assert "updated_at" not in drive
    assert drive["oauth"]["connected"] is True


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_disconnect_clears_the_connection(oauth_broker_client: TestClient, agent_name: str | None) -> None:
    """Disconnect resets the scope's connection."""
    params = _scope_params(agent_name)
    _connect(oauth_broker_client, "drive", "google_drive", agent_name)

    response = oauth_broker_client.post("/api/egress-broker/services/drive/disconnect", params=params)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "disconnected", "provider": "google_drive"}
    drive = _service(oauth_broker_client, "drive", agent_name)
    assert drive["oauth"]["connected"] is False
    assert drive["active_source"] is None


def _opt_in_to_shared_workers(egress_config_file: Path) -> None:
    """Let the requester-scoped `gh` service use connected accounts on shared and unscoped workers."""
    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    config = yaml.safe_load(egress_config_file.read_text(encoding="utf-8"))
    config["egress_broker"]["services"]["gh"]["oauth_on_shared_workers"] = True
    egress_config_file.write_text(yaml.dump(config), encoding="utf-8")
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)


def test_requester_scoped_provider_is_unavailable_on_the_dashboard_scopes_without_the_opt_in(
    oauth_broker_client: TestClient,
) -> None:
    """The panel's scopes are shared or unscoped workers, where the broker never uses a GitHub-style account."""
    for scope in (None, "test_agent"):
        gh = _service(oauth_broker_client, "gh", scope)
        assert gh["oauth"]["unavailable_reason"] == "shared_sandbox"
        assert (gh["oauth"]["connected"], gh["oauth"]["can_connect"]) == (False, False)
        assert gh["active_source"] is None
        assert _service(oauth_broker_client, "drive", scope)["oauth"]["unavailable_reason"] is None


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_requester_scoped_provider_status_follows_the_dashboard_requester(
    oauth_broker_client: TestClient,
    egress_config_file: Path,
    agent_name: str | None,
) -> None:
    """With the opt-in, a GitHub-style connection belongs to the dashboard user, so the panel shows it everywhere."""
    _opt_in_to_shared_workers(egress_config_file)
    _connect(oauth_broker_client, "gh", "github", agent_name)

    for scope in (None, "test_agent"):
        gh = _service(oauth_broker_client, "gh", scope)
        assert gh["oauth"]["connected"] is True
        assert gh["oauth"]["shared_worker_opt_in"] is True
        assert gh["active_source"] == "oauth"

    response = oauth_broker_client.post(
        "/api/egress-broker/services/gh/disconnect",
        params=_scope_params(agent_name),
    )
    assert response.status_code == 200, response.text
    assert _service(oauth_broker_client, "gh", agent_name)["oauth"]["connected"] is False


@pytest.mark.parametrize("action", ["connect", "disconnect"])
@pytest.mark.parametrize("name", ["unknown", "github", "ghost"])
def test_connect_and_disconnect_404_without_a_usable_provider(
    oauth_broker_client: TestClient,
    action: str,
    name: str,
) -> None:
    """Unknown services, services without an OAuth provider, and unknown providers are 404."""
    response = oauth_broker_client.post(f"/api/egress-broker/services/{name}/{action}")
    assert response.status_code == 404, response.text


def test_connect_and_disconnect_reject_unknown_agents(oauth_broker_client: TestClient) -> None:
    """The agent selector is checked by the same dashboard scope resolution as the key routes."""
    for action in ("connect", "disconnect"):
        response = oauth_broker_client.post(
            f"/api/egress-broker/services/drive/{action}",
            params={"agent_name": "no_such_agent"},
        )
        assert response.status_code in {400, 404}, response.text


def _enable_service_account() -> None:
    from dataclasses import replace  # noqa: PLC0415

    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    paths = main._app_runtime_paths(main.app)
    paths = replace(paths, process_env={**paths.process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"})
    main.initialize_api_app(main.app, paths)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)


def test_service_account_provider_cannot_be_connected(oauth_broker_client: TestClient) -> None:
    """A shared Google service account is runtime configuration, never a personal account."""
    _enable_service_account()

    response = oauth_broker_client.post("/api/egress-broker/services/drive/connect")
    assert response.status_code == 409, response.text
    drive = _service(oauth_broker_client, "drive")
    assert drive["oauth"]["service_account"] is True
    assert drive["oauth"]["connected"] is False
    assert drive["oauth"]["can_connect"] is False
    assert drive["active_source"] is None
    assert drive["configured"] is False


def test_personal_connection_stored_before_a_service_account_can_still_be_disconnected(
    oauth_broker_client: TestClient,
) -> None:
    """The broker keeps injecting a stored personal token, so the operator must be able to revoke it."""
    _connect(oauth_broker_client, "drive", "google_drive")
    _enable_service_account()
    assert _service(oauth_broker_client, "drive")["active_source"] == "oauth"

    response = oauth_broker_client.post("/api/egress-broker/services/drive/disconnect")
    assert response.status_code == 200, response.text
    drive = _service(oauth_broker_client, "drive")
    assert drive["oauth"]["connected"] is False
    assert drive["active_source"] is None


@pytest.mark.parametrize(
    "failure",
    [HTTPException(503, "internal-client-secret"), RuntimeError("internal-client-secret")],
)
def test_an_unreadable_connection_state_degrades_one_service_without_failing_the_panel(
    oauth_broker_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Whatever fails while reading one provider's state, that service shows as not connectable and the rest stay."""
    real = oauth.authenticated_connection_status

    async def fails_for_drive(provider_id: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if provider_id == "google_drive":
            raise failure
        return await real(provider_id, *args, **kwargs)

    monkeypatch.setattr(oauth, "authenticated_connection_status", fails_for_drive)
    response = oauth_broker_client.get("/api/egress-broker/services")
    assert response.status_code == 200, response.text
    assert "internal-client-secret" not in response.text
    drive = _service(oauth_broker_client, "drive")
    assert drive["oauth"]["connected"] is False
    assert drive["oauth"]["can_connect"] is False
    assert drive["oauth"]["unavailable_reason"] is None
    assert _service(oauth_broker_client, "gh")["oauth"]["unavailable_reason"] == "shared_sandbox"
    assert _service(oauth_broker_client, "github")["oauth"] is None


@pytest.mark.parametrize("action", ["connect", "disconnect"])
@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_connect_and_disconnect_are_refused_in_a_scope_the_broker_refuses_the_account_for(
    oauth_broker_client: TestClient,
    egress_config_file: Path,
    action: str,
    agent_name: str | None,
) -> None:
    """The panel offers nothing there, so the routes answer 409 with a readable string until the service opts in."""
    params = _scope_params(agent_name)
    url = f"/api/egress-broker/services/gh/{action}"

    refused = oauth_broker_client.post(url, params=params)
    assert refused.status_code == 409, refused.text
    assert isinstance(refused.json()["detail"], str)
    assert "share" in refused.json()["detail"]
    drive = oauth_broker_client.post(f"/api/egress-broker/services/drive/{action}", params=params)
    assert drive.status_code == 200, drive.text

    _opt_in_to_shared_workers(egress_config_file)
    allowed = oauth_broker_client.post(url, params=params)
    assert allowed.status_code == 200, allowed.text


def test_an_unreadable_worker_backend_degrades_one_service_without_failing_the_panel(
    oauth_broker_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared-sandbox check can fail on its own, and that costs only the service that needs it."""
    real = egress_status.shared_worker_oauth

    def broken(provider: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if provider.requester_scoped_credentials:
            msg = "internal-backend-detail"
            raise RuntimeError(msg)
        return real(provider, *args, **kwargs)

    monkeypatch.setattr(egress_status, "shared_worker_oauth", broken)
    response = oauth_broker_client.get("/api/egress-broker/services")

    assert response.status_code == 200, response.text
    assert "internal-backend-detail" not in response.text
    gh = _service(oauth_broker_client, "gh")["oauth"]
    assert (gh["connected"], gh["can_connect"], gh["unavailable_reason"]) == (False, False, None)
    assert _service(oauth_broker_client, "drive")["oauth"]["can_connect"] is True
    assert _service(oauth_broker_client, "github")["oauth"] is None


def test_listing_never_refreshes_tokens(oauth_broker_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel reads stored state, so a stalled provider token endpoint cannot stall it."""
    _connect(oauth_broker_client, "drive", "google_drive")

    def refresh_must_not_run(*_args: object, **_kwargs: object) -> None:
        msg = "A listing must not refresh tokens"
        raise AssertionError(msg)

    monkeypatch.setattr(oauth, "refresh_oauth_credentials", refresh_must_not_run)
    assert _service(oauth_broker_client, "drive")["oauth"]["connected"] is True


def test_listing_authenticates_once_not_per_service(
    oauth_broker_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router's dependency authenticates the request; the per-provider status does not repeat it."""
    calls: list[str] = []
    real = oauth.verify_user

    async def counted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append("verify")
        return await real(*args, **kwargs)

    monkeypatch.setattr(oauth, "verify_user", counted)
    assert oauth_broker_client.get("/api/egress-broker/services").status_code == 200
    assert calls == []
    assert oauth_broker_client.get("/api/oauth/google_drive/status").status_code == 200
    assert calls == ["verify"]


def test_key_status_is_read_off_the_event_loop(
    oauth_broker_client: TestClient,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """Reading a key's status decrypts a credential file, so the async listing runs it in a thread."""
    on_loop = calls_on_event_loop(secrets, "load_egress_document")
    assert oauth_broker_client.get("/api/egress-broker/services").status_code == 200
    assert on_loop
    assert not any(on_loop)
