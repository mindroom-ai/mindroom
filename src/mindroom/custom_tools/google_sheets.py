"""Google Sheets tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mindroom.config.legacy_fields import RETIRED_CONSOLIDATED_FUNCTIONS
from mindroom.custom_tools.google_service import (
    GoogleApiToolkit,
    filter_google_tools,
    google_http_error_result,
    matches_google_function,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider
from mindroom.tools.native_helpers import restrict_actions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_SERVICE_NAME = "Google Sheets"
_SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_UNSET = object()

_WriteAction = Literal["create", "update", "batch_update", "duplicate"]
_WRITE_ACTION_FIELDS: dict[str, str] = {
    "create": "title",
    "update": "spreadsheet_id range_name data",
    "batch_update": "spreadsheet_id requests",
    "duplicate": "spreadsheet_id title copy_permissions",
}


def _resolve_toggle(
    field_name: str,
    init_arg: str,
    field_value: object,
    init_value: object,
) -> bool:
    """Resolve a dashboard toggle and its constructor alias, treating explicit None as falsy."""
    if field_value is not _UNSET and init_value is not _UNSET:
        msg = f"Google Sheets received both {field_name!r} and {init_arg!r}"
        raise ValueError(msg)
    if field_value is not _UNSET:
        return bool(field_value)
    if init_value is not _UNSET:
        return bool(init_value)
    return True


def _resolve_sheets_scopes(
    scopes: Sequence[str] | None,
    *,
    has_read_tools: bool,
    has_write_tools: bool,
) -> list[str]:
    """Resolve and validate Google Sheets scopes for the enabled operations."""
    if scopes is None:
        resolved: list[str] = []
        if has_read_tools:
            resolved.append(_SHEETS_READ_SCOPE)
        if has_write_tools:
            resolved.append(_SHEETS_WRITE_SCOPE)
        return list(dict.fromkeys(resolved))

    resolved = list(scopes)
    if has_write_tools and _SHEETS_WRITE_SCOPE not in resolved:
        msg = f"The scope {_SHEETS_WRITE_SCOPE} is required for write operations"
        raise ValueError(msg)
    if has_read_tools and _SHEETS_READ_SCOPE not in resolved and _SHEETS_WRITE_SCOPE not in resolved:
        msg = f"Either {_SHEETS_READ_SCOPE} or {_SHEETS_WRITE_SCOPE} is required for read operations"
        raise ValueError(msg)
    return resolved


class GoogleSheetsTools(GoogleApiToolkit):
    """Read, create, update, and format Google Sheets with MindRoom-scoped OAuth credentials."""

    _oauth_provider = google_oauth_provider("google_sheets")
    _oauth_tool_name = "google_sheets"
    _google_api_name = "sheets"
    _google_api_version = "v4"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        scopes: Sequence[str] | None = None,
        spreadsheet_id: str | None = None,
        spreadsheet_range: str | None = None,
        read: object = _UNSET,
        create: object = _UNSET,
        update: object = _UNSET,
        read_sheet: object = _UNSET,
        create_sheet: object = _UNSET,
        update_sheet: object = _UNSET,
        create_duplicate_sheet: object = _UNSET,
        all: bool | None = False,  # noqa: A002
        include_tools: Sequence[str] | None = None,
        exclude_tools: Sequence[str] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.spreadsheet_range = spreadsheet_range

        enable_all = bool(all)
        enable_read = _resolve_toggle("read", "read_sheet", read, read_sheet)
        enable_create = _resolve_toggle("create", "create_sheet", create, create_sheet)
        enable_update = _resolve_toggle("update", "update_sheet", update, update_sheet)
        enable_duplicate = False if create_duplicate_sheet is _UNSET else bool(create_duplicate_sheet)

        write_actions: list[str] = []
        if enable_all or enable_create:
            write_actions.append("create")
        if enable_all or enable_update:
            write_actions.extend(("update", "batch_update"))
        if enable_all or enable_duplicate:
            write_actions.append("duplicate")
        self._write_actions: tuple[str, ...] = tuple(write_actions)

        tools: list[Callable[..., str]] = []
        if enable_all or enable_read:
            tools.append(self.google_sheets_read)
        if self._write_actions:
            tools.append(self.google_sheets_write)
        tools = filter_google_tools(
            tools,
            include_tools,
            exclude_tools,
            retired_tools=RETIRED_CONSOLIDATED_FUNCTIONS["google_sheets"],
            known_tools=("google_sheets_read", "google_sheets_write"),
        )

        registered_names = {getattr(tool, "__name__", "") for tool in tools}
        self.scopes = _resolve_sheets_scopes(
            scopes,
            has_read_tools="google_sheets_read" in registered_names,
            has_write_tools="google_sheets_write" in registered_names,
        )

        super().__init__(
            name="google_sheets",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )

    def _service_account_scopes(self) -> tuple[str, ...]:
        """Return Sheets service-account scopes matching enabled operations or explicit scopes."""
        return tuple(self.scopes)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``google_sheets_write``'s action enum and parameters to enabled actions."""
        if matches_google_function(function.name, "google_sheets_write"):
            return restrict_actions(function, self._write_actions, action_fields=_WRITE_ACTION_FIELDS)
        return function

    def google_sheets_read(
        self,
        spreadsheet_id: str | None = None,
        spreadsheet_range: str | None = None,
    ) -> str:
        """Read values from a Google Sheet range as a JSON list of rows.

        Configured toolkit `spreadsheet_id` and `spreadsheet_range` override call arguments when set.
        """
        sheet_id = self.spreadsheet_id or spreadsheet_id
        sheet_range = self.spreadsheet_range or spreadsheet_range
        if not sheet_id or not sheet_range:
            return "Spreadsheet ID and range must be provided either in constructor or method call"

        try:
            service = self._google_api_service()
            result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=sheet_range).execute()
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "read_sheet", exc)
        except Exception as exc:
            logger.warning("google_sheets_read_failed", error_type=type(exc).__name__)
            return f"Error reading Google Sheet: {exc}"
        return json.dumps(result.get("values", []))

    def _create_sheet(self, title: str | None) -> str:
        if not (title and title.strip()):
            return "title must be provided for create"
        try:
            service = self._google_api_service()
            spreadsheet = (
                service.spreadsheets().create(body={"properties": {"title": title}}, fields="spreadsheetId").execute()
            )
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "create_sheet", exc)
        except Exception as exc:
            logger.warning("google_sheets_create_failed", error_type=type(exc).__name__)
            return f"Error creating Google Sheet: {exc}"
        spreadsheet_id = spreadsheet.get("spreadsheetId")
        return f"Spreadsheet created: https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    def _update_sheet(
        self,
        spreadsheet_id: str | None,
        range_name: str | None,
        data: list[list[Any]] | None,
    ) -> str:
        if not (spreadsheet_id and spreadsheet_id.strip()) or not (range_name and range_name.strip()):
            return "Spreadsheet ID and range_name must be provided in update call"
        if data is None:
            return "data must be provided in update call"

        try:
            service = self._google_api_service()
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="RAW",
                body={"values": data},
            ).execute()
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "update_sheet", exc)
        except Exception as exc:
            logger.warning("google_sheets_update_failed", error_type=type(exc).__name__)
            return f"Error updating Google Sheet: {exc}"
        return f"Sheet updated successfully: {spreadsheet_id}"

    def _batch_update_sheet(
        self,
        spreadsheet_id: str | None,
        requests: list[dict[str, Any]] | None,
    ) -> str:
        if not (spreadsheet_id and spreadsheet_id.strip()):
            return "spreadsheet_id must be provided for batch_update"
        if requests is None:
            return "requests must be provided for batch_update"
        try:
            service = self._google_api_service()
            response = (
                service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
            )
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "batch_update", exc)
        return json.dumps(response)

    def _duplicate_sheet(
        self,
        source_id: str | None,
        new_title: str | None,
        copy_permissions: bool,
    ) -> str:
        if not (source_id and source_id.strip()):
            return "spreadsheet_id must be provided for duplicate"
        try:
            service = self._google_api_service()
            drive_service = build(
                "drive",
                "v3",
                http=self._google_authorized_http(self.creds),
                cache_discovery=False,
            )
            if not new_title:
                source_sheet = service.spreadsheets().get(spreadsheetId=source_id).execute()
                new_title = source_sheet["properties"]["title"]

            new_file = drive_service.files().copy(fileId=source_id, body={"name": new_title}).execute()
            new_spreadsheet_id = new_file.get("id")

            if copy_permissions:
                source_permissions = (
                    drive_service.permissions()
                    .list(fileId=source_id, fields="permissions(emailAddress,role,type)")
                    .execute()
                    .get("permissions", [])
                )
                for permission in source_permissions:
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
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "create_duplicate_sheet", exc)
        except Exception as exc:
            logger.warning("google_sheets_duplicate_failed", error_type=type(exc).__name__)
            return f"Error duplicating spreadsheet via Drive API: {exc}"
        return f"Spreadsheet duplicated successfully: https://docs.google.com/spreadsheets/d/{new_spreadsheet_id}"

    def google_sheets_write(
        self,
        action: _WriteAction,
        spreadsheet_id: str | None = None,
        title: str | None = None,
        range_name: str | None = None,
        data: list[list[Any]] | None = None,
        requests: list[dict[str, Any]] | None = None,
        copy_permissions: bool = True,
    ) -> str:
        """Create, update, format, or duplicate Google Sheets.

        - `create`: new spreadsheet with `title`
        - `update`: overwrite `spreadsheet_id` at `range_name` with literal 2D `data` (ignores configured read defaults)
        - `batch_update`: apply Sheets API `batchUpdate` `requests` to `spreadsheet_id` in order (formatting, widths, frozen rows, filters, sheet edits)
        - `duplicate`: copy `spreadsheet_id` via Drive API (`title`, `copy_permissions`; requires Drive scope)
        """
        if action not in self._write_actions:
            return f"Error: Google Sheets action {action!r} is disabled."
        if action == "create":
            return self._create_sheet(title)
        if action == "update":
            return self._update_sheet(spreadsheet_id, range_name, data)
        if action == "batch_update":
            return self._batch_update_sheet(spreadsheet_id, requests)
        if action == "duplicate":
            return self._duplicate_sheet(spreadsheet_id, title, copy_permissions)
        return f"Error: Unsupported Google Sheets write action {action!r}."
