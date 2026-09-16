"""Connections API integration for an additional Google workspace plugin."""

from __future__ import annotations

import asyncio
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
from mindroom.oauth import reset as oauth_reset
from mindroom.oauth.registry import load_oauth_providers
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
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


def _workspace_plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "workspace_plugin"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "workspace-example",
                "tools_module": "tools.py",
                "oauth_module": "oauth.py",
            },
        ),
    )
    (plugin / "workspace.py").write_text(
        "from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig\n"
        "WORKSPACE = GoogleWorkspaceConfig(\n"
        "    name='secondary',\n"
        "    display_name='Secondary',\n"
        "    client_config_service='secondary_google_oauth_client',\n"
        "    allowed_hosted_domains=('secondary.example',),\n"
        "    services=('gmail', 'google_drive', 'google_calendar', 'google_docs', 'google_sheets'),\n"
        ")\n",
    )
    (plugin / "tools.py").write_text(
        "from mindroom.tool_system.google_workspaces import register_google_workspace_tools\n"
        "from .workspace import WORKSPACE\n"
        "register_google_workspace_tools(WORKSPACE)\n",
    )
    (plugin / "oauth.py").write_text(
        "from mindroom.tool_system.google_workspaces import google_workspace_oauth_providers\n"
        "from .workspace import WORKSPACE\n"
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    return google_workspace_oauth_providers(WORKSPACE)\n",
    )
    return plugin


