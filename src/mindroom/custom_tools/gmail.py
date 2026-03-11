"""Custom Gmail Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GmailTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.gmail import GmailTools as AgnoGmailTools
from loguru import logger

from mindroom.credentials import get_credentials_manager
from mindroom.custom_tools.google_oauth import ScopedGoogleOAuthMixin

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import WorkerScope


class GmailTools(ScopedGoogleOAuthMixin, AgnoGmailTools):
    """Gmail tools wrapper that uses MindRoom's credential management."""

    _oauth_tool_name = "gmail"
    _oauth_log_name = "Gmail"

    def __init__(
        self,
        *,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Gmail tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GmailTools.
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
        self._set_original_auth(AgnoGmailTools._auth)
