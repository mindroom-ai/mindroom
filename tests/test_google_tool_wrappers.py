"""Tests for Google-backed custom tool wrappers."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
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
@pytest.mark.parametrize("tool_class", [GmailTools, GoogleCalendarTools, GoogleDriveTools, GoogleSheetsTools])
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


@pytest.mark.parametrize("tool_class", [GmailTools, GoogleCalendarTools, GoogleDriveTools, GoogleSheetsTools])
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


@pytest.mark.parametrize(
    ("tool_class", "expected_scopes"),
    [
        (
            GoogleCalendarTools,
            list(GoogleCalendarTools._oauth_provider.scopes),
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

    assert isinstance(tool, (GmailTools, GoogleCalendarTools, GoogleDriveTools, GoogleSheetsTools))
    assert tool._load_token_data() is not None


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
