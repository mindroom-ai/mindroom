"""Google Sheets tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.google_sheets import GoogleSheetsTools


@register_tool_with_metadata(
    name="google_sheets",
    file_access=ToolFileAccess.NONE,
    display_name="Google Sheets",
    description="Read, create, update, and format Google Sheets spreadsheets",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="google_sheets",
    icon="SiGooglesheets",
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
        bool_config_field("read", "Enable Read Operations", description="Allow reading data from spreadsheets"),
        bool_config_field("create", "Enable Create Operations", description="Allow creating new spreadsheets"),
        bool_config_field(
            "update",
            "Enable Update Operations",
            description="Allow updating and formatting existing spreadsheets",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    dependencies=["google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#google_sheets",
    function_names=(
        "google_sheets_read",
        "google_sheets_write",
    ),
    action_scoped_grants=("google_sheets_write",),
)
def google_sheets_tools() -> type[GoogleSheetsTools]:
    """Return Google Sheets tools for spreadsheet integration."""
    from mindroom.custom_tools.google_sheets import GoogleSheetsTools

    return GoogleSheetsTools