@pytest.fixture
def workspace_portal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enforce_turn_authorization: None,  # noqa: ARG001
) -> dict[str, Any]:
    """Serve the real signed Connections API with a real workspace plugin."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    env = {
        **_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"),
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MINDROOM_PUBLIC_URL": "https://chat.example",
    }
    paths = _runtime_paths(tmp_path, env)
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials(
        "google_oauth_client",
        {"client_id": "primary-client", "client_secret": "primary-secret"},
    )
    manager.save_credentials(
        "secondary_google_oauth_client",
        {"client_id": "secondary-client", "client_secret": "secondary-secret"},
    )
    payload = {
        "administrators": ["@admin:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "plugins": [str(_workspace_plugin(tmp_path))],
        "agents": {
            "personal": {
                "display_name": "Personal Mind",
                "role": "Personal assistant",
                "tools": [
                    "gmail",
                    "google_drive",
                    "google_calendar",
                    "google_docs",
                    "google_sheets",
                    "secondary_gmail",
                    "secondary_google_drive",
                    "secondary_google_calendar",
                    "secondary_google_docs",
                    "secondary_google_sheets",
                ],
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
    providers = load_oauth_providers(config, paths, skip_broken_plugins=False)
    headers = {
        name: {
            "X-Trusted-User": name,
            "X-Trusted-Jwt": _trusted_upstream_jwt(
                key,
                user_id=name,
                email=f"{name}@example.org",
                matrix_user_id=f"@{name}:example.org",
            ),
            "Origin": "https://chat.example",
        }
        for name in ("alice", "bob")
    }
    return {
        "client": TestClient(main.app, base_url="https://chat.example"),
        "config": config,
        "headers": headers,
        "paths": paths,
        "providers": providers,
    }


def test_connections_catalog_shows_both_google_workspace_service_sets(workspace_portal: dict[str, Any]) -> None:
    """Each account has five labeled rows backed by independent service connections."""
    response = workspace_portal["client"].get(
        "/api/connections",
        headers=workspace_portal["headers"]["alice"],
    )

    assert response.status_code == 200, response.text
    agent = response.json()["agents"][0]
    services = {service["provider"]: service for service in agent["services"]}
    assert {provider: service["display_name"] for provider, service in services.items()} == {
        "google_gmail": "Gmail",
        "google_drive": "Google Drive",
        "google_calendar": "Google Calendar",
        "google_docs": "Google Docs",
        "google_sheets": "Google Sheets",
        "secondary_google_gmail": "Secondary Gmail",
        "secondary_google_drive": "Secondary Google Drive",
        "secondary_google_calendar": "Secondary Google Calendar",
        "secondary_google_docs": "Secondary Google Docs",
        "secondary_google_sheets": "Secondary Google Sheets",
    }
    tools = {tool["name"]: tool for tool in agent["tools"] if tool["provider"] is not None}
    assert len(tools) == 10
    for provider, service in services.items():
        assert len(service["tools"]) == 1
        tool = tools[service["tools"][0]]
        assert tool["provider"] == provider
        assert tool["display_name"] == service["display_name"]


@pytest.mark.parametrize(
    ("provider_id", "expected_client_id"),
    [
        ("google_gmail", "primary-client"),
        ("google_drive", "primary-client"),
        ("google_calendar", "primary-client"),
        ("google_docs", "primary-client"),
        ("google_sheets", "primary-client"),
        ("secondary_google_gmail", "secondary-client"),
        ("secondary_google_drive", "secondary-client"),
        ("secondary_google_calendar", "secondary-client"),
        ("secondary_google_docs", "secondary-client"),
        ("secondary_google_sheets", "secondary-client"),
    ],
)
def test_connect_uses_provider_specific_client_and_callback(
    workspace_portal: dict[str, Any],
    provider_id: str,
    expected_client_id: str,
) -> None:
    """Each Connections card must start its own client and callback flow."""
    response = workspace_portal["client"].post(
        f"/api/connections/agents/personal/{provider_id}/connect",
        headers=workspace_portal["headers"]["alice"],
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["provider"] == provider_id
    query = parse_qs(urlparse(response.json()["auth_url"]).query)
    assert query["client_id"] == [expected_client_id]
    assert query["redirect_uri"] == [f"https://chat.example/api/oauth/{provider_id}/callback"]


def test_additional_callback_and_reset_preserve_other_credentials(
    workspace_portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An additional callback stays separate, and another requester cannot reset it."""
    providers = workspace_portal["providers"]
    paths = workspace_portal["paths"]
    default_provider = providers["google_gmail"]
    secondary_provider = providers["secondary_google_gmail"]
    default_credentials = {
        "token": "primary-access",
        "refresh_token": "primary-refresh",
        "client_id": "primary-client",
        "token_uri": default_provider.token_url,
        "scopes": list(default_provider.scopes),
        "_source": "oauth",
        "_oauth_provider": default_provider.id,
        "_oauth_claims": {
            "sub": "primary-subject",
            "email": "alice@acme.example",
            "hd": "acme.example",
            "email_verified": True,
        },
        "_oauth_claims_verified": True,
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

    def token_response(request: httpx.Request) -> httpx.Response:
        token_requests.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "secondary-access",
                "refresh_token": "secondary-refresh",
                "id_token": "verified-id-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(secondary_provider.scopes),
            },
        )

    real_client = providers_module.AsyncOAuth2Client
    monkeypatch.setattr(
        providers_module,
        "AsyncOAuth2Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(token_response), **kwargs),
    )
    monkeypatch.setattr(
        "mindroom.oauth.google.google_id_token.verify_oauth2_token",
        lambda _token, _request, audience: {
            "sub": "secondary-subject",
            "email": "alice@secondary.example",
            "hd": "secondary.example",
            "email_verified": True,
            "aud": audience,
        },
    )

    connect = workspace_portal["client"].post(
        "/api/connections/agents/personal/secondary_google_gmail/connect",
        headers=workspace_portal["headers"]["alice"],
        json={},
    )
    state = parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]
    callback = workspace_portal["client"].get(
        "/api/oauth/secondary_google_gmail/callback",
        params={"code": "secondary-code", "state": state},
        headers=workspace_portal["headers"]["alice"],
        follow_redirects=False,
    )

    assert callback.status_code == 307, callback.text
    assert token_requests[0]["client_id"] == ["secondary-client"]
    assert token_requests[0]["redirect_uri"] == [
        "https://chat.example/api/oauth/secondary_google_gmail/callback",
    ]
    secondary_credentials = _stored_oauth_credentials(
        secondary_provider,
        paths,
        requester_id="@alice:example.org",
        agent_name="personal",
    )
    assert secondary_credentials is not None
    assert secondary_credentials["token"] == "secondary-access"  # noqa: S105
    assert (
        _stored_oauth_credentials(
            default_provider,
            paths,
            requester_id="@alice:example.org",
            agent_name="personal",
        )
        == default_before
    )

    reset_target = oauth_reset.resolve_oauth_reset_target(
        secondary_provider.id,
        agent_name="personal",
        config=workspace_portal["config"],
        runtime_paths=paths,
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="personal",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
    )
    reset_url = asyncio.run(oauth_reset.issue_browser_oauth_reset_url(reset_target))
    denied = workspace_portal["client"].post(
        reset_url,
        headers=workspace_portal["headers"]["bob"],
        follow_redirects=False,
    )

    assert denied.status_code == 403
    assert (
        _stored_oauth_credentials(
            secondary_provider,
            paths,
            requester_id="@alice:example.org",
            agent_name="personal",
        )
        == secondary_credentials
    )
    assert (
        _stored_oauth_credentials(
            default_provider,
            paths,
            requester_id="@alice:example.org",
            agent_name="personal",
        )
        == default_before
    )
