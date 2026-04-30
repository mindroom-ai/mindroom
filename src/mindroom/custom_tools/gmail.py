"""Custom Gmail Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GmailTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.gmail import GmailTools as AgnoGmailTools

from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_gmail import google_gmail_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class GmailTools(ScopedOAuthClientMixin, AgnoGmailTools):
    """Gmail tools wrapper that uses MindRoom's credential management."""

    _oauth_provider = google_gmail_oauth_provider()
    _oauth_tool_name = "gmail"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Gmail tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GmailTools.
        """
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GmailTools requires an explicit credentials_manager"
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
        self._set_original_auth(AgnoGmailTools._auth)
        self._wrap_oauth_function_entrypoints()

    def _should_fallback_to_original_auth(self) -> bool:
        """Prefer the upstream auth path when a service account is configured."""
        return bool(self.service_account_path or self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))
