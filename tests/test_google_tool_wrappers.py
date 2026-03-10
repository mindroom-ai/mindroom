"""Tests for Google-backed custom tool wrappers using scoped credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    get_tool_execution_identity,
    tool_execution_identity,
)


@dataclass
class _FakeGoogleCreds:
    """Minimal credential object for exercising wrapper auth flows."""

    token: str
    refresh_token: str | None = None
    expired: bool = False
    valid: bool = True

    def refresh(self, _request: object) -> None:
        """Simulate token refresh by mutating the token value."""
        self.token = f"{self.token}-refreshed"
        self.expired = False
        self.valid = True


def _worker_identity(requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-123",
        tenant_id="tenant-123",
        account_id="account-456",
    )


@pytest.mark.parametrize(
    ("module_path", "tool_class"),
    [
        ("mindroom.custom_tools.gmail", GmailTools),
        ("mindroom.custom_tools.google_calendar", GoogleCalendarTools),
        ("mindroom.custom_tools.google_sheets", GoogleSheetsTools),
    ],
)
def test_google_wrappers_reload_scoped_credentials_per_requester(
    module_path: str,
    tool_class: type[Any],
) -> None:
    """A shared tool instance must reload credentials when the requester changes."""
    manager = MagicMock()

    def fake_load_scoped_credentials(_service: str, **_kwargs: object) -> dict[str, Any] | None:
        identity = get_tool_execution_identity()
        if identity is None:
            return None
        if identity.requester_id == "@alice:example.org":
            return {"token": "alice-token"}
        return {"token": "bob-token"}

    with (
        patch(f"{module_path}.get_credentials_manager", return_value=manager),
        patch(f"{module_path}.load_scoped_credentials", side_effect=fake_load_scoped_credentials),
        patch(f"{module_path}.ensure_tool_deps"),
    ):
        tool = tool_class(worker_scope="user", routing_agent_name="general")
        with patch.object(
            tool,
            "_build_credentials",
            side_effect=lambda token_data: _FakeGoogleCreds(token=token_data["token"]),
        ):
            with tool_execution_identity(_worker_identity("@alice:example.org")):
                tool._auth()
                assert tool.creds.token == "alice-token"  # noqa: S105

            with tool_execution_identity(_worker_identity("@bob:example.org")):
                tool._auth()
                assert tool.creds.token == "bob-token"  # noqa: S105


@pytest.mark.parametrize(
    ("module_path", "tool_class"),
    [
        ("mindroom.custom_tools.gmail", GmailTools),
        ("mindroom.custom_tools.google_calendar", GoogleCalendarTools),
        ("mindroom.custom_tools.google_sheets", GoogleSheetsTools),
    ],
)
def test_google_wrappers_refresh_tokens_in_scoped_store(
    module_path: str,
    tool_class: type[Any],
) -> None:
    """Refreshed Google tokens must persist back to the current worker scope."""
    manager = MagicMock()
    token_data = {"token": "alice-token", "refresh_token": "refresh-token"}
    fake_creds = _FakeGoogleCreds(
        token="alice-token",  # noqa: S106
        refresh_token="refresh-token",  # noqa: S106
        expired=True,
        valid=False,
    )

    def fake_load_scoped_credentials(_service: str, **_kwargs: object) -> dict[str, Any] | None:
        return token_data.copy() if get_tool_execution_identity() is not None else None

    with (
        patch(f"{module_path}.get_credentials_manager", return_value=manager),
        patch(f"{module_path}.load_scoped_credentials", side_effect=fake_load_scoped_credentials),
        patch(f"{module_path}.save_scoped_credentials") as mock_save_scoped_credentials,
        patch(f"{module_path}.ensure_tool_deps"),
    ):
        tool = tool_class(worker_scope="user", routing_agent_name="general")
        with (
            patch.object(tool, "_build_credentials", return_value=fake_creds),
            tool_execution_identity(_worker_identity("@alice:example.org")),
        ):
            tool._auth()

    mock_save_scoped_credentials.assert_called_once_with(
        "google",
        {"token": "alice-token-refreshed", "refresh_token": "refresh-token"},
        worker_scope="user",
        routing_agent_name="general",
        credentials_manager=manager,
    )
