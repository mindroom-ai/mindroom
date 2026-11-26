"""Google Sheets tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit, google_http_error_result
from mindroom.logging_config import get_logger
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_SERVICE_NAME = "Google Sheets"
_SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _resolve_toggle(
    field_name: str,
    init_arg: str,
    field_value: bool | None,
    init_value: bool | None,
) -> bool:
    """Resolve a dashboard toggle and its constructor alias, defaulting to enabled."""
    if field_value is not None and init_value is not None:
        msg = f"Google Sheets received both {field_name!r} and {init_arg!r}"
        raise ValueError(msg)
    if field_value is not None:
        return field_value
    if init_value is not None:
        return init_value
    return True


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
        read: bool | None = None,
        create: bool | None = None,
        update: bool | None = None,
        read_sheet: bool | None = None,
        create_sheet: bool | None = None,
        update_sheet: bool | None = None,
        create_duplicate_sheet: bool = False,
        all: bool = False,  # noqa: A002
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.spreadsheet_range = spreadsheet_range

        enable_read = _resolve_toggle("read", "read_sheet", read, read_sheet)
        enable_create = _resolve_toggle("create", "create_sheet", create, create_sheet)
        enable_update = _resolve_toggle("update", "update_sheet", update, update_sheet)
        if scopes is None:
            resolved_scopes: list[str] = []
            if all or enable_read:
                resolved_scopes.append(_SHEETS_READ_SCOPE)
            if all or enable_create or enable_update or create_duplicate_sheet:
                resolved_scopes.append(_SHEETS_WRITE_SCOPE)
            self.scopes = list(dict.fromkeys(resolved_scopes))
        else:
            self.scopes = list(scopes)

        tools: list[Callable[..., str]] = []
        if all or enable_read:
            tools.append(self.read_sheet)
        if all or enable_create:
            tools.append(self.create_sheet)
        if all or enable_update:
            tools.append(self.update_sheet)
        if all or create_duplicate_sheet:
            tools.append(self.create_duplicate_sheet)
        if all or enable_update:
            tools.append(self.batch_update_sheet)

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

    def read_sheet(
        self,
        spreadsheet_id: str | None = None,
        spreadsheet_range: str | None = None,
    ) -> str:
        """Read values from a Google Sheet. Prioritizes instance attributes over method parameters.

        Args:
            spreadsheet_id: Fallback spreadsheet ID if instance attribute is None.
            spreadsheet_range: Fallback range if instance attribute is None.

        Returns:
            JSON of list of rows, where each row is a list of values.

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

    def create_sheet(self, title: str) -> str:
        """Create a Google Sheet with a given title.

        Args:
            title: The title of the Google Sheet.

        Returns:
            The URL of the created Google Sheet.

        """
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

    def update_sheet(
        self,
        data: list[list[Any]],
        spreadsheet_id: str | None = None,
        range_name: str | None = None,
    ) -> str:
        """Update a Google Sheet with the provided data.

        Note: This function can overwrite existing data in the sheet.
        User needs to ensure that the provided range correctly matches the data that needs to be updated.

        Args:
            data: The data to update the sheet with.
            spreadsheet_id: The ID of the Google Sheet.
            range_name: The range of the Google Sheet to update.

        Returns:
            A message indicating the success or failure of the operation.

        """
        if not (spreadsheet_id and spreadsheet_id.strip()) or not (range_name and range_name.strip()):
            return "Spreadsheet ID and range_name must be provided in update_sheet call"

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

    def batch_update_sheet(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> str:
        """Apply Google Sheets API batchUpdate requests to one spreadsheet, in order.

        Use this for changes that update_sheet cannot make, such as cell formatting, column widths,
        frozen rows, filters, conditional formatting, merged cells, and adding or renaming sheets.
        Each request is one Sheets API Request object, for example
        {"repeatCell": {"range": {"sheetId": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": true}}}, "fields": "userEnteredFormat.textFormat.bold"}}.

        Args:
            spreadsheet_id: The ID of the Google Sheet.
            requests: Sheets API batchUpdate Request objects.

        Returns:
            JSON with the spreadsheet ID and one reply per request.

        """
        try:
            service = self._google_api_service()
            response = (
                service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
            )
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "batch_update", exc)
        return json.dumps(response)

    def create_duplicate_sheet(
        self,
        source_id: str,
        new_title: str | None = None,
        copy_permissions: bool = True,
    ) -> str:
        """Duplicate a Google Spreadsheet using the Google Drive API's copy feature.

        This ensures an exact duplicate including formatting and data.
        Requires the drive scope ``https://www.googleapis.com/auth/drive``.

        Args:
            source_id: The ID of the source spreadsheet.
            new_title: Optional new title for the duplicated spreadsheet. If not provided, the source title will be used.
            copy_permissions: Whether to copy the permissions from the source spreadsheet. Defaults to True.

        Returns:
            A link to the duplicated spreadsheet.

        """
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
