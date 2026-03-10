"""Custom Google Sheets Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleSheetsTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.googlesheets import GoogleSheetsTools as AgnoGoogleSheetsTools
from loguru import logger

from mindroom.credentials import get_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.tool_system.dependencies import ensure_tool_deps

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import WorkerScope

_GOOGLE_DEPS = ["google-auth", "google-auth-oauthlib"]


class GoogleSheetsTools(AgnoGoogleSheetsTools):
    """Google Sheets tools wrapper that uses MindRoom's credential management."""

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
        self._worker_scope = worker_scope
        self._routing_agent_name = routing_agent_name
        provided_creds = kwargs.pop("creds", None)
        self._provided_creds = provided_creds is not None
        creds = provided_creds or self._load_stored_credentials()

        # Pass credentials to parent class
        super().__init__(creds=creds, **kwargs)

        # Store original auth method for fallback
        self._original_auth = super()._auth

    def _load_token_data(self) -> dict[str, Any] | None:
        """Load scoped Google OAuth credentials for the current execution."""
        return load_scoped_credentials(
            "google",
            worker_scope=self._worker_scope,
            routing_agent_name=self._routing_agent_name,
            credentials_manager=self._creds_manager,
        )

    def _save_token_data(self, token_data: dict[str, Any]) -> None:
        """Persist refreshed Google OAuth credentials to the current execution scope."""
        save_scoped_credentials(
            "google",
            token_data,
            worker_scope=self._worker_scope,
            routing_agent_name=self._routing_agent_name,
            credentials_manager=self._creds_manager,
        )

    def _build_credentials(self, token_data: dict[str, Any]) -> Any:  # noqa: ANN401
        """Create a Google Credentials object from stored token data."""
        ensure_tool_deps(_GOOGLE_DEPS, "google_sheets")
        from google.oauth2.credentials import Credentials  # noqa: PLC0415

        return Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes", self.DEFAULT_SCOPES),
        )

    def _load_stored_credentials(self) -> Any:  # noqa: ANN401
        """Load stored credentials for the current execution scope."""
        token_data = self._load_token_data()
        if not token_data:
            return None

        try:
            creds = self._build_credentials(token_data)
        except Exception as e:
            logger.error(f"Failed to load Google Sheets credentials: {e}")
            return None

        logger.info("Loaded Google Sheets credentials from MindRoom storage")
        return creds

    def _auth(self) -> None:
        """Custom auth method that uses MindRoom's credential storage."""
        if self._provided_creds and self.creds and self.creds.valid:
            return

        service_account_path = self.service_account_path
        if service_account_path:
            self.creds = None
            self._original_auth()
            return

        token_data = self._load_token_data()
        if token_data:
            try:
                ensure_tool_deps(_GOOGLE_DEPS, "google_sheets")
                from google.auth.transport.requests import Request  # noqa: PLC0415

                self.creds = self._build_credentials(token_data)

                # Refresh if expired
                if self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())

                    # Save the refreshed credentials back
                    token_data["token"] = self.creds.token
                    self._save_token_data(token_data)

                logger.info("Google Sheets authentication successful")
            except Exception as e:
                logger.error(f"Failed to authenticate with Google Sheets: {e}")
                raise
        else:
            # If no credentials found, fall back to original auth method
            # This will prompt for OAuth flow
            self.creds = None
            logger.warning("No stored credentials found, initiating OAuth flow")
            self._original_auth()
