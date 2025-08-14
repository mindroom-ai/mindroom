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
    from mindroom.custom_tools.google_sheets import GoogleSheetsTools


@register_tool_with_metadata(
    name="google_sheets",
    display_name="Google Sheets",
    description="Read, create, update, and duplicate Google Sheets spreadsheets",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    auth_provider="google",  # Authentication provided by Google Services integration
    icon="FaGoogle",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="spreadsheet_id",
            label="Spreadsheet ID",
            type="text",
            required=False,
            placeholder="Leave empty to work with multiple spreadsheets",
            description="The ID of the Google Spreadsheet to work with. If not specified, you can work with multiple spreadsheets.",
        ),
        ConfigField(
            name="spreadsheet_range",
            label="Default Range",
            type="text",
            required=False,
            placeholder="e.g., Sheet1!A1:Z100",
            description="Default range to use for operations (optional)",
        ),
        ConfigField(
            name="read",
            label="Enable Read Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow reading data from spreadsheets",
        ),
        ConfigField(
            name="create",
            label="Enable Create Operations",
            type="boolean",
            required=False,
            default=False,
            description="Allow creating new spreadsheets",
        ),
        ConfigField(
            name="update",
            label="Enable Update Operations",
            type="boolean",
            required=False,
            default=False,
            description="Allow updating existing spreadsheets",
        ),
        ConfigField(
            name="duplicate",
            label="Enable Duplicate Operations",
            type="boolean",
            required=False,
            default=False,
            description="Allow duplicating spreadsheets",
        ),
    ],
    dependencies=["google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_sheets",
)
def google_sheets_tools() -> type[GoogleSheetsTools]:
    """Return Google Sheets tools for spreadsheet integration."""
    from mindroom.custom_tools.google_sheets import GoogleSheetsTools

    return GoogleSheetsTools
