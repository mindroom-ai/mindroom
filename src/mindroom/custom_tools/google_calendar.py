"""Custom Google Calendar Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleCalendarTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from typing import Any

from agno.tools.googlecalendar import GoogleCalendarTools as AgnoGoogleCalendarTools
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from loguru import logger

from mindroom.credentials import get_credentials_manager


class GoogleCalendarTools(AgnoGoogleCalendarTools):
    """Google Calendar tools wrapper that uses MindRoom's credential management."""

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Initialize Google Calendar tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleCalendarTools.
        """
        # Load credentials using the credentials manager
        creds_manager = get_credentials_manager()
        token_data = creds_manager.load_credentials("google")
        creds = None

        if token_data:
            try:
                # Create Google Credentials object from stored data
                creds = Credentials(
                    token=token_data.get("token"),
                    refresh_token=token_data.get("refresh_token"),
                    token_uri=token_data.get("token_uri"),
                    client_id=token_data.get("client_id"),
                    client_secret=token_data.get("client_secret"),
                    scopes=token_data.get("scopes", self.DEFAULT_SCOPES),
                )
                logger.info("Loaded Google Calendar credentials from MindRoom storage")
            except Exception as e:
                logger.error(f"Failed to load Google Calendar credentials: {e}")
                creds = None
        else:
            logger.warning("Google Calendar credentials not found in MindRoom storage")

        # Pass credentials to parent class
        super().__init__(creds=creds, **kwargs)

        # Store original auth method for fallback
        self._original_auth = super()._auth

    def _auth(self) -> None:
        """Custom auth method that uses MindRoom's credential storage."""
        # If we already have valid credentials, don't re-authenticate
        if self.creds and self.creds.valid:
            return

        # Reload credentials from MindRoom's storage in case they've been updated
        creds_manager = get_credentials_manager()
        token_data = creds_manager.load_credentials("google")

        if token_data:
            try:
                self.creds = Credentials(
                    token=token_data.get("token"),
                    refresh_token=token_data.get("refresh_token"),
                    token_uri=token_data.get("token_uri"),
                    client_id=token_data.get("client_id"),
                    client_secret=token_data.get("client_secret"),
                    scopes=token_data.get("scopes", self.DEFAULT_SCOPES),
                )

                # Refresh if expired
                if self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())

                    # Save the refreshed credentials back
                    token_data["token"] = self.creds.token
                    creds_manager.save_credentials("google", token_data)

                logger.info("Google Calendar authentication successful")
            except Exception as e:
                logger.error(f"Failed to authenticate with Google Calendar: {e}")
                raise
        else:
            # If no credentials found, fall back to original auth method
            # This will prompt for OAuth flow
            logger.warning("No stored credentials found, initiating OAuth flow")
            self._original_auth()
