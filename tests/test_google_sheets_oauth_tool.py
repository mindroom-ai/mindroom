"""Tests for the Google Sheets OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            "GOOGLE_SHEETS_CLIENT_ID": "client-id",
            "GOOGLE_SHEETS_CLIENT_SECRET": "client-secret",
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


def test_google_sheets_missing_credentials_raises_structured_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    with pytest.raises(OAuthConnectionRequired) as exc_info:
        tool._auth()

    assert exc_info.value.provider_id == "google_sheets"
    assert exc_info.value.connect_url is not None
    assert "/api/oauth/google_sheets/authorize?connect_token=" in exc_info.value.connect_url
    assert "@alice:example.org" not in str(exc_info.value)


def test_google_sheets_loads_tokens_from_oauth_service(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials("google_sheets", {"spreadsheet_id": "sheet-id", "_source": "ui"})
    credentials_manager.save_credentials(
        "google_sheets_oauth",
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
    )
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == "access-token"  # noqa: S105
    assert "spreadsheet_id" not in token_data
