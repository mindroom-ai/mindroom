"""Tests for built-in Google OAuth provider definitions."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import manager as credentials_module
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.google import (
    _GOOGLE_IDENTITY_SCOPES,
    _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY,
    _GOOGLE_PROVISIONED_CLIENT_TTL_SECONDS,
    _google_runtime_bootstrapper,
    _google_token_parser,
    google_oauth_provider,
)
from mindroom.oauth.providers import (
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
    OAuthClientConfig,
    OAuthConnectionRequired,
    OAuthProviderError,
    is_terminal_oauth_refresh_error_code,
    oauth_connection_required_payload,
)
from mindroom.oauth.service import build_oauth_connect_instruction, build_oauth_reconnect_instruction
from tests.oauth_test_utils import oauth_authorization_url

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
PROVISIONED_CLIENT_ID = "provisioned-client.apps.googleusercontent.com"
PROVISIONED_CLIENT_SECRET = "provisioned-client-secret"  # noqa: S105
GOOGLE_EXTRA_AUTH_PARAMS = {
    "access_type": "offline",
    "include_granted_scopes": "true",
    "prompt": "consent",
}
GOOGLE_NARROW_EXTRA_AUTH_PARAMS = {
    "access_type": "offline",
    "prompt": "consent",
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("invalid_grant", True),
        (" Invalid_Refresh_Token ", True),
        ("bad_refresh_token", True),
        ("temporarily_unavailable", False),
        (None, False),
    ],
)
def test_terminal_oauth_refresh_error_classification(value: object, expected: bool) -> None:
    """All refresh paths should share one normalized terminal-code policy."""
    assert is_terminal_oauth_refresh_error_code(value) is expected


_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.settings.readonly",
)


@pytest.mark.parametrize(
    (
        "provider_id",
        "display_name",
        "tool_config_service",
        "service_scopes",
        "status_capabilities",
        "include_granted_scopes",
    ),
    [
        (
            "google_calendar",
            "Google Calendar",
            "google_calendar",
            _CALENDAR_SCOPES,
            ("Calendar event read/write",),
            True,
        ),
        (
            "google_cloud",
            "Google Cloud",
            None,
            ("https://www.googleapis.com/auth/cloud-platform.read-only",),
            ("Read-only Google Cloud API access",),
            False,
        ),
        (
            "google_docs",
            "Google Docs",
            "google_docs",
            ("https://www.googleapis.com/auth/documents",),
            ("Docs create and read", "Docs text editing"),
            False,
        ),
        (
            "google_drive",
            "Google Drive",
            "google_drive",
            ("https://www.googleapis.com/auth/drive",),
            ("Drive file search and read", "Drive file upload and organization"),
            True,
        ),
        (
            "google_gmail",
            "Gmail",
            "gmail",
            ("https://www.googleapis.com/auth/gmail.modify",),
            ("Gmail read, send, draft, and mailbox management",),
            True,
        ),
        (
            "google_sheets",
            "Google Sheets",
            "google_sheets",
            ("https://www.googleapis.com/auth/spreadsheets",),
            ("Sheets read/write",),
            True,
        ),
        (
            "google_tasks",
            "Google Tasks",
            "google_tasks",
            ("https://www.googleapis.com/auth/tasks",),
            ("Tasks read/write",),
            False,
        ),
    ],
)
def test_each_google_provider_declares_its_service_on_the_shared_google_contract(
    provider_id: str,
    display_name: str,
    tool_config_service: str | None,
    service_scopes: tuple[str, ...],
    status_capabilities: tuple[str, ...],
    include_granted_scopes: bool,
) -> None:
    """Every built-in Google provider keeps its service-specific metadata and the shared Google OAuth metadata."""
    provider = google_oauth_provider(provider_id)
    provider_prefix = provider_id.upper()

    assert (provider.id, provider.display_name) == (provider_id, display_name)
    assert provider.scopes == (*_GOOGLE_IDENTITY_SCOPES, *service_scopes)
    assert provider.credential_service == f"{provider_id}_oauth"
    assert provider.tool_config_service == tool_config_service
    assert provider.client_config_services == (f"{provider_id}_oauth_client",)
    assert provider.status_capabilities == status_capabilities
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
    assert provider.extra_auth_params == (
        GOOGLE_EXTRA_AUTH_PARAMS if include_granted_scopes else GOOGLE_NARROW_EXTRA_AUTH_PARAMS
    )
    assert provider.pkce_code_challenge_method == "S256"
    assert provider.runtime_bootstrapper is _google_runtime_bootstrapper
    assert provider.token_parser is _google_token_parser


def test_a_blank_exported_domain_restriction_does_not_hide_the_env_file_one(tmp_path: Path) -> None:
    """A blank export, as a compose passthrough of an unset variable produces, must not lift a `.env` restriction."""
    (tmp_path / ".env").write_text(
        "GOOGLE_DRIVE_ALLOWED_HOSTED_DOMAINS=example.com\nGOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS=example.org\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"GOOGLE_DRIVE_ALLOWED_HOSTED_DOMAINS": "", "GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS": " "},
    )
    provider = google_oauth_provider("google_drive")

    assert provider.resolved_allowed_hosted_domains(runtime_paths) == ("example.com",)
    assert provider.resolved_allowed_email_domains(runtime_paths) == ("example.org",)


@pytest.mark.parametrize("scope_field", ["scopes", "scope"])
@pytest.mark.parametrize(
    ("requested_scope", "response_scope", "expected_scope"),
    [
        (None, None, None),
        (
            None,
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ),
        ("openid", None, "openid"),
        (
            "openid",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ),
    ],
)
def test_calendar_refresh_preserves_grant_without_requesting_new_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_scope: str | None,
    response_scope: str | None,
    expected_scope: str | None,
    scope_field: str,
) -> None:
    """Refreshing an older broad grant must not request newly configured scopes."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "google_oauth_client",
        {"client_id": "client-id", "client_secret": PROVISIONED_CLIENT_SECRET},
    )
    provider = replace(
        google_oauth_provider("google_calendar"),
        extra_token_params={"scope": requested_scope} if requested_scope is not None else {},
    )
    granted_scopes = [*_GOOGLE_IDENTITY_SCOPES, "https://www.googleapis.com/auth/calendar"]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: dict[str, object] = {"access_token": "new-access-token", "expires_in": 3600}
        if response_scope is not None:
            response["scope"] = response_scope
        return httpx.Response(200, json=response)

    def client_factory(**kwargs: object) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", client_factory)
    refreshed = asyncio.run(
        provider.refresh_token_data(
            {
                "token": "old-access-token",
                "refresh_token": "refresh-token",
                "token_uri": GOOGLE_TOKEN_URL,
                "client_id": "client-id",
                "expires_at": 1.0,
                scope_field: granted_scopes if scope_field == "scopes" else " ".join(granted_scopes),
                "_oauth_claims": {"email": "alice@example.test", "email_verified": True, "sub": "subject-1"},
                "_oauth_claims_verified": True,
            },
            runtime_paths,
        ),
    )

    assert len(requests) == 1
    assert requests[0].url == GOOGLE_TOKEN_URL
    body = parse_qs(requests[0].content.decode(), keep_blank_values=True)
    assert body["grant_type"] == ["refresh_token"]
    if requested_scope is None:
        assert "scope" not in body
    else:
        assert body["scope"] == [requested_scope]
    assert refreshed is not None
    assert refreshed["refresh_token"] == "refresh-token"  # noqa: S105
    assert refreshed["scopes"] == (granted_scopes if expected_scope is None else expected_scope.split())


