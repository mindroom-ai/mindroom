"""Tests for built-in Google OAuth provider definitions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.oauth.google import (
    _google_oauth_provider,
    _google_runtime_bootstrapper,
    _google_token_parser,
)
from mindroom.oauth.google_calendar import _GOOGLE_CALENDAR_OAUTH_SCOPES, google_calendar_oauth_provider
from mindroom.oauth.google_drive import _GOOGLE_DRIVE_OAUTH_SCOPES, google_drive_oauth_provider
from mindroom.oauth.google_gmail import _GOOGLE_GMAIL_OAUTH_SCOPES, google_gmail_oauth_provider
from mindroom.oauth.google_sheets import _GOOGLE_SHEETS_OAUTH_SCOPES, google_sheets_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProviderError, oauth_connection_required_payload
from mindroom.oauth.service import build_oauth_connect_instruction, build_oauth_reconnect_instruction

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
PROVISIONED_CLIENT_ID = "provisioned-client.apps.googleusercontent.com"
PROVISIONED_CLIENT_SECRET = "provisioned-client-secret"  # noqa: S105
GOOGLE_EXTRA_AUTH_PARAMS = {
    "access_type": "offline",
    "include_granted_scopes": "true",
    "prompt": "consent",
}


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            google_calendar_oauth_provider(),
            {
                "id": "google_calendar",
                "display_name": "Google Calendar",
                "scopes": _GOOGLE_CALENDAR_OAUTH_SCOPES,
                "credential_service": "google_calendar_oauth",
                "tool_config_service": "google_calendar",
                "client_config_services": ("google_calendar_oauth_client",),
                "status_capabilities": ("Calendar event read/write",),
            },
        ),
        (
            google_drive_oauth_provider(),
            {
                "id": "google_drive",
                "display_name": "Google Drive",
                "scopes": _GOOGLE_DRIVE_OAUTH_SCOPES,
                "credential_service": "google_drive_oauth",
                "tool_config_service": "google_drive",
                "client_config_services": ("google_drive_oauth_client",),
                "status_capabilities": ("Drive file search", "Drive file read"),
            },
        ),
        (
            google_gmail_oauth_provider(),
            {
                "id": "google_gmail",
                "display_name": "Gmail",
                "scopes": _GOOGLE_GMAIL_OAUTH_SCOPES,
                "credential_service": "google_gmail_oauth",
                "tool_config_service": "gmail",
                "client_config_services": ("google_gmail_oauth_client",),
                "status_capabilities": ("Gmail read/modify/compose",),
            },
        ),
        (
            google_sheets_oauth_provider(),
            {
                "id": "google_sheets",
                "display_name": "Google Sheets",
                "scopes": _GOOGLE_SHEETS_OAUTH_SCOPES,
                "credential_service": "google_sheets_oauth",
                "tool_config_service": "google_sheets",
                "client_config_services": ("google_sheets_oauth_client",),
                "status_capabilities": ("Sheets read/write",),
            },
        ),
    ],
)
def test_public_google_oauth_providers_preserve_service_specific_fields(
    provider: OAuthProvider,
    expected: dict[str, object],
) -> None:
    """Public Google provider factories preserve service-specific metadata."""
    assert provider.id == expected["id"]
    assert provider.display_name == expected["display_name"]
    assert provider.scopes == expected["scopes"]
    assert provider.credential_service == expected["credential_service"]
    assert provider.tool_config_service == expected["tool_config_service"]
    assert provider.client_config_services == expected["client_config_services"]
    assert provider.status_capabilities == expected["status_capabilities"]


@pytest.mark.parametrize(
    "provider",
    [
        google_calendar_oauth_provider(),
        google_drive_oauth_provider(),
        google_gmail_oauth_provider(),
        google_sheets_oauth_provider(),
    ],
)
def test_public_google_oauth_providers_preserve_shared_google_oauth_fields(provider: OAuthProvider) -> None:
    """Public Google provider factories preserve shared Google OAuth metadata."""
    provider_prefix = provider.id.upper()

    assert provider.authorization_url == GOOGLE_AUTHORIZATION_URL
    assert provider.token_url == GOOGLE_TOKEN_URL
    assert provider.shared_client_config_services == ("google_oauth_client",)
    assert provider.allowed_email_domains_env == (
        f"{provider_prefix}_ALLOWED_EMAIL_DOMAINS",
        f"MINDROOM_OAUTH_{provider_prefix}_ALLOWED_EMAIL_DOMAINS",
    )
    assert provider.allowed_hosted_domains_env == (
        f"{provider_prefix}_ALLOWED_HOSTED_DOMAINS",
        f"MINDROOM_OAUTH_{provider_prefix}_ALLOWED_HOSTED_DOMAINS",
    )
    assert provider.extra_auth_params == GOOGLE_EXTRA_AUTH_PARAMS
    assert provider.pkce_code_challenge_method == "S256"
    assert provider.runtime_bootstrapper is _google_runtime_bootstrapper
    assert provider.token_parser is _google_token_parser


def test_google_oauth_provider_helper_builds_common_google_provider_skeleton() -> None:
    """The private Google helper builds the shared provider skeleton."""
    provider = _google_oauth_provider(
        provider_id="google_example",
        display_name="Google Example",
        scopes=("openid", "https://www.googleapis.com/auth/example"),
        credential_service="google_example_oauth",
        tool_config_service="google_example",
        client_config_services=("google_example_oauth_client",),
        status_capabilities=("Example read/write",),
    )

    assert provider.id == "google_example"
    assert provider.display_name == "Google Example"
    assert provider.authorization_url == GOOGLE_AUTHORIZATION_URL
    assert provider.token_url == GOOGLE_TOKEN_URL
    assert provider.scopes == ("openid", "https://www.googleapis.com/auth/example")
    assert provider.credential_service == "google_example_oauth"
    assert provider.tool_config_service == "google_example"
    assert provider.client_config_services == ("google_example_oauth_client",)
    assert provider.shared_client_config_services == ("google_oauth_client",)
    assert provider.allowed_email_domains_env == (
        "GOOGLE_EXAMPLE_ALLOWED_EMAIL_DOMAINS",
        "MINDROOM_OAUTH_GOOGLE_EXAMPLE_ALLOWED_EMAIL_DOMAINS",
    )
    assert provider.allowed_hosted_domains_env == (
        "GOOGLE_EXAMPLE_ALLOWED_HOSTED_DOMAINS",
        "MINDROOM_OAUTH_GOOGLE_EXAMPLE_ALLOWED_HOSTED_DOMAINS",
    )
    assert provider.extra_auth_params == GOOGLE_EXTRA_AUTH_PARAMS
    assert provider.pkce_code_challenge_method == "S256"
    assert provider.runtime_bootstrapper is _google_runtime_bootstrapper
    assert provider.status_capabilities == ("Example read/write",)
    assert provider.token_parser is _google_token_parser


def _install_provisioning_transport(
    monkeypatch: pytest.MonkeyPatch,
    provisioning_url: str = "https://provisioning.example",
) -> list[httpx.Request]:
    """Install a provisioning transport that validates paired-client authentication."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == f"{provisioning_url}/v1/local-mindroom/oauth/google-client"
        assert request.headers["X-Local-MindRoom-Client-Id"] == "local-client"
        assert request.headers["X-Local-MindRoom-Client-Secret"] == "local-secret"
        return httpx.Response(
            200,
            json={
                "client_id": PROVISIONED_CLIENT_ID,
                "client_secret": PROVISIONED_CLIENT_SECRET,
            },
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("mindroom.oauth.google.httpx.AsyncClient", client_factory)
    return requests


def _paired_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_PROVISIONING_URL": "https://provisioning.example",
            "MINDROOM_LOCAL_CLIENT_ID": "local-client",
            "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret",
        },
    )


