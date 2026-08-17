"""Custom Gmail Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GmailTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.google.gmail import GmailTools as AgnoGmailTools
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin, google_service_account_configured
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_gmail import google_gmail_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.auth import AuthorizationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


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
        authorization: AuthorizationConfig | None = None,
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
            authorization=authorization,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )

        # Pass credentials to parent class
        super().__init__(creds=creds, **kwargs)

        # Store original auth method for fallback
        self._set_original_auth(AgnoGmailTools._auth)
        self._wrap_oauth_function_entrypoints()

    def _should_fallback_to_original_auth(self) -> bool:
        return google_service_account_configured(self.service_account_path, self._runtime_paths)

    def _build_service(self) -> Any:  # noqa: ANN401
        return build("gmail", "v1", http=self._google_authorized_http(self.creds))

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
