"""Tests for Google-backed custom tool wrappers."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools.function import Function
from google.auth.exceptions import RefreshError, TransportError

from mindroom.config.auth import AuthorizationConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import (
    CredentialsManager,
    get_runtime_credentials_manager,
    load_scoped_credentials,
    save_scoped_credentials,
)
from mindroom.custom_tools import google_service
from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_docs import GoogleDocsTools
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin, google_service_account_configured
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.oauth import client as oauth_client_module
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    OAuthCredentialsRefreshResult,
    reset_oauth_credentials,
)
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.oauth.service import oauth_credentials_worker_target
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


class ValidCredentials:
    """Minimal valid credential object for constructor tests."""

    valid = True


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create an isolated runtime context for Google tool wrapper tests."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={},
    )
    get_runtime_credentials_manager(paths).save_credentials(
        "google_oauth_client",
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "ui",
        },
    )
    return paths


@pytest.mark.parametrize("worker_scope", ["user", "user_agent"])
@pytest.mark.parametrize(
    "tool_class",
    [GmailTools, GoogleCalendarTools, GoogleDocsTools, GoogleDriveTools, GoogleSheetsTools],
)
def test_google_wrappers_allow_isolating_worker_scopes(
    worker_scope: str,
    tool_class: type[Any],
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Google OAuth-backed tools can use requester-isolated credential scopes."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    tool = tool_class(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=resolve_worker_target(
            worker_scope,
            "general",
            execution_identity=identity,
            tenant_id=runtime_paths.env_value("CUSTOMER_ID"),
            account_id=runtime_paths.env_value("ACCOUNT_ID"),
        ),
    )

    assert isinstance(tool, tool_class)


@pytest.mark.parametrize(
    "tool_class",
    [GmailTools, GoogleCalendarTools, GoogleDocsTools, GoogleDriveTools, GoogleSheetsTools],
)
def test_google_service_cache_is_isolated_per_thread(
    tool_class: type[Any],
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Google API clients should not share httplib2-backed service objects across threads."""
    tool = tool_class(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=ValidCredentials(),
    )
    barrier = threading.Barrier(2)

    def set_and_read_thread_service() -> bool:
        thread_service = object()
        tool.service = thread_service
        barrier.wait(timeout=5)
        return tool.service is thread_service

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: set_and_read_thread_service(), range(2)))

    assert results == [True, True]


def test_google_service_state_first_access_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent first access must not replace another thread's service state."""

    class Tool(ThreadLocalGoogleServiceMixin):
        pass

    class RaceLocal:
        service: Any | None = None

    tool = Tool()
    creation_barrier = threading.Barrier(2)
    read_barrier = threading.Barrier(2)

    def race_local_factory() -> RaceLocal:
        creation_barrier.wait(timeout=5)
        return RaceLocal()

    monkeypatch.setattr(google_service.threading, "local", race_local_factory)

    def set_and_read_thread_service() -> bool:
        thread_service = object()
        tool.service = thread_service
        read_barrier.wait(timeout=5)
        return tool.service is thread_service

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: set_and_read_thread_service(), range(2)))

    assert results == [True, True]