def test_google_exchange_defaults_to_requested_scopes_when_response_omits_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial authorization retains configured scopes when Google omits scope."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "google_oauth_client",
        {"client_id": "client-id", "client_secret": PROVISIONED_CLIENT_SECRET},
    )
    provider = google_oauth_provider("google_calendar")

    def client_factory(**kwargs: object) -> AsyncOAuth2Client:
        assert kwargs["scope"] == provider.scopes
        return AsyncOAuth2Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"access_token": "access-token", "id_token": "identity-token"},
                ),
            ),
            **kwargs,
        )

    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", client_factory)
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *_args: {"email": "alice@example.test", "email_verified": True, "sub": "subject-1"},
    )

    result = asyncio.run(
        provider.exchange_code(
            "auth-code",
            runtime_paths,
            token_url=provider.token_url,
            code_verifier="pkce-verifier",
        ),
    )

    assert result.token_data["scopes"] == list(provider.scopes)


def test_google_token_parser_bounds_identity_certificate_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity verification must not outlive the credential transaction lock budget."""
    captured_timeouts: list[object] = []

    class _Request:
        def __call__(
            self,
            _url: str,
            *,
            method: str = "GET",
            timeout: float = 120.0,
        ) -> object:
            assert method == "GET"
            captured_timeouts.append(timeout)
            return object()

    def verify_token(
        _id_token: str,
        request: Callable[..., object],
        _audience: str,
    ) -> dict[str, object]:
        request("https://www.googleapis.com/oauth2/v1/certs", method="GET")
        return {"sub": "subject-1", "email": "alice@example.com", "email_verified": True}

    monkeypatch.setattr("google.auth.transport.requests.Request", _Request)
    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", verify_token)
    provider = google_oauth_provider("google_drive")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    result = _google_token_parser(
        provider,
        {"access_token": "access-token", "id_token": "identity-token"},
        OAuthClientConfig(
            client_id="client-id",
            client_secret=PROVISIONED_CLIENT_SECRET,
            redirect_uri="http://localhost:8765/api/oauth/google_drive/callback",
        ),
        runtime_paths,
    )

    assert result.claims["sub"] == "subject-1"
    assert captured_timeouts == [20.0]


def _install_provisioning_transport(
    monkeypatch: pytest.MonkeyPatch,
    provisioning_url: str = "https://provisioning.example",
    *,
    client_id: str = PROVISIONED_CLIENT_ID,
    client_secret: str = PROVISIONED_CLIENT_SECRET,
    status_code: int = 200,
) -> list[httpx.Request]:
    """Install a provisioning transport that validates paired-client authentication."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == f"{provisioning_url}/v1/local-mindroom/oauth/google-client"
        assert request.headers["X-Local-MindRoom-Client-Id"] == "local-client"
        assert request.headers["X-Local-MindRoom-Client-Secret"] == "local-secret"
        return httpx.Response(
            status_code,
            json={
                "client_id": client_id,
                "client_secret": client_secret,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("custom_client", "observed_operation"),
    [(True, "read"), (False, "read"), (False, "manager"), (False, "save")],
    ids=["custom-read", "provisioned-read", "provisioned-manager", "provisioned-save"],
)
async def test_google_bootstrap_client_storage_stays_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_client: bool,
    observed_operation: str,
) -> None:
    """Custom config reads and provisioned config reads/writes leave the owner loop free."""
    owner_thread = threading.get_ident()
    requests = _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    service = "google_drive_oauth_client" if custom_client else "google_oauth_client"
    manager.save_credentials(
        service,
        {
            "client_id": "previous-client.apps.googleusercontent.com",
            "client_secret": "previous-secret",
            RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: not custom_client,
            _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY: 0.0,
        },
    )
    client_path = manager.get_credentials_path(service)
    original_read = credentials_module._read_credentials_payload
    original_save = manager.save_credentials
    operations: set[str] = set()

    def observed_read(path: Path) -> bytes | None:
        if path == client_path:
            assert threading.get_ident() != owner_thread, "Google client read blocked the event loop"
            operations.add("read")
        return original_read(path)

    def observed_manager(paths: RuntimePaths) -> CredentialsManager:
        assert threading.get_ident() != owner_thread, "Google client path resolution blocked the event loop"
        operations.add("manager")
        return get_runtime_credentials_manager(paths)

    def observed_save(saved_service: str, credentials: dict[str, Any]) -> None:
        assert threading.get_ident() != owner_thread, "Google client write blocked the event loop"
        operations.add("save")
        original_save(saved_service, credentials)

    with monkeypatch.context() as storage_patch:
        if observed_operation == "read":
            storage_patch.setattr(credentials_module, "_read_credentials_payload", observed_read)
        elif observed_operation == "manager":
            storage_patch.setattr("mindroom.oauth.google.get_runtime_credentials_manager", observed_manager)
        else:
            storage_patch.setattr(manager, "save_credentials", observed_save)
        endpoints = await google_oauth_provider("google_drive").runtime_endpoints(runtime_paths)

    assert operations == {observed_operation}
    assert endpoints.authorization_url == GOOGLE_AUTHORIZATION_URL
    assert endpoints.token_url == GOOGLE_TOKEN_URL
    stored = manager.load_credentials(service)
    assert stored is not None
    if custom_client:
        assert stored["client_id"] == "previous-client.apps.googleusercontent.com"
        assert requests == []
    else:
        assert stored["client_id"] == PROVISIONED_CLIENT_ID
        assert stored["client_secret"] == PROVISIONED_CLIENT_SECRET
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_google_bootstrap_drains_client_publication_before_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cancellation cannot abandon an accepted client-config publication."""
    _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    original_save = manager.save_credentials
    entered = threading.Event()
    release = threading.Event()

    def blocked_save(service: str, credentials: dict[str, Any]) -> None:
        entered.set()
        assert release.wait(5), "Client publication gate was not released"
        original_save(service, credentials)

    monkeypatch.setattr(manager, "save_credentials", blocked_save)
    bootstrap = asyncio.create_task(google_oauth_provider("google_drive").runtime_endpoints(runtime_paths))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        bootstrap.cancel()
        await asyncio.sleep(0)
        bootstrap.cancel()
        await asyncio.sleep(0)
        assert not bootstrap.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await bootstrap

    stored = manager.load_credentials("google_oauth_client")
    assert stored is not None
    assert stored["client_id"] == PROVISIONED_CLIENT_ID
    assert stored["client_secret"] == PROVISIONED_CLIENT_SECRET


def test_google_oauth_provider_bootstraps_client_for_paired_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paired runtime fetches and stores the desktop client outside the package."""
    _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    provider = google_oauth_provider("google_drive")

    resolution = asyncio.run(provider.client_config_resolution_async(runtime_paths))

    assert resolution is not None
    assert resolution.custom is False
    assert resolution.service == "google_oauth_client"
    assert resolution.config.client_id == PROVISIONED_CLIENT_ID
    assert resolution.config.client_secret == PROVISIONED_CLIENT_SECRET
    assert resolution.config.redirect_uri == "http://localhost:8765/api/oauth/google_drive/callback"


