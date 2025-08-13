"""Google Sheets tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.googlesheets import GoogleSheetsTools


@register_tool_with_metadata(
    name="google_sheets",
    display_name="Google Sheets",
    description="Read, create, update, and duplicate Google Sheets spreadsheets",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    icon="FaGoogle",
    icon_color="text-green-600",
    config_fields=[
        # Authentication/OAuth Configuration
        ConfigField(
            name="scopes",
            label="OAuth Scopes",
            type="text",
            required=False,
            default=None,
            placeholder="https://www.googleapis.com/auth/spreadsheets",
            description="Custom OAuth scopes. If None, determined by operations (read-only or read-write)",
        ),
        ConfigField(
            name="creds",
            label="Pre-existing Credentials",
            type="text",
            required=False,
            default=None,
            description="Pre-existing Google OAuth credentials object",
        ),
        ConfigField(
            name="creds_path",
            label="Credentials File Path",
            type="text",
            required=False,
            default=None,
            placeholder="credentials.json",
            description="Path to credentials JSON file from Google Cloud Console",
        ),
        ConfigField(
            name="token_path",
            label="Token File Path",
            type="password",
            required=False,
            default=None,
            placeholder="token.json",
            description="Path to store OAuth token file for future authentication",
        ),
        ConfigField(
            name="oauth_port",
            label="OAuth Port",
            type="number",
            required=False,
            default=0,
            placeholder="8080",
            description="Port to use for OAuth authentication callback (0 for auto-select)",
        ),
        # Spreadsheet Configuration
        ConfigField(
            name="spreadsheet_id",
            label="Spreadsheet ID",
            type="text",
            required=False,
            default=None,
            placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
            description="ID of the target spreadsheet (from the Google Sheets URL)",
        ),
        ConfigField(
            name="spreadsheet_range",
            label="Spreadsheet Range",
            type="text",
            required=False,
            default=None,
            placeholder="Sheet1!A1:E10",
            description="Range within the spreadsheet (e.g., 'Sheet1!A1:E10')",
        ),
        # Operation Permissions
        ConfigField(
            name="read",
            label="Enable Read Operations",
            type="boolean",
            required=False,
            default=True,
            description="Enable reading values from Google Sheets",
        ),
        ConfigField(
            name="create",
            label="Enable Create Operations",
            type="boolean",
            required=False,
            default=False,
            description="Enable creating new Google Sheets",
        ),
        ConfigField(
            name="update",
            label="Enable Update Operations",
            type="boolean",
            required=False,
            default=False,
            description="Enable updating data in Google Sheets",
        ),
        ConfigField(
            name="duplicate",
            label="Enable Duplicate Operations",
            type="boolean",
            required=False,
            default=False,
            description="Enable duplicating existing Google Sheets",
        ),
    ],
    dependencies=[
        "google-api-python-client",
        "google-auth-httplib2", 
        "google-auth-oauthlib"
    ],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_sheets",
)
def google_sheets_tools() -> type[GoogleSheetsTools]:
    """Return Google Sheets tools for spreadsheet integration."""
    from agno.tools.googlesheets import GoogleSheetsTools  # noqa: PLC0415

    return GoogleSheetsTools