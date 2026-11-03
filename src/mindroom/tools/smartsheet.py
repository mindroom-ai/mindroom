"""Smartsheet tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.smartsheet import SmartsheetTools


@register_tool_with_metadata(
    name="smartsheet",
    file_access=ToolFileAccess.NONE,
    display_name="Smartsheet",
    description="Find, read, and edit Smartsheet sheets and rows",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="smartsheet",
    icon="Sheet",
    icon_color="text-blue-700",
    config_fields=[
        ConfigField(
            name="access_token",
            env=("SMARTSHEET_ACCESS_TOKEN",),
            label="Access Token",
            type="password",
            required=False,
            default=None,
            description="Smartsheet API access token for everyone; takes precedence over OAuth.",
        ),
    ],
    oauth_fallback_fields=("access_token",),
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/project-management/#smartsheet",
    helper_text=(
        "Each user connects their own Smartsheet account, or save one API access token for everyone. "
        "Store the Smartsheet developer app client ID and secret in the smartsheet_oauth_client credential service."
    ),
    function_names=(
        "list_sheets",
        "search_sheets",
        "get_sheet",
        "add_rows",
        "update_rows",
        "delete_rows",
    ),
)
def smartsheet_tools() -> type[SmartsheetTools]:
    """Return Smartsheet tools for sheets and rows."""
    from mindroom.custom_tools.smartsheet import SmartsheetTools

    return SmartsheetTools
