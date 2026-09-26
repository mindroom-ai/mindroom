"""Microsoft 365 connected documents tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.microsoft_365 import Microsoft365Tools


@register_tool_with_metadata(
    name="microsoft_365",
    file_access=ToolFileAccess.AGENT,
    display_name="Microsoft 365",
    description="Connect OneDrive and SharePoint Excel workbooks and edit them in place after human approval",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    consumes_workspace_paths=True,
    auth_provider="microsoft_365",
    icon="FaMicrosoft",
    icon_color="text-sky-600",
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
        "Each user connects their own work or school Microsoft account. "
        "Store the Entra app registration's client ID and secret in the microsoft_365_oauth_client credential service, "
        "and set MICROSOFT_365_TENANT_ID for a single-tenant app."
    ),
    function_names=(
        "connect_office_document",
        "save_office_document",
        "read_office_document",
        "edit_office_document",
    ),
)
def microsoft_365_tools() -> type[Microsoft365Tools]:
    """Return connected OneDrive and SharePoint workbook tools."""
    from mindroom.custom_tools.microsoft_365 import Microsoft365Tools

    return Microsoft365Tools
