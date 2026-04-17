"""Custom Google Calendar Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleCalendarTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.googlecalendar import GoogleCalendarTools as AgnoGoogleCalendarTools

from mindroom.custom_tools._google_oauth import ScopedGoogleOAuthMixin
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class GoogleCalendarTools(ScopedGoogleOAuthMixin, AgnoGoogleCalendarTools):
    """Google Calendar tools wrapper that uses MindRoom's credential management."""

    _oauth_tool_name = "google_calendar"
    _oauth_log_name = "Google Calendar"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        allowed_shared_services: frozenset[str] | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Calendar tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleCalendarTools.
        """
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GoogleCalendarTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        creds = self._initialize_google_oauth(
            allowed_shared_services=allowed_shared_services,
            worker_target=worker_target,
            provided_creds=provided_creds,
            logger=logger,
        )

        super().__init__(**kwargs)
        self.creds = creds

        # Store original auth method for fallback
        self._set_original_auth(AgnoGoogleCalendarTools._auth)
