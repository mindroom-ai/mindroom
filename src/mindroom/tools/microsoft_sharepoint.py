"""Microsoft SharePoint tool registration."""

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
    from mindroom.custom_tools.microsoft_sharepoint import MicrosoftSharePointTools


@register_tool_with_metadata(
    name="microsoft_sharepoint",
    display_name="Microsoft SharePoint",
    description="Find SharePoint sites, read and change list items, and read site pages as the connected user",
    category=ToolCategory.PRODUCTIVITY,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="microsoft_sharepoint",
    icon="LayoutList",
    icon_color="text-teal-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow adding list items and changing their fields.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/microsoft-365/",
    helper_text=(
        "Each user connects their own Microsoft account. "
        "Store the Microsoft Entra app's client ID and secret in the microsoft_oauth_client credential service."
    ),
    function_names=(
        "microsoft_sharepoint_search_sites",
        "microsoft_sharepoint_get_site",
        "microsoft_sharepoint_list_lists",
        "microsoft_sharepoint_get_list",
        "microsoft_sharepoint_list_items",
        "microsoft_sharepoint_create_item",
        "microsoft_sharepoint_update_item",
        "microsoft_sharepoint_list_pages",
        "microsoft_sharepoint_get_page",
    ),
)
def microsoft_sharepoint_tools() -> type[MicrosoftSharePointTools]:
    """Return Microsoft SharePoint tools that act as the requester."""
    from mindroom.custom_tools.microsoft_sharepoint import MicrosoftSharePointTools

    return MicrosoftSharePointTools
