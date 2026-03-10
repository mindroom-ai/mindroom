"""Tests for Google-backed custom tool wrappers."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.custom_tools.gmail import GmailTools
from mindroom.custom_tools.google_calendar import GoogleCalendarTools
from mindroom.custom_tools.google_sheets import GoogleSheetsTools


@pytest.mark.parametrize("worker_scope", ["user", "user_agent", "room_thread"])
@pytest.mark.parametrize("tool_class", [GmailTools, GoogleCalendarTools, GoogleSheetsTools])
def test_google_wrappers_reject_isolating_worker_scopes(
    worker_scope: str,
    tool_class: type[Any],
) -> None:
    """Google-backed tools are intentionally unsupported for isolating worker scopes."""
    with pytest.raises(ValueError, match="worker_scope=shared"):
        tool_class(worker_scope=worker_scope, routing_agent_name="general")
