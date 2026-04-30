"""Tests for Google OAuth credentials in isolated worker runtimes."""

# ruff: noqa: D103, S105

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import constants
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.google_gmail import google_gmail_oauth_provider
from mindroom.oauth.google_sheets import google_sheets_oauth_provider

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.oauth.providers import OAuthProvider


@pytest.mark.parametrize(
    ("provider", "tool_cls", "client_id_env", "client_secret_env"),
    [
        (
            google_drive_oauth_provider(),
            GoogleDriveTools,
            "MINDROOM_OAUTH_GOOGLE_DRIVE_CLIENT_ID",
            "MINDROOM_OAUTH_GOOGLE_DRIVE_CLIENT_SECRET",
        ),
        (
            google_calendar_oauth_provider(),
            GoogleCalendarTools,
            "MINDROOM_OAUTH_GOOGLE_CALENDAR_CLIENT_ID",
            "MINDROOM_OAUTH_GOOGLE_CALENDAR_CLIENT_SECRET",
        ),
        (
            google_sheets_oauth_provider(),
            GoogleSheetsTools,
            "MINDROOM_OAUTH_GOOGLE_SHEETS_CLIENT_ID",
            "MINDROOM_OAUTH_GOOGLE_SHEETS_CLIENT_SECRET",
        ),
        (
            google_gmail_oauth_provider(),
            GmailTools,
            "MINDROOM_OAUTH_GOOGLE_GMAIL_CLIENT_ID",
            "MINDROOM_OAUTH_GOOGLE_GMAIL_CLIENT_SECRET",
        ),
        (
            google_drive_oauth_provider(),
            GoogleDriveTools,
            "GOOGLE_DRIVE_CLIENT_ID",
            "GOOGLE_DRIVE_CLIENT_SECRET",
        ),
        (
            google_calendar_oauth_provider(),
            GoogleCalendarTools,
            "GOOGLE_CALENDAR_CLIENT_ID",
            "GOOGLE_CALENDAR_CLIENT_SECRET",
        ),
        (
            google_sheets_oauth_provider(),
            GoogleSheetsTools,
            "GOOGLE_SHEETS_CLIENT_ID",
            "GOOGLE_SHEETS_CLIENT_SECRET",
        ),
        (
            google_gmail_oauth_provider(),
            GmailTools,
            "GOOGLE_GMAIL_CLIENT_ID",
            "GOOGLE_GMAIL_CLIENT_SECRET",
        ),
        (
            google_drive_oauth_provider(),
            GoogleDriveTools,
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
        ),
    ],
)
def test_isolated_runtime_keeps_google_oauth_client_secret(
    tmp_path: Path,
    provider: OAuthProvider,
    tool_cls: type[Any],
    client_id_env: str,
    client_secret_env: str,
) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            client_id_env: "client-id",
            client_secret_env: "client-secret",
        },
    )
    isolated_runtime_paths = constants.isolated_runtime_paths(runtime_paths)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        provider.credential_service,
        {
            "token": "access-token",
            "refresh_token": "refresh-token",
            "scopes": list(provider.scopes),
            "expires_at": datetime(2030, 1, 1, tzinfo=UTC).timestamp(),
            "_source": "oauth",
        },
    )

    tool = tool_cls(
        runtime_paths=isolated_runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    assert isolated_runtime_paths.env_value(client_secret_env) == "client-secret"
    assert tool.creds is not None
    assert tool.creds.client_secret == "client-secret"
    assert client_secret_env not in constants.sandbox_execution_runtime_env_values(
        isolated_runtime_paths,
    )
    assert client_secret_env not in constants.sandbox_shell_execution_runtime_env_values(
        isolated_runtime_paths,
        extra_env_passthrough="*",
        process_env=isolated_runtime_paths.process_env,
    )


def test_isolated_runtime_keeps_google_oauth_redirect_uri(tmp_path: Path) -> None:
    runtime_paths = constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "GOOGLE_DRIVE_CLIENT_ID": "client-id",
            "GOOGLE_DRIVE_CLIENT_SECRET": "client-secret",
            "GOOGLE_DRIVE_REDIRECT_URI": "https://mindroom.example/api/oauth/google_drive/callback",
        },
    )

    isolated_runtime_paths = constants.isolated_runtime_paths(runtime_paths)

    assert isolated_runtime_paths.env_value("GOOGLE_DRIVE_REDIRECT_URI") == (
        "https://mindroom.example/api/oauth/google_drive/callback"
    )