def test_google_service_account_configured_checks_instance_and_runtime_values(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Service-account fallback should honor explicit and runtime configuration."""
    service_account_path = tmp_path / "service-account.json"
    runtime_paths_with_env = replace(
        runtime_paths,
        process_env={
            **runtime_paths.process_env,
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(service_account_path),
        },
    )

    assert google_service_account_configured(str(service_account_path), runtime_paths) is True
    assert google_service_account_configured(None, runtime_paths_with_env) is True
    assert google_service_account_configured(None, runtime_paths) is False


@pytest.mark.parametrize(
    ("tool_class", "expected_scopes"),
    [
        (
            GoogleCalendarTools,
            list(GoogleCalendarTools._oauth_provider.scopes),
        ),
        (
            GoogleDocsTools,
            list(GoogleDocsTools._oauth_provider.scopes),
        ),
        (
            GoogleSheetsTools,
            list(GoogleSheetsTools._oauth_provider.scopes),
        ),
    ],
)
def test_google_wrapper_build_credentials_uses_provider_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tool_class: type[Any],
    expected_scopes: list[str],
    runtime_paths: RuntimePaths,
) -> None:
    """Stored tokens without a scope list should fall back to the provider scopes."""
    monkeypatch.setattr("mindroom.oauth.client.ensure_tool_deps", lambda *_args, **_kwargs: None)

    tool = object.__new__(tool_class)
    tool._oauth_tool_name = tool_class._oauth_tool_name
    tool._oauth_provider = tool_class._oauth_provider
    tool._runtime_paths = runtime_paths
    creds = tool._credentials_from_token_data(
        {
            "token": "token",
            "refresh_token": "refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
    )

    assert creds.scopes == expected_scopes


@pytest.mark.parametrize(
    ("tool_name", "credential_service"),
    [
        ("gmail", "google_gmail_oauth"),
        ("google_calendar", "google_calendar_oauth"),
        ("google_docs", "google_docs_oauth"),
        ("google_drive", "google_drive_oauth"),
        ("google_sheets", "google_sheets_oauth"),
    ],
)
def test_google_wrappers_load_provider_oauth_credentials(
    tool_name: str,
    credential_service: str,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Google wrappers should load each provider's OAuth token service."""
    credentials_manager = CredentialsManager(base_path=tmp_path / "credentials")
    credentials_manager.save_credentials(
        credential_service,
        {
            "token": "token",
            "refresh_token": "refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "_source": "oauth",
        },
    )

    tool = get_tool_by_name(
        tool_name,
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    assert isinstance(tool, (GmailTools, GoogleCalendarTools, GoogleDocsTools, GoogleDriveTools, GoogleSheetsTools))
    assert tool._load_token_data() is not None


def test_scoped_oauth_client_structured_auth_failure_returns_oauth_required_json_string() -> None:
    """Scoped OAuth tools should return the public OAuth-required payload as a JSON string."""
    tool = object.__new__(GoogleDriveTools)
    result = tool._structured_auth_failure(
        OAuthConnectionRequired(
            "Google Drive is not connected for this agent.",
            provider_id="google_drive",
            connect_url="/api/oauth/google_drive/connect?agent_name=general",
        ),
    )

    payload = json.loads(result)
    assert list(payload) == ["error", "oauth_connection_required", "provider", "connect_url"]
    assert payload == {
        "error": "Google Drive is not connected for this agent.",
        "oauth_connection_required": True,
        "provider": "google_drive",
        "connect_url": "/api/oauth/google_drive/connect?agent_name=general",
    }


def test_scoped_oauth_client_connection_required_uses_shared_instruction(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Client OAuth prompts should share the service-owned instruction text."""
    tool = object.__new__(GoogleDriveTools)
    tool._oauth_provider = GoogleDriveTools._oauth_provider
    tool._runtime_paths = runtime_paths
    tool._worker_target = None
    tool._creds_manager = get_runtime_credentials_manager(runtime_paths)

    seen: list[object] = []

    def connection_required(context: object, *, reason: str | None = None) -> OAuthConnectionRequired:
        seen.append(context)
        assert reason is None
        return OAuthConnectionRequired(
            "shared instruction: https://connect.example.test",
            provider_id="google_drive",
            connect_url="https://connect.example.test",
        )

    monkeypatch.setattr(oauth_client_module, "oauth_connection_required", connection_required)

    exc = tool._connection_required()

    assert str(exc) == "shared instruction: https://connect.example.test"
    assert exc.provider_id == "google_drive"
    assert exc.connect_url == "https://connect.example.test"
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("refresh_error", "expected_reason", "credential_remains"),
    [
        (RefreshError("refresh rejected", {"error": "invalid_grant"}), "refresh_rejected", False),
        (RefreshError("refresh rejected", {"error": "invalid_refresh_token"}), "refresh_rejected", False),
        (TransportError("provider unavailable"), None, True),
    ],
)
def test_google_wrapper_refresh_failure_recovery_is_terminal_only(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
    refresh_error: Exception,
    expected_reason: str | None,
    credential_remains: bool,
) -> None:
    """Only a terminal Google refresh rejection should clear the current credential scope."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=identity,
    )
    token_data = {
        "token": "expired-access-token",
        "refresh_token": "stored-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id",
        "expires_at": 1.0,
        "scopes": list(GoogleDriveTools._oauth_provider.scopes),
        "_source": "oauth",
        "_oauth_provider": GoogleDriveTools._oauth_provider.id,
    }
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        token_data,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    def fail_refresh(*_args: object, **_kwargs: object) -> None:
        raise refresh_error

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail_refresh)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    result = tool._ensure_structured_auth()

    assert result is not None
    payload = json.loads(result)
    assert payload["oauth_connection_required"] is True
    assert payload.get("reason") == expected_reason
    stored = load_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    assert (stored is not None) is credential_remains


def test_google_wrapper_replaces_swallowed_mid_call_refresh_rejection(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A provider rejection swallowed by an upstream tool should still become a reconnect response."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=identity,
    )
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "valid-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    provider_detail = "refresh rejected with stored-refresh-token"

    def fail_refresh(*_args: object, **_kwargs: object) -> None:
        raise RefreshError(
            provider_detail,
            {"error": "invalid_grant", "error_description": provider_detail},
        )

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail_refresh)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    captured_log_messages: list[str] = []

    def swallowed_failure() -> str:
        try:
            tool.creds.refresh(object())
        except RefreshError as exc:
            captured_log_messages.append(str(exc))
            return f"Unexpected error: {exc}"
        return "unexpected success"

    tool.functions = {"swallowed_failure": Function(name="swallowed_failure", entrypoint=swallowed_failure)}
    tool._wrap_oauth_function_entrypoints()

    payload = json.loads(tool.swallowed_failure())

    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "refresh_rejected"
    assert captured_log_messages == ["OAuth credential refresh failed"]
    assert provider_detail not in repr(captured_log_messages)
    assert (
        load_scoped_credentials(
            GoogleDriveTools._oauth_provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is None
    )


def test_google_lazy_refresh_reuses_rotation_committed_for_a_stale_client(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A stale lazy client must observe a serialized rotation instead of rotating again."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "expired-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 1.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    tools = [
        GoogleDriveTools(
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        for _ in range(2)
    ]
    provider_calls = 0
    rotated_access = "rotated-access-token"

    def rotate(credentials: object, _request: object) -> None:
        nonlocal provider_calls
        provider_calls += 1
        credentials.token = rotated_access  # type: ignore[attr-defined]
        credentials.expiry = datetime.fromtimestamp(4_102_444_800.0, tz=UTC)  # type: ignore[attr-defined]

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", rotate)

    tools[0].creds.refresh(object())
    tools[1].creds.refresh(object())

    assert provider_calls == 1
    assert tools[0].creds.token == rotated_access
    assert tools[1].creds.token == rotated_access


def test_google_lazy_refresh_serializes_local_snapshot_publication(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """One client must not rotate again before its prior local token publication finishes."""
    first_rotated_access_token = "rotated-access-token-1"  # noqa: S105
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "expired-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 1.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    provider_calls = 0
    provider_calls_lock = threading.Lock()

    def rotate(credentials: object, _request: object) -> None:
        nonlocal provider_calls
        with provider_calls_lock:
            provider_calls += 1
            call_number = provider_calls
        credentials.token = f"rotated-access-token-{call_number}"  # type: ignore[attr-defined]
        credentials.expiry = datetime.fromtimestamp(4_102_444_800.0, tz=UTC)  # type: ignore[attr-defined]

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", rotate)
    real_refresh = oauth_client_module.refresh_oauth_credentials_sync
    lifecycle_calls = 0
    lifecycle_calls_lock = threading.Lock()
    second_lifecycle_entered = threading.Event()

    def observe_refresh(
        context: OAuthCredentialContext,
        refresh: Callable[[Mapping[str, Any]], dict[str, Any] | None],
    ) -> OAuthCredentialsRefreshResult:
        nonlocal lifecycle_calls
        with lifecycle_calls_lock:
            lifecycle_calls += 1
            if lifecycle_calls == 2:
                second_lifecycle_entered.set()
        return real_refresh(context, refresh)

    monkeypatch.setattr(oauth_client_module, "refresh_oauth_credentials_sync", observe_refresh)
    real_raw_credentials = tool._raw_credentials_from_token_data
    first_publish_blocked = threading.Event()
    release_first_publish = threading.Event()

    def block_first_publish(token_data: dict[str, Any]) -> Any:  # noqa: ANN401
        refreshed = real_raw_credentials(token_data)
        if token_data.get("token") == first_rotated_access_token:
            first_publish_blocked.set()
            assert release_first_publish.wait(timeout=5)
        return refreshed

    monkeypatch.setattr(tool, "_raw_credentials_from_token_data", block_first_publish)
    second_call_started = threading.Event()

    def second_refresh() -> None:
        second_call_started.set()
        tool.creds.refresh(object())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(tool.creds.refresh, object())
        assert first_publish_blocked.wait(timeout=5)
        second = executor.submit(second_refresh)
        assert second_call_started.wait(timeout=5)
        try:
            assert not second_lifecycle_entered.wait(timeout=0.5)
        finally:
            release_first_publish.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert provider_calls == 1
    assert tool.creds.token == first_rotated_access_token


def test_google_wrapper_constructor_canonicalizes_alias_without_runtime_context(
    runtime_paths: RuntimePaths,
) -> None:
    """Toolkit construction must own alias resolution without an ambient call context."""
    alias = "@telegram_alice:example.org"
    canonical = "@alice:example.org"
    canonical_access_token = "canonical-access-token"  # noqa: S105
    authorization = AuthorizationConfig(aliases={canonical: [alias]})
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=alias,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    raw_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    canonical_target = oauth_credentials_worker_target(
        GoogleDriveTools._oauth_provider,
        raw_target,
        authorization=authorization,
    )
    assert canonical_target is not None
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": canonical_access_token,
            "refresh_token": "canonical-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=canonical_target,
    )

    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=raw_target,
        authorization=authorization,
    )

    assert tool.creds.token == canonical_access_token
    assert tool._oauth_credential_context().worker_target == canonical_target


@pytest.mark.asyncio
async def test_google_wrapper_drops_valid_cached_credentials_after_reset(
    runtime_paths: RuntimePaths,
) -> None:
    """Every managed entrypoint must observe reset before reusing a cached access token."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "valid-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    tool.service = object()

    assert await reset_oauth_credentials(tool._oauth_credential_context()) is True
    payload = json.loads(await asyncio.to_thread(tool._ensure_structured_auth))

    assert payload["oauth_connection_required"] is True
    assert tool.creds is None
    assert tool.service is None


@pytest.mark.asyncio
async def test_google_wrapper_replaces_swallowed_async_upload_refresh_rejection(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Async uploads should return the same reconnect response as synchronous calls."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=identity,
    )
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "valid-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    def fail_refresh(*_args: object, **_kwargs: object) -> None:
        message = "refresh rejected"
        raise RefreshError(message, {"error": "invalid_grant"})

    def swallowed_upload(self: GoogleDriveTools, *_args: object, **_kwargs: object) -> str:
        try:
            self.creds.refresh(object())
        except RefreshError as exc:
            return f"Unexpected error: {exc}"
        return "unexpected success"

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail_refresh)
    monkeypatch.setattr(GoogleDriveTools, "_upload_file", swallowed_upload)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    entrypoint = tool.async_functions["google_drive_upload_file"].entrypoint
    assert entrypoint is not None

    payload = json.loads(await entrypoint("unused"))

    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "refresh_rejected"
    assert (
        load_scoped_credentials(
            GoogleDriveTools._oauth_provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is None
    )


def test_google_wrapper_keeps_refresh_rejection_state_per_call(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A successful parallel call must not consume another call's reconnect signal."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "valid-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    def fail_refresh(*_args: object, **_kwargs: object) -> None:
        message = "refresh rejected"
        raise RefreshError(message, {"error": "invalid_grant"})

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail_refresh)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    failure_recorded = threading.Event()
    release_failure = threading.Event()

    def swallowed_failure() -> str:
        try:
            tool.creds.refresh(object())
        except RefreshError as exc:
            failure_recorded.set()
            assert release_failure.wait(timeout=5)
            return f"Unexpected error: {exc}"
        return "unexpected success"

    tool.functions = {
        "swallowed_failure": Function(name="swallowed_failure", entrypoint=swallowed_failure),
        "successful_call": Function(name="successful_call", entrypoint=lambda: "success"),
    }
    tool._wrap_oauth_function_entrypoints()

    with ThreadPoolExecutor(max_workers=2) as executor:
        failed_future = executor.submit(tool.swallowed_failure)
        assert failure_recorded.wait(timeout=5)
        successful_result = executor.submit(tool.successful_call).result(timeout=5)
        release_failure.set()
        failed_result = failed_future.result(timeout=5)

    assert successful_result == "success"
    failed_payload = json.loads(failed_result)
    assert failed_payload["oauth_connection_required"] is True
    assert failed_payload["reason"] == "refresh_rejected"


def test_google_wrapper_reports_missing_connection_after_terminal_deletion(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A later live client still returns a connection prompt after the grant is deleted."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_target = resolve_worker_target("user_agent", "general", execution_identity=identity)
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "valid-access-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    def fail_refresh(*_args: object, **_kwargs: object) -> None:
        message = "refresh rejected"
        raise RefreshError(message, {"error": "invalid_grant"})

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail_refresh)
    tools = [
        GoogleDriveTools(
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        for _ in range(2)
    ]
    for tool in tools:

        def swallowed_failure(*, _tool: GoogleDriveTools = tool) -> str:
            try:
                _tool.creds.refresh(object())
            except RefreshError as exc:
                return f"Unexpected error: {exc}"
            return "unexpected success"

        tool.functions = {"swallowed_failure": Function(name="swallowed_failure", entrypoint=swallowed_failure)}
        tool._wrap_oauth_function_entrypoints()

    first_payload = json.loads(tools[0].swallowed_failure())
    second_payload = json.loads(tools[1].swallowed_failure())

    assert first_payload["reason"] == "refresh_rejected"
    assert second_payload["oauth_connection_required"] is True
    assert "reason" not in second_payload


def test_google_wrapper_skips_stored_oauth_when_service_account_env_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Service-account deployments should not load stored user OAuth tokens at construction."""
    runtime_paths = replace(
        runtime_paths,
        process_env={
            **runtime_paths.process_env,
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json"),
        },
    )

    def fail_load_stored_credentials(_self: ScopedOAuthClientMixin) -> None:
        raise AssertionError

    monkeypatch.setattr(
        ScopedOAuthClientMixin,
        "_load_stored_credentials",
        fail_load_stored_credentials,
    )

    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
    )

    assert tool.creds is None


def test_google_wrapper_applies_env_file_service_account_to_upstream_auth(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Service-account values from RuntimePaths must be visible to Agno auth."""
    service_account_path = tmp_path / "service-account.json"
    runtime_paths = replace(
        runtime_paths,
        env_file_values={
            **runtime_paths.env_file_values,
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(service_account_path),
            "GOOGLE_DELEGATED_USER": "alice@example.com",
        },
    )

    def fail_load_stored_credentials(_self: ScopedOAuthClientMixin) -> None:
        raise AssertionError

    monkeypatch.setattr(
        ScopedOAuthClientMixin,
        "_load_stored_credentials",
        fail_load_stored_credentials,
    )

    tool = GmailTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
    )

    assert tool.creds is None
    assert tool.service_account_path == str(service_account_path)
    assert tool.delegated_user == "alice@example.com"
    assert tool._should_fallback_to_original_auth() is True


def test_google_wrapper_service_account_fallback_wins_over_valid_cached_oauth(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """A valid cached OAuth credential must not bypass service-account auth."""

    class ValidOAuthCreds:
        valid = True

    class ValidServiceAccountCreds:
        valid = True

    tool = object.__new__(GoogleDriveTools)
    tool._runtime_paths = runtime_paths
    tool._provided_creds = False
    tool._defer_to_original_auth = True
    tool._original_auth_completed = False
    tool.service_account_path = str(tmp_path / "service-account.json")
    tool.creds = ValidOAuthCreds()
    calls: list[str] = []

    def original_auth() -> None:
        calls.append("original")
        tool.creds = ValidServiceAccountCreds()

    tool._original_auth = original_auth

    assert tool._ensure_structured_auth() is None
    assert calls == ["original"]
    assert tool._ensure_structured_auth() is None
    assert calls == ["original"]


def test_google_wrapper_valid_provided_creds_skip_service_account_fallback(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Explicit valid credentials should keep Agno's no-auth constructor contract."""

    class ValidProvidedCreds:
        valid = True

    tool = object.__new__(GoogleDriveTools)
    tool._runtime_paths = runtime_paths
    tool._provided_creds = True
    tool._defer_to_original_auth = True
    tool._original_auth_completed = False
    tool.service_account_path = str(tmp_path / "service-account.json")
    tool.creds = ValidProvidedCreds()
    calls: list[str] = []

    def original_auth() -> None:
        calls.append("original")

    tool._original_auth = original_auth

    assert tool._ensure_structured_auth() is None
    assert calls == []


@pytest.mark.parametrize(
    ("max_read_size", "expected"),
    [
        ("42", 42),
        ("42.5", 42.5),
        ("", 10485760),
        (None, 10485760),
    ],
)
def test_google_drive_constructor_coerces_optional_max_read_size(
    max_read_size: object,
    expected: float,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Direct constructor overrides should match stored dashboard number coercion."""
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=ValidCredentials(),
        max_read_size=max_read_size,
    )

    assert tool.max_read_size == expected


@pytest.mark.parametrize(
    ("max_read_size", "error_type", "match"),
    [
        (True, TypeError, "Google Drive max_read_size must be a number"),
        ("not-a-number", ValueError, "Google Drive max_read_size must be a number"),
        (float("inf"), TypeError, "Google Drive max_read_size must be a finite number"),
        ("inf", ValueError, "Google Drive max_read_size must be a finite number"),
    ],
)
def test_google_drive_constructor_rejects_invalid_max_read_size_with_current_errors(
    max_read_size: object,
    error_type: type[Exception],
    match: str,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Direct constructor validation should keep current exception types and messages."""
    with pytest.raises(error_type, match=match):
        GoogleDriveTools(
            runtime_paths=runtime_paths,
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            creds=ValidCredentials(),
            max_read_size=max_read_size,
        )