def test_google_oauth_provider_bootstraps_client_for_paired_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paired runtime fetches and stores the desktop client outside the package."""
    _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    provider = google_drive_oauth_provider()

    resolution = asyncio.run(provider.client_config_resolution_async(runtime_paths))

    assert resolution is not None
    assert resolution.custom is False
    assert resolution.service == "google_oauth_client"
    assert resolution.config.client_id == PROVISIONED_CLIENT_ID
    assert resolution.config.client_secret == PROVISIONED_CLIENT_SECRET
    assert resolution.config.redirect_uri == "http://localhost:8765/api/oauth/google_drive/callback"


def test_google_oauth_provider_requires_pairing_or_custom_client_on_fresh_install(tmp_path: Path) -> None:
    """An unpaired runtime fails before sending users through a broken public-client flow."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    provider = google_drive_oauth_provider()

    resolution = provider.client_config_resolution(runtime_paths)

    assert resolution is None


@pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1", "[::1]"])
def test_google_oauth_provider_allows_http_provisioning_only_on_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostname: str,
) -> None:
    """Local development may use HTTP without exposing pairing credentials remotely."""
    _install_provisioning_transport(monkeypatch, f"http://{hostname}")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_PROVISIONING_URL": f"http://{hostname}",
            "MINDROOM_LOCAL_CLIENT_ID": "local-client",
            "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret",
        },
    )

    resolution = asyncio.run(google_drive_oauth_provider().client_config_resolution_async(runtime_paths))

    assert resolution is not None


