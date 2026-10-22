"""Microsoft OneNote tool registration."""

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
    from mindroom.custom_tools.microsoft_onenote import MicrosoftOneNoteTools


@register_tool_with_metadata(
    name="microsoft_onenote",
    display_name="Microsoft OneNote",
    description="List, read, create, and append to pages in OneNote notebooks the connected user owns or can open",
    category=ToolCategory.PRODUCTIVITY,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="microsoft_onenote",
    icon="NotebookPen",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow creating pages and appending text to pages.",
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
        "microsoft_onenote_list_notebooks",
        "microsoft_onenote_list_sections",
        "microsoft_onenote_list_pages",
        "microsoft_onenote_get_page",
        "microsoft_onenote_create_page",
        "microsoft_onenote_append_to_page",
    ),
)
def microsoft_onenote_tools() -> type[MicrosoftOneNoteTools]:
    """Return Microsoft OneNote tools that act as the requester."""
    from mindroom.custom_tools.microsoft_onenote import MicrosoftOneNoteTools

    return MicrosoftOneNoteTools
