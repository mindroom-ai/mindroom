"""Connections API behavior for the default Atlassian connection and an additional plugin connection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from mindroom.api import main
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import providers as providers_module
from mindroom.oauth.registry import load_oauth_providers
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import (
    _publish_config,
    _publish_stored_oauth_credentials,
    _runtime_paths,
    _stored_oauth_credentials,
    _use_runtime_auth_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

_SITES = """
from mindroom.tool_system.atlassian_connections import (
    AtlassianConnectionConfig,
    atlassian_connection_oauth_provider,
    register_atlassian_connection_tools,
)

PARTNER = AtlassianConnectionConfig(
    name="partner",
    display_name="Partner Confluence",
    site_url="https://acme.atlassian.net",
    products=("confluence",),
)
register_atlassian_connection_tools(PARTNER)


def register_oauth_providers(settings, runtime_paths):
    return [atlassian_connection_oauth_provider(PARTNER)]
"""


def _sites_plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "atlassian_sites"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(
        json.dumps({"name": "atlassian-sites", "tools_module": "sites.py", "oauth_module": "sites.py"}),
    )
    (plugin / "sites.py").write_text(_SITES)
    return plugin


@pytest.fixture
def atlassian_portal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce_turn_authorization: None,  # noqa: ARG001
) -> dict[str, Any]:
    """Serve the real signed Connections API with both Atlassian connections."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    env = {
        **_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"),
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MINDROOM_PUBLIC_URL": "https://chat.example.com",
    }
    paths = _runtime_paths(tmp_path, env)
    get_runtime_credentials_manager(paths).save_credentials(
        "atlassian_oauth_client",
        {"client_id": "atlassian-client", "client_secret": "atlassian-secret"},
    )
    payload = {
        "administrators": ["@admin:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "plugins": [str(_sites_plugin(tmp_path))],
        "agents": {
            "personal": {
                "display_name": "Personal Mind",
                "role": "Personal assistant",
                "tools": ["atlassian", "partner_atlassian"],
                "private": {"per": "user_agent"},
                "access": {"users": ["@alice:example.org", "@bob:example.org"]},
            },
        },
    }
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    config = main._app_context(main.app).runtime_config
    assert config is not None
    headers = {
        name: {
            "X-Trusted-User": name,
            "X-Trusted-Jwt": _trusted_upstream_jwt(
                key,
                user_id=name,
                email=f"{name}@example.org",
                matrix_user_id=f"@{name}:example.org",
            ),
            "Origin": "https://chat.example.com",
        }
        for name in ("alice", "bob")
    }
    return {
        "client": TestClient(main.app, base_url="https://chat.example.com"),
        "headers": headers,
        "paths": paths,
        "providers": load_oauth_providers(config, paths, skip_broken_plugins=False),
    }


def test_connections_catalog_lists_each_atlassian_connection(atlassian_portal: dict[str, Any]) -> None:
    """Each connection is its own labeled service backed by its own tool."""
    response = atlassian_portal["client"].get("/api/connections", headers=atlassian_portal["headers"]["alice"])

    assert response.status_code == 200, response.text
    services = {service["provider"]: service for service in response.json()["agents"][0]["services"]}
    assert {provider: service["display_name"] for provider, service in services.items()} == {
        "atlassian": "Atlassian",
        "partner_atlassian": "Partner Confluence",
    }
    assert services["atlassian"]["tools"] == ["atlassian"]
    assert services["partner_atlassian"]["tools"] == ["partner_atlassian"]


@pytest.mark.parametrize(
    ("provider_id", "requests_jira"),
    [("atlassian", True), ("partner_atlassian", False)],
)
def test_connect_shares_the_app_but_not_the_callback_or_scopes(
    atlassian_portal: dict[str, Any],
    provider_id: str,
    requests_jira: bool,
) -> None:
    """Both connections authorize through the shared app with their own callback and scopes."""
    response = atlassian_portal["client"].post(
        f"/api/connections/agents/personal/{provider_id}/connect",
        headers=atlassian_portal["headers"]["alice"],
        json={},
    )

    assert response.status_code == 200, response.text
    query = parse_qs(urlparse(response.json()["auth_url"]).query)
    assert query["client_id"] == ["atlassian-client"]
    assert query["audience"] == ["api.atlassian.com"]
    assert query["redirect_uri"] == [f"https://chat.example.com/api/oauth/{provider_id}/callback"]
    assert ("read:jira-work" in query["scope"][0].split()) is requests_jira


def test_callback_stores_only_that_connection_for_that_requester(
    atlassian_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connecting the partner site leaves the default connection and other requesters untouched."""
    providers = atlassian_portal["providers"]
    paths = atlassian_portal["paths"]
    client = atlassian_portal["client"]
    headers = atlassian_portal["headers"]
    default_provider, partner_provider = providers["atlassian"], providers["partner_atlassian"]
    default_credentials = {
        "token": "default-access",
        "refresh_token": "default-refresh",
        "client_id": "atlassian-client",
        "scopes": list(default_provider.scopes),
        "expires_at": 4_102_444_800.0,
        "_source": "oauth",
        "_oauth_provider": default_provider.id,
    }
    _publish_stored_oauth_credentials(
        default_provider,
        paths,
        default_credentials,
        requester_id="@alice:example.org",
        agent_name="personal",
    )
    default_before = _stored_oauth_credentials(
        default_provider,
        paths,
        requester_id="@alice:example.org",
        agent_name="personal",
    )
    token_requests: list[dict[str, list[str]]] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        token_requests.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "partner-access",
                "refresh_token": "partner-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(partner_provider.scopes),
            },
        )

    real_client = providers_module.AsyncOAuth2Client
    monkeypatch.setattr(
        providers_module,
        "AsyncOAuth2Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(token_endpoint), **kwargs),
    )

    connect = client.post(
        "/api/connections/agents/personal/partner_atlassian/connect",
        headers=headers["alice"],
        json={},
    )
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        "/api/oauth/partner_atlassian/callback",
        params={"code": "partner-code", "state": state},
        headers=headers["alice"],
        follow_redirects=False,
    )

    assert callback.status_code == 307, callback.text
    assert token_requests[0]["redirect_uri"] == ["https://chat.example.com/api/oauth/partner_atlassian/callback"]
    partner_credentials = _stored_oauth_credentials(
        partner_provider,
        paths,
        requester_id="@alice:example.org",
        agent_name="personal",
    )
    assert partner_credentials is not None
    assert partner_credentials["token"] == "partner-access"  # noqa: S105
    assert (
        _stored_oauth_credentials(default_provider, paths, requester_id="@alice:example.org", agent_name="personal")
        == default_before
    )

    def connected(provider_id: str, requester: str) -> bool:
        response = client.get(f"/api/connections/agents/personal/{provider_id}/status", headers=headers[requester])
        assert response.status_code == 200, response.text
        return response.json()["connected"]

    assert connected("partner_atlassian", "alice") is True
    assert connected("atlassian", "alice") is True
    assert connected("partner_atlassian", "bob") is False

    disconnect = client.post(
        "/api/connections/agents/personal/partner_atlassian/disconnect",
        headers=headers["alice"],
        json={},
    )

    assert disconnect.status_code == 200, disconnect.text
    assert connected("partner_atlassian", "alice") is False
    assert (
        _stored_oauth_credentials(default_provider, paths, requester_id="@alice:example.org", agent_name="personal")
        == default_before
    )
