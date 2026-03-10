"""Custom Google Calendar Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleCalendarTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.googlecalendar import GoogleCalendarTools as AgnoGoogleCalendarTools
from loguru import logger

from mindroom.credentials import get_credentials_manager
from mindroom.custom_tools._google_oauth import ScopedGoogleOAuthMixin

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import WorkerScope


class GoogleCalendarTools(ScopedGoogleOAuthMixin, AgnoGoogleCalendarTools):
    """Google Calendar tools wrapper that uses MindRoom's credential management."""

    _oauth_tool_name = "google_calendar"
    _oauth_log_name = "Google Calendar"

    def __init__(
        self,
        *,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Calendar tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleCalendarTools.
        """
        self._creds_manager = get_credentials_manager()
        creds = self._initialize_google_oauth(
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            provided_creds=kwargs.pop("creds", None),
            logger=logger,
        )

        super().__init__(**kwargs)
        self.creds = creds

        # Store original auth method for fallback
        self._set_original_auth(AgnoGoogleCalendarTools._auth)
