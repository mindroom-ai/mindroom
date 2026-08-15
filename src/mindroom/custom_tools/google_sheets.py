"""Custom Google Sheets Tools wrapper for MindRoom.

This module provides a wrapper around Agno's GoogleSheetsTools that properly handles
credentials stored in MindRoom's unified credentials location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.google.sheets import GoogleSheetsTools as AgnoGoogleSheetsTools
from agno.tools.google.sheets import authenticate
from googleapiclient.discovery import build

from mindroom.custom_tools.google_service import ThreadLocalGoogleServiceMixin, google_service_account_configured
from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin
from mindroom.oauth.google_sheets import google_sheets_oauth_provider

if TYPE_CHECKING:
    from mindroom.config.auth import AuthorizationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_CONFIG_FIELD_INIT_ARG_ALIASES = {
    "read": "read_sheet",
    "create": "create_sheet",
    "update": "update_sheet",
}


class GoogleSheetsTools(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, AgnoGoogleSheetsTools):
    """Google Sheets tools wrapper that uses MindRoom's credential management."""

    _oauth_provider = google_sheets_oauth_provider()
    _oauth_tool_name = "google_sheets"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        authorization: AuthorizationConfig | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Initialize Google Sheets tools with MindRoom credentials.

        This wrapper automatically loads credentials from MindRoom's
        unified credential storage and passes them to the Agno GoogleSheetsTools.
        """
        provided_creds = kwargs.pop("creds", None)
        self._normalize_dashboard_config_kwargs(kwargs)
        if credentials_manager is None:
            msg = "GoogleSheetsTools requires an explicit credentials_manager"
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
        self._set_original_auth(AgnoGoogleSheetsTools._auth)
        self._wrap_oauth_function_entrypoints()

    def _should_fallback_to_original_auth(self) -> bool:
        return google_service_account_configured(self.service_account_path, self._runtime_paths)

    def _build_service(self) -> Any:  # noqa: ANN401
        return build("sheets", "v4", http=self._google_authorized_http(self.creds))

    def _build_drive_service(self) -> Any:  # noqa: ANN401
        """Build the secondary Drive client through the same OAuth transport boundary."""
        return build("drive", "v3", http=self._google_authorized_http(self.creds))

    @authenticate
    def create_duplicate_sheet(
        self,
        source_id: str,
        new_title: str | None = None,
        copy_permissions: bool = True,
    ) -> str:
        """Duplicate one spreadsheet while retaining structured OAuth rejection."""
        if not self.creds:
            return "Not authenticated. Call auth() first."
        if not self.service:
            return "Service not initialized"

        try:
            drive_scope = "https://www.googleapis.com/auth/drive"
            if drive_scope not in self.scopes:
                self.scopes.append(drive_scope)
                self._auth()

            drive_service = self._build_drive_service()
            if not new_title:
                source_sheet = self.service.spreadsheets().get(spreadsheetId=source_id).execute()
                new_title = source_sheet["properties"]["title"]

            new_file = drive_service.files().copy(fileId=source_id, body={"name": new_title}).execute()
            new_spreadsheet_id = new_file.get("id")
            if copy_permissions:
                permissions = (
                    drive_service.permissions()
                    .list(fileId=source_id, fields="permissions(emailAddress,role,type)")
                    .execute()
                    .get("permissions", [])
                )
                for permission in permissions:
                    if permission.get("role") == "owner":
                        continue
                    drive_service.permissions().create(
                        fileId=new_spreadsheet_id,
                        body={
                            "role": permission.get("role"),
                            "type": permission.get("type"),
                            "emailAddress": permission.get("emailAddress"),
                        },
                    ).execute()

        except Exception as exc:
            return f"Error duplicating spreadsheet via Drive API: {exc}"
        else:
            return f"Spreadsheet duplicated successfully: https://docs.google.com/spreadsheets/d/{new_spreadsheet_id}"

    def _normalize_dashboard_config_kwargs(self, kwargs: dict[str, Any]) -> None:
        """Map dashboard field names onto Agno's constructor argument names."""
        for field_name, init_arg in _CONFIG_FIELD_INIT_ARG_ALIASES.items():
            if field_name not in kwargs and init_arg not in kwargs:
                kwargs[init_arg] = True
                continue
            if field_name not in kwargs:
                continue
            if init_arg in kwargs:
                msg = f"Google Sheets received both {field_name!r} and {init_arg!r}"
                raise ValueError(msg)
            kwargs[init_arg] = kwargs.pop(field_name)
