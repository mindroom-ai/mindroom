"""Tests for the Google Calendar OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.oauth.google_calendar import GOOGLE_CALENDAR_OAUTH_SCOPES, google_calendar_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            "GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
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
        tool._auth()

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


def test_google_calendar_loads_tokens_from_oauth_service(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials("google_calendar", {"calendar_id": "primary", "_source": "ui"})
    credentials_manager.save_credentials(
        "google_calendar_oauth",
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
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


def test_google_calendar_default_config_disables_write_methods(tmp_path: Path) -> None:
    tool = get_tool_by_name(
        "google_calendar",
        _runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        disable_sandbox_proxy=True,
    )

    assert isinstance(tool, GoogleCalendarTools)
    assert "create_event" not in tool.functions
    assert "update_event" not in tool.functions
    assert "delete_event" not in tool.functions
    assert "quick_add_event" not in tool.functions
    assert "move_event" not in tool.functions
    assert "respond_to_event" not in tool.functions
    assert "https://www.googleapis.com/auth/calendar" in tool.scopes


def test_google_calendar_allow_update_enables_write_methods(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials("google_calendar", {"allow_update": True, "_source": "ui"})

    tool = get_tool_by_name(
        "google_calendar",
        _runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
        disable_sandbox_proxy=True,
    )

    assert isinstance(tool, GoogleCalendarTools)
    assert "create_event" in tool.functions
    assert "update_event" in tool.functions
    assert "delete_event" in tool.functions
    assert "quick_add_event" in tool.functions
    assert "move_event" in tool.functions
    assert "respond_to_event" in tool.functions
    assert "https://www.googleapis.com/auth/calendar" in tool.scopes


def test_google_calendar_provider_uses_write_scope_for_method_gated_tools() -> None:
    provider = google_calendar_oauth_provider()

    assert provider.scopes == GOOGLE_CALENDAR_OAUTH_SCOPES
    assert "https://www.googleapis.com/auth/calendar" in provider.scopes


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
