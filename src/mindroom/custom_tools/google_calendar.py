"""Custom Google Calendar Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleCalendarTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.google.calendar import GoogleCalendarTools as AgnoGoogleCalendarTools
from googleapiclient.discovery import build

from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_calendar import google_calendar_oauth_provider

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


class GoogleCalendarTools(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, AgnoGoogleCalendarTools):
    """Google Calendar tools wrapper that uses MindRoom's credential management."""

    _oauth_provider = google_calendar_oauth_provider()
    _oauth_tool_name = "google_calendar"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Calendar tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleCalendarTools.
        """
        provided_creds = kwargs.pop("creds", None)
        allow_update = kwargs.get("allow_update", True) is True
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
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        creds = self._initialize_oauth_client(
            worker_target=worker_target,
            config=runtime_config,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )

        # AGNO_COMPAT: Calendar construction rejects granular OAuth scope combinations.
        # Reason: Agno requires calendar/calendar.readonly in its scope list even
        # when granular scopes cover the registered operations. Keep its default
        # construction markers and inject MindRoom's narrowly scoped credentials.
        # Upstream issue: No matching granular Calendar scope validation issue identified.
        # Upstream PR: None identified; operation-aware scope validation remains untracked.
        # Remove when: Agno accepts sufficient granular scopes during construction;
        # retain MindRoom's scope selection, credential ownership, and tool permissions.
        # Coverage: tests/test_google_calendar_oauth_tool.py::test_google_calendar_default_config_enables_write_methods;
        # tests/test_google_calendar_oauth_tool.py::test_google_calendar_provider_uses_narrow_scopes_for_every_tool_operation.
        super().__init__(**kwargs)
        self.creds = creds

        # Store original auth method for fallback
        self._set_original_auth(AgnoGoogleCalendarTools._resolve_creds)
        self._wrap_oauth_function_entrypoints()

    def _build_service(self, creds: Any) -> Any:  # noqa: ANN401
        return build("calendar", "v3", http=self._google_authorized_http(creds))
