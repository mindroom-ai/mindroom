"""Custom Gmail Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GmailTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.google.auth import google_authenticate
from agno.tools.google.gmail import GmailTools as AgnoGmailTools
from agno.tools.google.gmail import validate_email
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_gmail import google_gmail_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_authenticate = google_authenticate("gmail")
_GMAIL_PROFILE_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
    },
)
_GMAIL_SEND_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    },
)


class GmailTools(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, AgnoGmailTools):
    """Gmail tools wrapper that uses MindRoom's credential management."""

    _oauth_provider = google_gmail_oauth_provider()
    _oauth_tool_name = "gmail"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
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
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            config=runtime_config,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )

        # Pass credentials to parent class
        super().__init__(creds=creds, **kwargs)
        self.register(self.send_email_to_self)
        if self.functions.get("send_email_to_self") is not None and (
            not _GMAIL_PROFILE_SCOPES.intersection(self.scopes) or not _GMAIL_SEND_SCOPES.intersection(self.scopes)
        ):
            self.functions.pop("send_email_to_self")
            logger.warning("gmail_send_email_to_self_disabled_missing_scope")

        # Store original auth method for fallback
        self._set_original_auth(AgnoGmailTools._resolve_creds)
        self._wrap_oauth_function_entrypoints()

    def _check_tools_filters(
        self,
        available_tools: list[str],
        include_tools: list[str] | None = None,
        exclude_tools: list[str] | None = None,
    ) -> None:
        """Include the local function in Agno's toolkit filter validation."""
        super()._check_tools_filters(
            [*available_tools, "send_email_to_self"],
            include_tools=include_tools,
            exclude_tools=exclude_tools,
        )

    @_authenticate
    def send_email_to_self(self, subject: str, body: str) -> str:
        """Send an email to the connected Gmail account.

        Args:
            subject: Email subject.
            body: Email body content.

        Returns:
            Stringified dictionary containing the sent email details.

        """
        service = self.service
        assert service is not None
        profile = service.users().getProfile(userId="me").execute()
        email_address = profile.get("emailAddress") if isinstance(profile, dict) else None
        if (
            not isinstance(email_address, str)
            or email_address != email_address.strip()
            or not validate_email(email_address)
        ):
            msg = "Connected Gmail profile did not return one valid email address"
            raise RuntimeError(msg)
        return self.send_email(to=email_address, subject=subject, body=body)

    def _build_service(self, creds: Any) -> Any:  # noqa: ANN401
        return build("gmail", "v1", http=self._google_authorized_http(creds))

    def _batch_get(
        self,
        ids: list[str],
        request_builder: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        """Execute Gmail batches while retaining final per-item authorization rejection."""
        results: list[dict[str, Any]] = []
        service = self.service
        assert service is not None

        def callback(request_id: str, response: Any, exception: Exception | None) -> None:  # noqa: ANN401
            if exception is None:
                results.append(response)
                return
            if isinstance(exception, HttpError) and exception.resp.status == 401:
                self._mark_google_authorization_rejected()
            logger.warning(
                "gmail_batch_request_failed",
                request_id=request_id,
                error_type=type(exception).__name__,
            )
            results.append({"id": request_id, "error": "Google request failed"})

        for offset in range(0, len(ids), self.max_batch_size):
            chunk = ids[offset : offset + self.max_batch_size]
            batch = service.new_batch_http_request(callback=callback)
            for item_id in chunk:
                batch.add(request_builder(item_id), request_id=item_id)
            batch.execute()
        return results
