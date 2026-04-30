"""Custom Google Sheets Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleSheetsTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.googlesheets import GoogleSheetsTools as AgnoGoogleSheetsTools

from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_sheets import google_sheets_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class GoogleSheetsTools(ScopedOAuthClientMixin, AgnoGoogleSheetsTools):
    """Google Sheets tools wrapper that uses MindRoom's credential management."""

    _oauth_provider = google_sheets_oauth_provider()
    _oauth_tool_name = "google_sheets"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Sheets tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleSheetsTools.
        """
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GoogleSheetsTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
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