def test_google_oauth_provider_rejects_remote_http_provisioning(tmp_path: Path) -> None:
    """Pairing credentials must never be sent to a plaintext remote endpoint."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_PROVISIONING_URL": "http://provisioning.example",
            "MINDROOM_LOCAL_CLIENT_ID": "local-client",
            "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret",
        },
    )

    with pytest.raises(OAuthProviderError, match="must use HTTPS"):
        asyncio.run(google_drive_oauth_provider().client_config_resolution_async(runtime_paths))


def test_google_oauth_provider_bootstrapped_client_authorization_uses_pkce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provisioned desktop client sends an S256 challenge and the local callback."""
    requests = _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    provider = google_gmail_oauth_provider()
    code_verifier = provider.issue_pkce_code_verifier()
    assert code_verifier is not None

    auth_url = asyncio.run(
        provider.authorization_uri_async(
            runtime_paths,
            state="test-state",
            code_verifier=code_verifier,
        ),
    )
    params = parse_qs(urlparse(auth_url).query)

    assert params["client_id"] == [PROVISIONED_CLIENT_ID]
    assert params["redirect_uri"] == ["http://localhost:8765/api/oauth/google_gmail/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["test-state"]
    assert len(requests) == 1


def test_oauth_connection_required_payload_preserves_structured_fields() -> None:
    """OAuth-required tool payloads keep the established public field names."""
    exc = OAuthConnectionRequired(
        "Google Drive is not connected for this agent.",
        provider_id="google_drive",
        connect_url="/api/oauth/google_drive/connect?agent_name=general",
    )

    assert oauth_connection_required_payload(exc) == {
        "error": "Google Drive is not connected for this agent.",
        "oauth_connection_required": True,
        "provider": "google_drive",
        "connect_url": "/api/oauth/google_drive/connect?agent_name=general",
    }


def test_oauth_connection_required_payload_marks_loopback_links() -> None:
    """Agents and clients can distinguish links that must open on the host."""
    exc = OAuthConnectionRequired(
        "Google Drive is not connected for this agent.",
        provider_id="google_drive",
        connect_url="http://localhost:8765/api/oauth/google_drive/authorize?connect_token=opaque",
    )

    assert oauth_connection_required_payload(exc)["requires_host_browser"] is True


def test_build_oauth_connect_instruction_formats_shared_message() -> None:
    """OAuth connection instructions should have one shared service formatter."""
    assert build_oauth_connect_instruction(
        google_drive_oauth_provider(),
        "/api/oauth/google_drive/connect?agent_name=general",
    ) == (
        "Google Drive is not connected for this agent. "
        "Open this MindRoom link to connect it, then retry the request: "
        "/api/oauth/google_drive/connect?agent_name=general"
    )


def test_build_oauth_connect_instruction_explains_loopback_device_handoff() -> None:
    """Loopback links tell mobile users which computer must open the URL."""
    connect_url = "http://localhost:8765/api/oauth/google_drive/authorize?connect_token=opaque"

    assert build_oauth_connect_instruction(google_drive_oauth_provider(), connect_url) == (
        "Google Drive is not connected for this agent. "
        "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
        "not on a phone or another computer. If needed, open this conversation there or copy the complete "
        "link into that browser. After connecting, retry the request: "
        f"{connect_url}"
    )


def test_build_oauth_reconnect_instruction_explains_loopback_device_handoff() -> None:
    """Expired loopback sessions retain the same device-handoff guidance."""
    connect_url = "http://127.0.0.1:8765/api/oauth/google_drive/authorize?connect_token=opaque"

    assert build_oauth_reconnect_instruction(google_drive_oauth_provider(), connect_url) == (
        "Google Drive session for this agent expired or is no longer valid. "
        "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
        "not on a phone or another computer. If needed, open this conversation there or copy the complete "
        "link into that browser. After reconnecting, retry the request: "
        f"{connect_url}"
    )
