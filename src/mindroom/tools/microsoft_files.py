"""OneDrive and SharePoint files tool registration."""

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
    from mindroom.custom_tools.microsoft_files import MicrosoftFilesTools


@register_tool_with_metadata(
    name="microsoft_files",
    display_name="Microsoft OneDrive and SharePoint",
    description="Search, read, and save OneDrive and SharePoint files and Excel ranges as the connected user",
    category=ToolCategory.PRODUCTIVITY,
    file_access=ToolFileAccess.AGENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    consumes_workspace_paths=True,
    auth_provider="microsoft_files",
    icon="TbBrandOnedrive",
    icon_color="text-sky-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow replacing and uploading files and writing Excel ranges.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
        ToolManagedInitArg.FILE_ACCESS,
    ),
    docs_url="https://docs.mindroom.chat/tools/microsoft-365/",
    helper_text=(
        "Each user connects their own Microsoft account. "
        "Store the Microsoft Entra app's client ID and secret in the microsoft_oauth_client credential service."
    ),
    function_names=(
        "microsoft_files_search",
        "microsoft_files_list_folder",
        "microsoft_files_get_file",
        "microsoft_files_download_file",
        "microsoft_files_replace_file",
        "microsoft_files_upload_file",
        "microsoft_files_excel_list_worksheets",
        "microsoft_files_excel_read_range",
        "microsoft_files_excel_update_range",
    ),
)
def microsoft_files_tools() -> type[MicrosoftFilesTools]:
    """Return OneDrive and SharePoint tools that act as the requester."""
    from mindroom.custom_tools.microsoft_files import MicrosoftFilesTools

    return MicrosoftFilesTools