def test_google_oauth_provider_reads_the_pairing_as_matrix_registration_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank exports do not hide the `.env` pairing, and without a provisioning URL the hosted service pairs it."""
    requests = _install_provisioning_transport(monkeypatch, "https://mindroom.chat")
    (tmp_path / ".env").write_text(
        "MINDROOM_LOCAL_CLIENT_ID=local-client\nMINDROOM_LOCAL_CLIENT_SECRET=local-secret\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_PROVISIONING_URL": "",
            "MINDROOM_LOCAL_CLIENT_ID": "",
            "MINDROOM_LOCAL_CLIENT_SECRET": "",
        },
    )

    resolution = asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))

    assert resolution is not None
    assert resolution.config.client_id == PROVISIONED_CLIENT_ID
    assert len(requests) == 1


@pytest.mark.parametrize(
    "process_env",
    [{}, {"MINDROOM_PROVISIONING_URL": "https://provisioning.example"}],
    ids=["fresh-install", "seeded-provisioning-url"],
)
def test_google_oauth_provider_leaves_unpaired_install_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
) -> None:
    """An unpaired runtime with no stored client has no client yet, which is not a bootstrap failure."""
    requests = _install_provisioning_transport(monkeypatch)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )

    resolution = asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))

    assert resolution is None
    assert requests == []


@pytest.mark.parametrize("service", ["google_drive_oauth_client", "google_oauth_client"])
def test_google_oauth_provider_preserves_custom_client_without_provisioning_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    """Provider-specific and shared custom clients always take precedence over provisioning."""
    requests = _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        service,
        {
            "client_id": "custom-client.apps.googleusercontent.com",
            "client_secret": "custom-client-secret",
        },
    )

    resolution = asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))

    assert resolution is not None
    assert resolution.custom is True
    assert resolution.service == service
    assert requests == []


def test_google_oauth_provider_keeps_cached_client_after_unpairing(tmp_path: Path) -> None:
    """A previously provisioned client remains usable if pairing environment values disappear."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PROVISIONING_URL": "https://provisioning.example"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    stale_credentials = {
        "client_id": PROVISIONED_CLIENT_ID,
        "client_secret": PROVISIONED_CLIENT_SECRET,
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
        _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY: 0.0,
    }
    manager.save_credentials("google_oauth_client", stale_credentials)

    asyncio.run(google_oauth_provider("google_drive").runtime_endpoints(runtime_paths))

    assert manager.load_credentials("google_oauth_client") == stale_credentials


