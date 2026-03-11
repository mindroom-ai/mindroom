"""Custom Google Sheets Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleSheetsTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.googlesheets import GoogleSheetsTools as AgnoGoogleSheetsTools
from loguru import logger

from mindroom.credentials import get_credentials_manager
from mindroom.custom_tools.google_oauth import ScopedGoogleOAuthMixin

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import WorkerScope


class GoogleSheetsTools(ScopedGoogleOAuthMixin, AgnoGoogleSheetsTools):
    """Google Sheets tools wrapper that uses MindRoom's credential management."""

    _oauth_tool_name = "google_sheets"
    _oauth_log_name = "Google Sheets"

    def __init__(
        self,
        *,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Sheets tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleSheetsTools.
        """
        self._creds_manager = get_credentials_manager()
        provided_creds = kwargs.pop("creds", None)
        creds = self._initialize_google_oauth(
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            provided_creds=provided_creds,
            logger=logger,
        )

        # Pass credentials to parent class
        super().__init__(creds=creds, **kwargs)

        # Store original auth method for fallback
        self._set_original_auth(AgnoGoogleSheetsTools._auth)

    def _should_fallback_to_original_auth(self) -> bool:
        """Prefer the upstream auth path when a service account is configured."""
        return bool(self.service_account_path)
