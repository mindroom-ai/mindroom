"""Tests for the Google Drive OAuth-backed tool."""

# ruff: noqa: D103, TC003

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from mindroom import constants
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target


def test_google_drive_missing_credentials_returns_connect_instruction(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    execution_identity = ToolExecutionIdentity(
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
        execution_identity=execution_identity,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert "Google Drive is not connected for this agent" in result["error"]
    assert "https://mindroom.example.test/api/oauth/google_drive/authorize?connect_token=" in result["error"]
    assert "@alice:example.org" not in result["error"]


def test_google_drive_connect_instruction_uses_redirect_uri_public_origin(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "GOOGLE_DRIVE_CLIENT_ID": "client-id",
            "GOOGLE_DRIVE_CLIENT_SECRET": "client-secret",
            "GOOGLE_DRIVE_REDIRECT_URI": "https://mindroom.example.test/api/oauth/google_drive/callback",
        },
    )
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    execution_identity = ToolExecutionIdentity(
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
        execution_identity=execution_identity,
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    result = json.loads(tool.search_files(query="name contains 'plan'", max_results=1))

    assert "https://mindroom.example.test/api/oauth/google_drive/authorize?connect_token=" in result["error"]
    assert "http://localhost:8765" not in result["error"]


def test_google_drive_credentials_restore_stored_expiry(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "GOOGLE_DRIVE_CLIENT_ID": "client-id",
            "GOOGLE_DRIVE_CLIENT_SECRET": "client-secret",
        },
    )
    tool = GoogleDriveTools(
        runtime_paths=runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )
    expires_at = datetime(2030, 1, 1, tzinfo=UTC).timestamp()

    creds = tool._credentials_from_token_data(
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": expires_at,
        },
    )

    assert creds.expiry.replace(tzinfo=UTC) == datetime(2030, 1, 1, tzinfo=UTC)