@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "localhost.",
        "app.localhost",
        "127.0.0.1",
        "127.0.0.2",
        "[::1]",
        "[::ffff:127.0.0.2]",
    ],
)
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

    resolution = asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))

    assert resolution is not None


@pytest.mark.parametrize(
    "hostname",
    ["provisioning.example", "2130706433", "127.1", "0x7f000001", "1.1", "0x08080808"],
)
def test_google_oauth_provider_rejects_remote_http_provisioning(tmp_path: Path, hostname: str) -> None:
    """Pairing credentials must never be sent to a plaintext remote endpoint."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_PROVISIONING_URL": f"http://{hostname}",
            "MINDROOM_LOCAL_CLIENT_ID": "local-client",
            "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret",
        },
    )

    with pytest.raises(OAuthProviderError, match="must use HTTPS"):
        asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))


def test_google_oauth_provider_bootstrapped_client_authorization_uses_pkce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provisioned desktop client sends an S256 challenge and the local callback."""
    requests = _install_provisioning_transport(monkeypatch)
    runtime_paths = _paired_runtime_paths(tmp_path)
    provider = google_oauth_provider("google_gmail")
    code_verifier = provider.issue_pkce_code_verifier()
    assert code_verifier is not None

    auth_url = asyncio.run(
        oauth_authorization_url(
            provider,
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


def test_google_oauth_provider_refreshes_stale_provisioned_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provisioned client is refreshed after the app secret rotates."""
    now = 1000.0 + _GOOGLE_PROVISIONED_CLIENT_TTL_SECONDS
    rotated_client_secret = "rotated-client-secret"  # noqa: S105
    monkeypatch.setattr("mindroom.oauth.google.time.time", lambda: now)
    requests = _install_provisioning_transport(
        monkeypatch,
        client_id="rotated-client.apps.googleusercontent.com",
        client_secret=rotated_client_secret,
    )
    runtime_paths = _paired_runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_oauth_client",
        {
            "client_id": PROVISIONED_CLIENT_ID,
            "client_secret": PROVISIONED_CLIENT_SECRET,
            RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
            _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY: 1000.0,
        },
    )

    asyncio.run(google_oauth_provider("google_drive").runtime_endpoints(runtime_paths))

    assert len(requests) == 1
    assert manager.load_credentials("google_oauth_client") == {
        "client_id": "rotated-client.apps.googleusercontent.com",
        "client_secret": rotated_client_secret,
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
        _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY: now,
    }


def test_google_oauth_provider_keeps_stale_client_when_refresh_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provisioning outage does not discard the last usable cached client."""
    requests = _install_provisioning_transport(monkeypatch, status_code=503)
    runtime_paths = _paired_runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(runtime_paths)
    stale_credentials = {
        "client_id": PROVISIONED_CLIENT_ID,
        "client_secret": PROVISIONED_CLIENT_SECRET,
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
        _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY: 0.0,
    }
    manager.save_credentials("google_oauth_client", stale_credentials)

    asyncio.run(google_oauth_provider("google_drive").runtime_endpoints(runtime_paths))

    assert len(requests) == 1
    assert manager.load_credentials("google_oauth_client") == stale_credentials


@pytest.mark.parametrize(
    ("process_env", "message"),
    [
        ({"MINDROOM_LOCAL_CLIENT_ID": "local-client"}, "Provisioning credentials are incomplete"),
        (
            {
                "MINDROOM_LOCAL_CLIENT_ID": "local-client",
                "MINDROOM_LOCAL_CLIENT_SECRET": "local-secret",
                "MATRIX_HOMESERVER": "https://matrix.example.org",
            },
            "Google OAuth bootstrap requires MINDROOM_PROVISIONING_URL",
        ),
    ],
    ids=["no-secret", "no-provisioning-url"],
)
def test_google_oauth_provider_refuses_a_partial_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
    message: str,
) -> None:
    """A pairing that lost part of its settings is a broken pairing to restore, not an install without a client."""
    requests = _install_provisioning_transport(monkeypatch)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )

    with pytest.raises(OAuthProviderError, match=message):
        asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))
    assert requests == []


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "MindRoom pairing credentials are invalid or revoked"),
        (403, "MindRoom pairing credentials are invalid or revoked"),
        (503, "The MindRoom provisioning service has not configured the Google OAuth client yet"),
    ],
)
def test_google_oauth_provider_refuses_a_failed_fetch_with_nothing_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    message: str,
) -> None:
    """A revoked or unready pairing with no cached client fails instead of looking like an install without a client."""
    requests = _install_provisioning_transport(monkeypatch, status_code=status_code)
    runtime_paths = _paired_runtime_paths(tmp_path)

    with pytest.raises(OAuthProviderError, match=message):
        asyncio.run(google_oauth_provider("google_drive").client_config_resolution_async(runtime_paths))
    assert len(requests) == 1
    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") is None


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
        google_oauth_provider("google_drive"),
        "/api/oauth/google_drive/connect?agent_name=general",
    ) == (
        "Google Drive is not connected for this agent. "
        "Open this MindRoom link to connect it, then retry the request: "
        "/api/oauth/google_drive/connect?agent_name=general"
    )


