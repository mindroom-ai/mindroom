"""Tests for the Google Calendar OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.oauth.google import google_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            **(extra_env or {}),
        },
    )


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    return resolve_worker_target("user_agent", "general", execution_identity=identity)


def test_google_calendar_missing_credentials_raises_structured_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    with pytest.raises(OAuthConnectionRequired) as exc_info:
        tool._authenticate()

    assert exc_info.value.provider_id == "google_calendar"
    assert exc_info.value.connect_url is not None
    assert "/api/oauth/google_calendar/authorize?connect_token=" in exc_info.value.connect_url
    assert "@alice:example.org" not in str(exc_info.value)


def test_google_calendar_public_method_returns_structured_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    result = json.loads(tool.list_calendars())

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_calendar"
    assert "/api/oauth/google_calendar/authorize?connect_token=" in result["connect_url"]

    result = json.loads(tool.list_events(limit=1))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_calendar"
    assert "/api/oauth/google_calendar/authorize?connect_token=" in result["connect_url"]


def test_google_calendar_loads_tokens_from_oauth_service(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials("google_calendar", {"calendar_id": "primary", "_source": "ui"})
    publish_oauth_credentials(
        google_oauth_provider("google_calendar"),
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == "access-token"  # noqa: S105
    assert "calendar_id" not in token_data


def test_google_calendar_default_config_enables_write_methods(tmp_path: Path) -> None:
    tool = get_tool_by_name(
        "google_calendar",
        _runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, GoogleCalendarTools)
    assert "create_event" in tool.functions
    assert "update_event" in tool.functions
    assert "delete_event" in tool.functions
    assert "quick_add_event" in tool.functions
    assert "move_event" in tool.functions
    assert "respond_to_event" in tool.functions
    assert "https://www.googleapis.com/auth/calendar.events" in tool._oauth_provider.scopes
    assert "https://www.googleapis.com/auth/calendar" not in tool._oauth_provider.scopes


def test_google_calendar_allow_update_enables_write_methods(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials("google_calendar", {"allow_update": True, "_source": "ui"})

    tool = get_tool_by_name(
        "google_calendar",
        _runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, GoogleCalendarTools)
    assert "create_event" in tool.functions
    assert "update_event" in tool.functions
    assert "delete_event" in tool.functions
    assert "quick_add_event" in tool.functions
    assert "move_event" in tool.functions
    assert "respond_to_event" in tool.functions
    assert "https://www.googleapis.com/auth/calendar.events" in tool._oauth_provider.scopes
    assert "https://www.googleapis.com/auth/calendar" not in tool._oauth_provider.scopes


def test_google_calendar_service_account_env_uses_upstream_auth(tmp_path: Path) -> None:
    tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(
            tmp_path,
            {"GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json")},
        ),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )

    assert tool._should_fallback_to_original_auth() is True


def test_google_calendar_instructions_name_only_offered_functions(tmp_path: Path) -> None:
    tool = get_tool_by_name(
        "google_calendar",
        _runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        tool_config_overrides={"allow_update": False},
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, GoogleCalendarTools)
    instructions = tool.instructions_for(tool.functions)
    assert "get_event" in instructions
    assert "list_events" in instructions
    assert "update_event" not in instructions
    assert "delete_event" not in instructions
    assert "quick_add_event" not in instructions


def test_google_calendar_allow_update_false_overrides_explicit_write_flags(tmp_path: Path) -> None:
    tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        allow_update=False,
        create_event=True,
        update_event=True,
        delete_event=True,
        quick_add_event=True,
        move_event=True,
        respond_to_event=True,
    )

    for write_tool in (
        "create_event",
        "update_event",
        "delete_event",
        "quick_add_event",
        "move_event",
        "respond_to_event",
    ):
        assert write_tool not in tool.functions
    assert "list_events" in tool.functions
    assert "get_event" in tool.functions


@pytest.mark.parametrize(
    "readonly_scope",
    [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    ],
)
def test_google_calendar_readonly_scopes_reject_write_operations_even_with_write_creds(
    tmp_path: Path,
    readonly_scope: str,
) -> None:
    write_capable_creds = GoogleOAuthCredentials(
        token="valid-access-token",  # noqa: S106
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("https://www.googleapis.com/auth/calendar",),
        expiry=datetime(2100, 1, 1),  # noqa: DTZ001
    )

    with pytest.raises(ValueError, match="write operations"):
        GoogleCalendarTools(
            runtime_paths=_runtime_paths(tmp_path),
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            creds=write_capable_creds,
            scopes=[readonly_scope],
        )

    read_only_tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=write_capable_creds,
        allow_update=False,
        scopes=[readonly_scope],
    )
    assert "delete_event" not in read_only_tool.functions
    assert "list_events" in read_only_tool.functions


def test_google_calendar_granular_events_write_scope_allows_writes(tmp_path: Path) -> None:
    tool = GoogleCalendarTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )

    assert "create_event" in tool.functions
    assert "delete_event" in tool.functions
