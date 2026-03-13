"""Tests for Google-backed custom tool wrappers."""

from __future__ import annotations

from typing import Any

import pytest

import mindroom.custom_tools._google_oauth as google_oauth_module
from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_sheets import GoogleSheetsTools


@pytest.mark.parametrize("worker_scope", ["user", "user_agent"])
@pytest.mark.parametrize("tool_class", [GmailTools, GoogleCalendarTools, GoogleSheetsTools])
def test_google_wrappers_reject_isolating_worker_scopes(
    worker_scope: str,
    tool_class: type[Any],
) -> None:
    """Google-backed tools are intentionally unsupported for isolating worker scopes."""
    with pytest.raises(ValueError, match="worker_scope=shared"):
        tool_class(worker_scope=worker_scope, routing_agent_name="general")


@pytest.mark.parametrize(
    ("tool_class", "expected_scopes"),
    [
        (
            GoogleCalendarTools,
            list(GoogleCalendarTools.DEFAULT_SCOPES.values()),
        ),
        (
            GoogleSheetsTools,
            list(GoogleSheetsTools.DEFAULT_SCOPES.values()),
        ),
    ],
)
def test_google_wrapper_build_credentials_uses_scope_urls_for_dict_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tool_class: type[Any],
    expected_scopes: list[str],
) -> None:
    """Dict-based Agno DEFAULT_SCOPES should be converted to a list of scope URLs."""
    monkeypatch.setattr(google_oauth_module, "ensure_tool_deps", lambda *_args, **_kwargs: None)

    tool = object.__new__(tool_class)
    tool._oauth_tool_name = "google"
    creds = tool._build_credentials(
        {
            "token": "token",
            "refresh_token": "refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
    )

    assert creds.scopes == expected_scopes
