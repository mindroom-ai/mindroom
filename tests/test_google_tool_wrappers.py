"""Tests for Google-backed custom tool wrappers."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Never

import pytest
from agno.tools.function import Function
from agno.utils import log as agno_log_module
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.errors import HttpError

from mindroom.config.auth import AuthorizationConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import (
    CredentialsManager,
    get_runtime_credentials_manager,
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
    exchange_and_store_oauth_credentials,
    load_oauth_credentials,
    oauth_connection_generation,
    oauth_credential_generation,
    reset_oauth_credentials,
)
from mindroom.oauth.google_drive import GOOGLE_DRIVE_READ_OAUTH_SCOPES
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthTokenResult
from mindroom.oauth.service import oauth_credentials_worker_target
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target, tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


def _valid_credentials(*, token: str = "valid-access-token") -> GoogleOAuthCredentials:  # noqa: S107
    """Build exact supported credentials for constructor tests."""
    return GoogleOAuthCredentials(
        token=token,
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("scope",),
        expiry=datetime(2100, 1, 1),  # noqa: DTZ001
    )


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
        creds=_valid_credentials(),
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
        creds: Any | None = None
        service: Any | None = None
        credential_key: object | None = None

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


@pytest.mark.parametrize(
    ("cache_field", "account_a_value"),
    [
        pytest.param("_label_cache", {"inbox": "label-a"}, id="gmail-labels"),
        pytest.param("_user_email", "alice@example.com", id="calendar-principal"),
    ],
)
def test_google_account_cache_clears_when_credential_key_changes(
    cache_field: str,
    account_a_value: object,
) -> None:
    """Account-derived identifiers must not survive a requester credential switch."""

    class Tool(ThreadLocalGoogleServiceMixin):
        pass

    tool = Tool()
    tool._google_credential_key = "account-a"
    setattr(tool, cache_field, account_a_value)
    tool._google_credential_key = "account-a"
    assert getattr(tool, cache_field) == account_a_value

    tool._google_credential_key = "account-b"

    assert getattr(tool, cache_field) is None


def test_google_refresh_revision_adoption_preserves_active_service_and_account_caches() -> None:
    """Same-account refresh must not invalidate service state midway through one tool call."""

    class Tool(ThreadLocalGoogleServiceMixin):
        pass

    tool = Tool()
    service = object()
    tool._google_credential_key = "revision-a"
    tool.service = service
    tool._label_cache = {"inbox": "label-a"}
    tool._user_email = "alice@example.com"

    tool._adopt_google_credential_revision("revision-b")

    assert tool._google_credential_key == "revision-b"
    assert tool.service is service
    assert tool._label_cache == {"inbox": "label-a"}
    assert tool._user_email == "alice@example.com"


@pytest.mark.parametrize(
    ("cache_field", "values"),
    [
        pytest.param("_label_cache", ({"inbox": "label-a"}, {"inbox": "label-b"}), id="gmail-labels"),
        pytest.param("_user_email", ("alice@example.com", "bob@example.com"), id="calendar-principal"),
    ],
)
def test_google_account_cache_is_thread_local(cache_field: str, values: tuple[object, object]) -> None:
    """Concurrent requester workers must not share account-derived identifiers."""

    class Tool(ThreadLocalGoogleServiceMixin):
        pass

    tool = Tool()
    barrier = threading.Barrier(2)

    def set_and_read(value: object) -> object:
        setattr(tool, cache_field, value)
        barrier.wait(timeout=5)
        return getattr(tool, cache_field)

    with ThreadPoolExecutor(max_workers=2) as executor:
        observed = list(executor.map(set_and_read, values))

    assert observed == list(values)


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
    tool._oauth_quota_project_id = None
    creds = tool._raw_credentials_from_token_data(
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
    tool._authorization = None
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

    if expected_reason is None:
        with pytest.raises(RefreshError, match="OAuth credential refresh failed"):
            tool._ensure_structured_auth()
        assert tool._consume_oauth_connection_required() == (False, None)
    else:
        result = tool._ensure_structured_auth()
        assert result is not None
        payload = json.loads(result)
        assert payload["oauth_connection_required"] is True
        assert payload.get("reason") == expected_reason
    stored = load_oauth_credentials(tool._oauth_credential_context())
    assert (stored is not None) is credential_remains


@pytest.mark.parametrize(("status", "expected"), [(401, True), (403, False), (200, False)])
def test_google_http_tracker_records_only_final_401(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: bool,
) -> None:
    """The shared transport observes the final response returned after built-in retries."""
    tool = object.__new__(GoogleDriveTools)
    tracked_http = tool._google_authorized_http(object())

    def return_response(*_args: object, **_kwargs: object) -> tuple[object, bytes]:
        return SimpleNamespace(status=status), b"response"

    monkeypatch.setattr("google_auth_httplib2.AuthorizedHttp.request", return_response)

    _response, content = tracked_http.request("https://www.googleapis.com/example")

    assert tool._consume_google_authorization_rejected() is expected
    if status == 401:
        assert content != b"response"
        assert b"response" not in content
    else:
        assert content == b"response"


@pytest.mark.parametrize(
    ("tool_class", "operation"),
    [
        (GoogleCalendarTools, "calendar"),
        (GmailTools, "gmail"),
    ],
)
def test_google_final_401_provider_text_is_redacted_before_toolkit_logging(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    tool_class: type[Any],
    operation: str,
) -> None:
    """Final managed 401 bodies cannot reach Google tool results or logs."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    provider = tool_class._oauth_provider
    save_scoped_credentials(
        provider.credential_service,
        {
            "token": "retained-access-token",
            "refresh_token": "retained-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tool = tool_class(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tracked_http = tool._google_authorized_http(tool.creds)
    sentinel = b'{"error":{"message":"provider-controlled-final-401-secret"}}'
    response = SimpleNamespace(status=401, reason="Unauthorized")

    def rejected_request(*_args: object, **_kwargs: object) -> tuple[object, bytes]:
        return response, sentinel

    class RejectedRequest:
        def events(self) -> RejectedRequest:
            return self

        def users(self) -> RejectedRequest:
            return self

        def messages(self) -> RejectedRequest:
            return self

        def list(self, **_kwargs: object) -> RejectedRequest:
            return self

        def execute(self) -> Never:
            rejected_response, content = tracked_http.request("https://www.googleapis.com/example")
            raise HttpError(rejected_response, content)

    monkeypatch.setattr("google_auth_httplib2.AuthorizedHttp.request", rejected_request)
    tool.service = RejectedRequest()
    agno_log_output = io.StringIO()
    agno_logger = agno_log_module.team_logger
    agno_handler = logging.StreamHandler(agno_log_output)
    agno_logger.addHandler(agno_handler)
    monkeypatch.setattr(agno_log_module, "logger", agno_logger)

    try:
        if operation == "calendar":
            result = tool.list_events(limit=1, start_date="2026-01-01T00:00:00")
        else:
            result = tool.get_latest_emails(1)
    finally:
        agno_logger.removeHandler(agno_handler)
        agno_handler.close()

    payload = json.loads(result)
    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "access_rejected"
    assert "provider-controlled-final-401-secret" not in result
    assert "provider-controlled-final-401-secret" not in agno_log_output.getvalue()


def test_google_sheets_secondary_drive_service_uses_tracked_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate-sheet Drive client must share the final-401 boundary."""
    tool = object.__new__(GoogleSheetsTools)
    credentials = object()
    tool.creds = credentials
    built: dict[str, object] = {}
    service = object()

    def build_service(api: str, version: str, **kwargs: object) -> object:
        built.update({"api": api, "version": version, **kwargs})
        return service

    monkeypatch.setattr("mindroom.custom_tools.google_sheets.build", build_service)

    assert tool._build_drive_service() is service
    assert built["api"] == "drive"
    assert built["version"] == "v3"
    assert "credentials" not in built
    assert isinstance(built["http"], AuthorizedHttp)
    assert built["http"].credentials is credentials


def test_google_sheets_duplicate_initializes_primary_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override retains upstream authentication and primary-service initialization."""
    tool = object.__new__(GoogleSheetsTools)
    tool.creds = _valid_credentials()
    tool.service = None
    tool.scopes = ["https://www.googleapis.com/auth/drive"]
    sheets_service = object()

    class CopyRequest:
        @staticmethod
        def execute() -> dict[str, str]:
            return {"id": "new-sheet"}

    class Files:
        @staticmethod
        def copy(**_kwargs: object) -> CopyRequest:
            return CopyRequest()

    class DriveService:
        @staticmethod
        def files() -> Files:
            return Files()

    monkeypatch.setattr(tool, "_build_service", lambda: sheets_service)
    monkeypatch.setattr(tool, "_build_drive_service", DriveService)

    result = GoogleSheetsTools.create_duplicate_sheet(
        tool,
        "source-sheet",
        new_title="Copy",
        copy_permissions=False,
    )

    assert tool.service is sheets_service
    assert result.endswith("/new-sheet")


def test_gmail_batch_tracks_final_item_401() -> None:
    """A batch-level HTTP 200 must not hide a final per-item authorization rejection."""
    tool = object.__new__(GmailTools)

    class Batch:
        def __init__(self, callback: Callable[..., None]) -> None:
            self.callback = callback

        def add(self, _request: object, *, request_id: str) -> None:
            self.request_id = request_id

        def execute(self) -> None:
            response = SimpleNamespace(status=401, reason="Unauthorized")
            self.callback(
                self.request_id,
                None,
                HttpError(response, b'{"error":"provider-controlled"}'),
            )

    class Service:
        @staticmethod
        def new_batch_http_request(*, callback: Callable[..., None]) -> Batch:
            return Batch(callback)

    tool.service = Service()
    tool.max_batch_size = 10

    results = tool._batch_get(["message-1"], lambda _message_id: object())

    assert results == [{"id": "message-1", "error": "Google request failed"}]
    assert tool._consume_google_authorization_rejected() is True


@pytest.mark.parametrize("operation_name", ["read_operation", "write_operation"])
def test_google_wrapper_maps_swallowed_final_resource_401_to_access_rejected(
    runtime_paths: RuntimePaths,
    operation_name: str,
) -> None:
    """Managed Google read and write errors share one structured final-401 boundary."""
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
    service = GoogleDriveTools._oauth_provider.credential_service
    save_scoped_credentials(
        service,
        {
            "token": "retained-access-token",
            "refresh_token": "retained-refresh-token",
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

    def swallowed_resource_401() -> str:
        tool._google_service_state().authorization_rejected = True
        return json.dumps({"error": "provider-controlled 401 detail"})

    tool.functions = {
        operation_name: Function(name=operation_name, entrypoint=swallowed_resource_401),
    }
    tool._wrap_oauth_function_entrypoints()

    entrypoint = tool.functions[operation_name].entrypoint
    assert entrypoint is not None
    result = entrypoint()
    assert isinstance(result, str)
    payload = json.loads(result)

    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "access_rejected"
    assert "provider-controlled" not in json.dumps(payload)
    assert tool.creds is None
    assert tool.service is None
    stored = load_oauth_credentials(tool._oauth_credential_context())
    assert stored is not None
    assert stored["token"] == "retained-access-token"  # noqa: S105


def test_google_docs_partial_create_401_preserves_non_retryable_result(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected initial-text write must not hide or encourage duplicating the created document."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    provider = GoogleDocsTools._oauth_provider
    save_scoped_credentials(
        provider.credential_service,
        {
            "token": "retained-access-token",
            "refresh_token": "retained-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(provider.scopes),
            "_source": "oauth",
            "_oauth_provider": provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tool = GoogleDocsTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tracked_http = tool._google_authorized_http(tool.creds)
    sentinel = b'{"error":{"message":"provider-controlled-partial-write-secret"}}'
    rejected_response = SimpleNamespace(status=401, reason="Unauthorized")

    def rejected_request(*_args: object, **_kwargs: object) -> tuple[object, bytes]:
        return rejected_response, sentinel

    class Request:
        def __init__(self, result: dict[str, object] | None = None) -> None:
            self.result = result

        def execute(self) -> dict[str, object]:
            if self.result is not None:
                return self.result
            response, content = tracked_http.request("https://www.googleapis.com/docs/v1/documents/created-doc")
            raise HttpError(response, content)

    class Documents:
        @staticmethod
        def create(*, body: dict[str, object]) -> Request:
            assert body == {"title": "Recovery notes"}
            return Request({"documentId": "created-doc", "title": "Recovery notes"})

        @staticmethod
        def batchUpdate(*, documentId: str, body: dict[str, object]) -> Request:  # noqa: N802, N803
            assert documentId == "created-doc"
            assert body["requests"]
            return Request()

    class Service:
        @staticmethod
        def documents() -> Documents:
            return Documents()

    monkeypatch.setattr("google_auth_httplib2.AuthorizedHttp.request", rejected_request)
    tool.service = Service()

    result = tool.google_docs_create_document("Recovery notes", "Initial text")
    payload = json.loads(result)

    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "access_rejected"
    assert payload["partial_success"] is True
    assert payload["retry_safe"] is False
    assert payload["partial_result"]["document"]["documentId"] == "created-doc"
    assert payload["partial_result"]["documentUrl"].endswith("/created-doc/edit")
    assert "do not automatically retry" in payload["error"].lower()
    assert "rerun the original request" not in payload["error"].lower()
    assert "provider-controlled-partial-write-secret" not in result


def test_nested_google_wrapper_preserves_final_resource_401_for_outer_owner(
    runtime_paths: RuntimePaths,
) -> None:
    """A nested tool call must leave final-401 translation to the outer entrypoint."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    service = GoogleDriveTools._oauth_provider.credential_service
    save_scoped_credentials(
        service,
        {
            "token": "retained-access-token",
            "refresh_token": "retained-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    def inner_operation() -> str:
        tool._google_service_state().authorization_rejected = True
        return json.dumps({"error": "provider-controlled 401 detail"})

    def outer_operation() -> str:
        inner_payload = json.loads(tool.inner_operation())
        return json.dumps({"error": inner_payload["error"]})

    tool.functions = {
        "inner_operation": Function(name="inner_operation", entrypoint=inner_operation),
        "outer_operation": Function(name="outer_operation", entrypoint=outer_operation),
    }
    tool._wrap_oauth_function_entrypoints()

    result = tool.outer_operation()
    payload = json.loads(result)

    assert payload["oauth_connection_required"] is True
    assert payload["reason"] == "access_rejected"
    assert "provider-controlled" not in result


def test_google_lazy_refresh_rejects_expired_access_only_snapshot(runtime_paths: RuntimePaths) -> None:
    """An expired access token without a refresh grant cannot report lazy-refresh success."""
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
            "token": "expired-access-only-token",
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

    with pytest.raises(RefreshError, match="OAuth credential refresh failed"):
        tool.creds.refresh(object())

    assert tool._consume_oauth_connection_required() == (True, None)
    assert tool.creds is None


def test_google_forced_refresh_rejects_unexpired_access_only_snapshot(runtime_paths: RuntimePaths) -> None:
    """A rejected bearer without a refresh grant cannot report forced-refresh success."""
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
            "token": "unexpired-access-only-token",
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

    with pytest.raises(RefreshError, match="OAuth credential refresh failed"):
        tool.creds.refresh(object())

    assert tool._consume_oauth_connection_required() == (True, None)
    assert tool.creds is None


def test_google_drive_refreshes_expired_readonly_grant(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Drive's supported read-only grant must reach provider refresh before read authentication."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
    )
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "expired-readonly-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 1.0,
            "scopes": list(GOOGLE_DRIVE_READ_OAUTH_SCOPES),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    def refresh(credentials: GoogleOAuthCredentials, _request: object) -> None:
        credentials.token = "refreshed-readonly-token"  # noqa: S105
        credentials.expiry = datetime(2100, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(GoogleOAuthCredentials, "refresh", refresh)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    assert tool._ensure_structured_auth() is None
    assert tool.creds.token == "refreshed-readonly-token"  # noqa: S105


def test_google_forced_refresh_rejects_unchanged_readonly_bearer(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A forced retry cannot replay the same provider-rejected read-only bearer."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    worker_target = resolve_worker_target(
        "user_agent",
        "general",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
    )
    save_scoped_credentials(
        GoogleDriveTools._oauth_provider.credential_service,
        {
            "token": "rejected-readonly-token",
            "refresh_token": "stored-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "expires_at": 4_102_444_800.0,
            "scopes": list(GOOGLE_DRIVE_READ_OAUTH_SCOPES),
            "_source": "oauth",
            "_oauth_provider": GoogleDriveTools._oauth_provider.id,
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    rotated_refresh_token = "rotated-refresh-token"  # noqa: S105

    def rotate_refresh_grant(credentials: GoogleOAuthCredentials, _request: object) -> None:
        credentials._refresh_token = rotated_refresh_token
        credentials.expiry = datetime(2100, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(GoogleOAuthCredentials, "refresh", rotate_refresh_grant)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    with pytest.raises(RefreshError, match="OAuth credential refresh failed"):
        tool.creds.refresh(object())

    stored = load_oauth_credentials(tool._oauth_credential_context())
    assert stored is not None
    assert stored["token"] == "rejected-readonly-token"  # noqa: S105
    assert stored["refresh_token"] == rotated_refresh_token


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
    assert load_oauth_credentials(tool._oauth_credential_context()) is None


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
            quota_project_id="billing-project",
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
    built_http: list[Any] = []

    def build_drive_service(
        _service_name: str,
        _version: str,
        *,
        http: Any,  # noqa: ANN401
    ) -> object:
        built_http.append(http)
        return object()

    monkeypatch.setattr("mindroom.custom_tools.google_drive.build", build_drive_service)

    tools[0].creds.refresh(object())
    tools[1].creds.refresh(object())
    tools[0]._build_service()

    assert provider_calls == 1
    assert tools[0].creds.token == rotated_access
    assert tools[1].creds.token == rotated_access
    assert len(built_http) == 1
    assert built_http[0].credentials is tools[0].creds
    assert built_http[0].credentials.quota_project_id == "billing-project"
    assert "refresh" in built_http[0].credentials.__dict__


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
        *,
        scope_validator: Callable[[dict[str, Any]], bool] | None = None,
        expected_connection_generation: str | None = None,
    ) -> OAuthCredentialsRefreshResult:
        nonlocal lifecycle_calls
        with lifecycle_calls_lock:
            lifecycle_calls += 1
            if lifecycle_calls == 2:
                second_lifecycle_entered.set()
        return real_refresh(
            context,
            refresh,
            scope_validator=scope_validator,
            expected_connection_generation=expected_connection_generation,
        )

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
    credentials = tool.creds

    def second_refresh() -> None:
        second_call_started.set()
        credentials.refresh(object())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(credentials.refresh, object())
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
    assert credentials.token == first_rotated_access_token


def test_google_before_request_serializes_validity_check_with_refresh(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A delayed same-wave request must observe the first rotation before deciding to refresh."""
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
    provider_entered = threading.Event()
    release_provider = threading.Event()
    delayed_observed_expired = threading.Event()
    provider_calls = 0
    rotated_access_token = "rotated-access-token"  # noqa: S105

    def rotate(credentials: object, _request: object) -> None:
        nonlocal provider_calls
        provider_calls += 1
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        credentials.token = rotated_access_token  # type: ignore[attr-defined]
        credentials.expiry = datetime.fromtimestamp(4_102_444_800.0, tz=UTC)  # type: ignore[attr-defined]

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", rotate)
    real_blocking_refresh = GoogleOAuthCredentials._blocking_refresh

    def observe_delayed_expiry(credentials: GoogleOAuthCredentials, request: object) -> None:
        if threading.current_thread().name == "delayed-google-request":
            delayed_observed_expired.set()
        real_blocking_refresh(credentials, request)

    monkeypatch.setattr(GoogleOAuthCredentials, "_blocking_refresh", observe_delayed_expiry)
    credentials = tool.creds

    def delayed_request() -> None:
        threading.current_thread().name = "delayed-google-request"
        credentials.before_request(object(), "GET", "https://example.test", {})

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="google-request") as executor:
        first = executor.submit(credentials.before_request, object(), "GET", "https://example.test", {})
        assert provider_entered.wait(timeout=5)
        delayed = executor.submit(delayed_request)
        try:
            assert not delayed_observed_expired.wait(timeout=0.2)
        finally:
            release_provider.set()
        first.result(timeout=5)
        delayed.result(timeout=5)

    assert provider_calls == 1
    assert credentials.token == rotated_access_token


def test_google_forced_refresh_waiting_on_valid_request_does_not_reuse_old_success(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """A forced refresh blocked by normal auth work must still replace its rejected bearer."""
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
    valid_request_entered = threading.Event()
    release_valid_request = threading.Event()

    def block_valid_request(
        _credentials: GoogleOAuthCredentials,
        _request: object,
        _method: str,
        _url: str,
        _headers: dict[str, str],
    ) -> None:
        valid_request_entered.set()
        assert release_valid_request.wait(timeout=5)

    monkeypatch.setattr(GoogleOAuthCredentials, "before_request", block_valid_request)
    real_refresh_state = oauth_client_module._GoogleRefreshState
    refresh_states: list[Any] = []

    def capture_refresh_state(*args: object, **kwargs: object) -> Any:  # noqa: ANN401
        state = real_refresh_state(*args, **kwargs)
        refresh_states.append(state)
        return state

    monkeypatch.setattr(oauth_client_module, "_GoogleRefreshState", capture_refresh_state)
    provider_calls = 0

    def rotate(credentials: object, _request: object) -> None:
        nonlocal provider_calls
        provider_calls += 1
        credentials.token = f"rotated-access-token-{provider_calls}"  # type: ignore[attr-defined]
        credentials.expiry = datetime.fromtimestamp(4_102_444_800.0, tz=UTC)  # type: ignore[attr-defined]

    monkeypatch.setattr(GoogleOAuthCredentials, "refresh", rotate)
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    credentials = tool.creds
    credentials.refresh(object())
    assert provider_calls == 1

    class ObservedRLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.failed_nonblocking_acquire = threading.Event()

        def acquire(self, blocking: bool = True) -> bool:
            acquired = self._lock.acquire(blocking)
            if not blocking and not acquired:
                self.failed_nonblocking_acquire.set()
            return acquired

        def release(self) -> None:
            self._lock.release()

        def __enter__(self) -> ObservedRLock:
            self.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self.release()

    observed_lock = ObservedRLock()
    refresh_states[0].lock = observed_lock
    with ThreadPoolExecutor(max_workers=2) as executor:
        valid_request = executor.submit(
            credentials.before_request,
            object(),
            "GET",
            "https://example.test",
            {},
        )
        assert valid_request_entered.wait(timeout=5)
        forced_refresh = executor.submit(credentials.refresh, object())
        assert observed_lock.failed_nonblocking_acquire.wait(timeout=5)
        release_valid_request.set()
        valid_request.result(timeout=5)
        forced_refresh.result(timeout=5)

    assert provider_calls == 2
    assert credentials.token == "rotated-access-token-2"  # noqa: S105


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
async def test_google_wrapper_reloads_callback_replacement_in_materialized_worker(
    runtime_paths: RuntimePaths,
) -> None:
    """A successful reconnect must invalidate credentials and services already materialized by a worker."""
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
            "token": "account-a-access-token",
            "refresh_token": "account-a-refresh-token",
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

    def exchange_account_b(
        *_args: object,
    ) -> OAuthTokenResult:
        return OAuthTokenResult(
            token_data={
                "token": "account-b-access-token",
                "refresh_token": "account-b-refresh-token",
                "expires_at": 4_102_444_800.0,
                "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            },
            claims={"sub": "account-b"},
            claims_verified=True,
        )

    callback_provider = replace(
        GoogleDriveTools._oauth_provider,
        token_exchanger=exchange_account_b,
        claim_validator=None,
        runtime_bootstrapper=None,
    )
    callback_context = replace(tool._oauth_credential_context(), provider=callback_provider)
    issued_revision = oauth_credential_generation(callback_context)
    issued_connection_generation = oauth_connection_generation(callback_context)
    worker = ThreadPoolExecutor(max_workers=1)
    try:

        def materialize_account_a_service() -> None:
            assert tool._ensure_structured_auth() is None
            tool.service = object()

        await asyncio.wrap_future(worker.submit(materialize_account_a_service))
        await exchange_and_store_oauth_credentials(
            callback_context,
            "account-b-code",
            "pkce-verifier",
            expected_connection_generation=issued_connection_generation,
        )

        def revalidate_materialized_service() -> tuple[str | None, str, object | None]:
            result = tool._ensure_structured_auth()
            return result, tool.creds.token, tool.service

        result, token, service = await asyncio.wrap_future(worker.submit(revalidate_materialized_service))
    finally:
        worker.shutdown(wait=True)

    assert oauth_credential_generation(callback_context) != issued_revision
    assert result is None
    assert token == "account-b-access-token"  # noqa: S105
    assert service is None


@pytest.mark.asyncio
async def test_google_lazy_refresh_cannot_adopt_reconnected_account(runtime_paths: RuntimePaths) -> None:
    """A retained account-A credential object must not turn into account B after reconnect."""
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
            "token": "account-a-access-token",
            "refresh_token": "account-a-refresh-token",
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
    retained_account_a = tool.creds

    def exchange_account_b(*_args: object) -> OAuthTokenResult:
        return OAuthTokenResult(
            token_data={
                "token": "account-b-access-token",
                "refresh_token": "account-b-refresh-token",
                "expires_at": 4_102_444_800.0,
                "scopes": list(GoogleDriveTools._oauth_provider.scopes),
            },
            claims={"sub": "account-b"},
            claims_verified=True,
        )

    callback_context = replace(
        tool._oauth_credential_context(),
        provider=replace(
            GoogleDriveTools._oauth_provider,
            token_exchanger=exchange_account_b,
            claim_validator=None,
            runtime_bootstrapper=None,
        ),
    )
    issued_connection_generation = oauth_connection_generation(callback_context)
    await exchange_and_store_oauth_credentials(
        callback_context,
        "account-b-code",
        "pkce-verifier",
        expected_connection_generation=issued_connection_generation,
    )

    with pytest.raises(RefreshError):
        retained_account_a.refresh(object())

    assert retained_account_a.token == "account-a-access-token"  # noqa: S105
    assert retained_account_a.refresh_token == "account-a-refresh-token"  # noqa: S105
    assert tool._ensure_structured_auth() is None
    assert tool.creds.token == "account-b-access-token"  # noqa: S105


def test_google_wrapper_full_context_key_clears_persistent_worker_between_requesters(
    runtime_paths: RuntimePaths,
) -> None:
    """Equal initial revisions for different requesters must not share one worker cache."""
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    bob_identity = replace(alice_identity, requester_id="@bob:example.org")
    alice_target = resolve_worker_target("user_agent", "general", execution_identity=alice_identity)
    bob_target = resolve_worker_target("user_agent", "general", execution_identity=bob_identity)
    for target, token in ((alice_target, "alice-token"), (bob_target, "bob-token")):
        save_scoped_credentials(
            GoogleDriveTools._oauth_provider.credential_service,
            {
                "token": token,
                "refresh_token": f"{token}-refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id",
                "expires_at": 4_102_444_800.0,
                "scopes": list(GoogleDriveTools._oauth_provider.scopes),
                "_source": "oauth",
                "_oauth_provider": GoogleDriveTools._oauth_provider.id,
            },
            credentials_manager=credentials_manager,
            worker_target=target,
        )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=alice_target,
    )

    def authenticate(identity: ToolExecutionIdentity, *, install_service: bool) -> tuple[str, bool]:
        with tool_execution_identity(identity):
            assert tool._ensure_structured_auth() is None
            service_cleared = tool.service is None
            if install_service:
                tool.service = object()
            return tool.creds.token, service_cleared

    with ThreadPoolExecutor(max_workers=1) as executor:
        alice_token, _ = executor.submit(authenticate, alice_identity, install_service=True).result(timeout=5)
        bob_token, bob_service_cleared = executor.submit(
            authenticate,
            bob_identity,
            install_service=False,
        ).result(timeout=5)

    assert alice_token == "alice-token"  # noqa: S105
    assert bob_token == "bob-token"  # noqa: S105
    assert bob_service_cleared is True


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
    payload = json.loads(tool._ensure_structured_auth() or "{}")

    assert payload["oauth_connection_required"] is True
    assert tool.creds is None
    assert tool.service is None


@pytest.mark.asyncio
async def test_google_wrapper_drops_cached_services_in_every_worker_after_reset(
    runtime_paths: RuntimePaths,
) -> None:
    """One worker observing reset must not acknowledge another worker's cached service."""
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
    workers_ready = threading.Event()
    workers_ready_lock = threading.Lock()
    ready_count = 0
    reset_complete = threading.Event()
    first_worker_revalidated = threading.Event()

    def worker_call(worker_index: int) -> tuple[bool, bool]:
        nonlocal ready_count
        assert tool._ensure_structured_auth() is None
        tool.service = object()
        with workers_ready_lock:
            ready_count += 1
            if ready_count == 2:
                workers_ready.set()
        assert reset_complete.wait(timeout=5)
        if worker_index == 1:
            assert first_worker_revalidated.wait(timeout=5)
        try:
            payload = json.loads(tool._ensure_structured_auth() or "{}")
            return payload.get("oauth_connection_required") is True, tool.service is None
        finally:
            if worker_index == 0:
                first_worker_revalidated.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker_call, worker_index) for worker_index in range(2)]
        try:
            assert await asyncio.to_thread(workers_ready.wait, 5)
            assert await reset_oauth_credentials(tool._oauth_credential_context()) is True
            reset_complete.set()
            results = await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
        finally:
            reset_complete.set()
            first_worker_revalidated.set()

    assert results == [(True, True), (True, True)]


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
    assert load_oauth_credentials(tool._oauth_credential_context()) is None


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
    successful_authenticated = threading.Event()
    release_success = threading.Event()

    def swallowed_failure() -> str:
        try:
            tool.creds.refresh(object())
        except RefreshError as exc:
            failure_recorded.set()
            assert release_failure.wait(timeout=5)
            return f"Unexpected error: {exc}"
        return "unexpected success"

    def successful_call() -> str:
        successful_authenticated.set()
        assert release_success.wait(timeout=5)
        return "success"

    tool.functions = {
        "swallowed_failure": Function(name="swallowed_failure", entrypoint=swallowed_failure),
        "successful_call": Function(name="successful_call", entrypoint=successful_call),
    }
    tool._wrap_oauth_function_entrypoints()

    with ThreadPoolExecutor(max_workers=2) as executor:
        successful_future = executor.submit(tool.successful_call)
        assert successful_authenticated.wait(timeout=5)
        failed_future = executor.submit(tool.swallowed_failure)
        assert failure_recorded.wait(timeout=5)
        release_success.set()
        successful_result = successful_future.result(timeout=5)
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


def test_google_drive_forwards_env_file_quota_project_to_service_account_auth(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Runtime env-file quota ownership must reach Agno's service-account path."""
    runtime_paths = replace(
        runtime_paths,
        env_file_values={
            **runtime_paths.env_file_values,
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json"),
            "GOOGLE_CLOUD_QUOTA_PROJECT_ID": "billing-project",
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

    assert tool.quota_project_id == "billing-project"


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
    tool._provided_credentials = None
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
    tool = object.__new__(GoogleDriveTools)
    tool._runtime_paths = runtime_paths
    tool._provided_creds = True
    tool._provided_credentials = _valid_credentials()
    tool._defer_to_original_auth = True
    tool._original_auth_completed = False
    tool.service_account_path = str(tmp_path / "service-account.json")
    tool.creds = tool._provided_credentials
    calls: list[str] = []

    def original_auth() -> None:
        calls.append("original")

    tool._original_auth = original_auth

    assert tool._ensure_structured_auth() is None
    assert calls == []


def test_google_wrapper_copies_supplied_credentials_and_mutable_scopes(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Caller mutation and refresh workers cannot alter private toolkit credentials."""
    scopes = ["scope-a"]
    granted_scopes = ["scope-a"]
    supplied = GoogleOAuthCredentials(
        token="caller-token",  # noqa: S106
        refresh_token="caller-refresh",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=scopes,
        granted_scopes=granted_scopes,
        quota_project_id="caller-project",
        expiry=datetime(2100, 1, 1, tzinfo=UTC),
    )
    supplied.with_non_blocking_refresh()

    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=supplied,
        quota_project_id="tool-project",
    )
    private = tool.creds
    supplied.token = "mutated-token"  # noqa: S105
    scopes.append("scope-b")
    granted_scopes.append("scope-b")

    assert private is not supplied
    assert private.token == "caller-token"  # noqa: S105
    assert private.scopes == ("scope-a",)
    assert private.granted_scopes == ("scope-a",)
    assert private.quota_project_id == "tool-project"
    assert private.refresh_handler is None
    assert private._use_non_blocking_refresh is False


def test_google_wrapper_rejects_supplied_refresh_handler(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Caller-controlled token brokers must not cross toolkit ownership."""
    supplied = _valid_credentials()
    supplied.refresh_handler = lambda _request, _scopes: ("token", datetime(2100, 1, 1, tzinfo=UTC))

    with pytest.raises(ValueError, match="refresh_handler"):
        GoogleDriveTools(
            runtime_paths=runtime_paths,
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            creds=supplied,
        )


def test_google_wrapper_rejects_supplied_reauth_credentials(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Gcloud-only reauth semantics are outside supported supplied credentials."""
    supplied = GoogleOAuthCredentials(
        token="token",  # noqa: S106
        enable_reauth_refresh=True,
        expiry=datetime(2100, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="reauth refresh"):
        GoogleDriveTools(
            runtime_paths=runtime_paths,
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            creds=supplied,
        )


def test_google_wrapper_rejects_supplied_subclass_and_arbitrary_object(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Only pinned concrete Google credentials enter the supplied credential boundary."""

    class CredentialSubclass(GoogleOAuthCredentials):
        pass

    rejected = (
        CredentialSubclass(token="token", expiry=datetime(2100, 1, 1, tzinfo=UTC)),  # noqa: S106
        object(),
    )
    for supplied in rejected:
        with pytest.raises(TypeError, match=r"exact google\.oauth2\.credentials\.Credentials"):
            GoogleDriveTools(
                runtime_paths=runtime_paths,
                credentials_manager=CredentialsManager(tmp_path / "credentials"),
                creds=supplied,
            )


def test_google_wrapper_supplied_credentials_lock_is_reentrant_and_serializes_workers(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Nested Drive calls reenter while independent supplied-credential calls serialize."""
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=_valid_credentials(),
    )
    tool.functions = {
        "inner": Function(name="inner", entrypoint=lambda: "inner"),
        "outer": Function(name="outer", entrypoint=lambda: tool.inner()),
    }
    tool._wrap_oauth_function_entrypoints()

    with ThreadPoolExecutor(max_workers=1) as nested_executor:
        assert nested_executor.submit(tool.outer).result(timeout=5) == "inner"

    active_calls = 0
    max_active_calls = 0
    first_entered = threading.Event()
    second_entered = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    call_count = 0

    def serialized_call() -> str:
        nonlocal active_calls, max_active_calls, call_count
        call_count += 1
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        if call_count == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        active_calls -= 1
        return "done"

    tool.functions = {"serialized": Function(name="serialized", entrypoint=serialized_call)}
    tool._wrap_oauth_function_entrypoints()

    def second_call() -> str:
        second_started.set()
        return tool.serialized()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(tool.serialized)
        assert first_entered.wait(timeout=5)
        second = executor.submit(second_call)
        assert second_started.wait(timeout=5)
        try:
            assert not second_entered.wait(timeout=0.1)
            assert max_active_calls == 1
        finally:
            release_first.set()
        assert first.result(timeout=5) == "done"
        assert second.result(timeout=5) == "done"

    assert max_active_calls == 1


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
        creds=_valid_credentials(),
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
            creds=_valid_credentials(),
            max_read_size=max_read_size,
        )