def test_build_oauth_connect_instruction_explains_loopback_device_handoff() -> None:
    """Loopback links tell mobile users which computer must open the URL."""
    connect_url = "http://localhost:8765/api/oauth/google_drive/authorize?connect_token=opaque"

    assert build_oauth_connect_instruction(google_oauth_provider("google_drive"), connect_url) == (
        "Google Drive is not connected for this agent. "
        "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
        "not on a phone or another computer. If needed, open this conversation there or copy the complete "
        "link into that browser. After connecting, retry the request: "
        f"{connect_url}"
    )


def test_build_oauth_reconnect_instruction_explains_loopback_device_handoff() -> None:
    """Expired loopback sessions retain the same device-handoff guidance."""
    connect_url = "http://127.0.0.1:8765/api/oauth/google_drive/authorize?connect_token=opaque"

    assert build_oauth_reconnect_instruction(google_oauth_provider("google_drive"), connect_url) == (
        "Google Drive session for this agent expired or is no longer valid. "
        "Open this MindRoom link in a browser on the computer where the MindRoom process is running, "
        "not on a phone or another computer. If needed, open this conversation there or copy the complete "
        "link into that browser. After reconnecting, retry the request. "
        "This link is valid for 10 minutes; if it expires, rerun the original request for a fresh link: "
        f"{connect_url}"
    )


def test_build_oauth_reconnect_instruction_omits_expiry_for_dashboard_link() -> None:
    """Requester-less dashboard fallback links must not claim a capability TTL."""
    connect_url = "https://mindroom.example/api/oauth/google_drive/authorize?agent_name=general&execution_scope=shared"

    assert build_oauth_reconnect_instruction(google_oauth_provider("google_drive"), connect_url) == (
        "Google Drive session for this agent expired or is no longer valid. "
        "Reconnect it with this MindRoom link. After reconnecting, retry the request. "
        f"Reconnect here: {connect_url}"
    )
