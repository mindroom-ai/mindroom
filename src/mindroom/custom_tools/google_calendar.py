"""Custom Google Calendar Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleCalendarTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.googlecalendar import GoogleCalendarTools as AgnoGoogleCalendarTools

from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_calendar import google_calendar_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class GoogleCalendarTools(ScopedOAuthClientMixin, AgnoGoogleCalendarTools):
    """Google Calendar tools wrapper that uses MindRoom's credential management."""

    _oauth_provider = google_calendar_oauth_provider()
    _oauth_tool_name = "google_calendar"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Calendar tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleCalendarTools.
        """
        provided_creds = kwargs.pop("creds", None)
        allow_update = kwargs.get("allow_update") is True
        kwargs.update(
            {
                "create_event": allow_update,
                "update_event": allow_update,
                "delete_event": allow_update,
                "quick_add_event": allow_update,
                "move_event": allow_update,
                "respond_to_event": allow_update,
            },
        )
        if credentials_manager is None:
            msg = "GoogleCalendarTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            provided_creds=provided_creds,
            logger=logger,
        )

        super().__init__(**kwargs)
        self.creds = creds

        # Store original auth method for fallback
        self._set_original_auth(AgnoGoogleCalendarTools._auth)
        self._wrap_oauth_function_entrypoints()

    def _should_fallback_to_original_auth(self) -> bool:
        """Prefer the upstream auth path when a service account is configured."""
        return bool(self.service_account_path or self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))
