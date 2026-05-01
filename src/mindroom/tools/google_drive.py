"""Google Drive tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from mindroom.custom_tools.google_drive import GoogleDriveTools


@register_tool_with_metadata(
    name="google_drive",
    display_name="Google Drive",
    description="Search and read files from the connected user's Google Drive",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    auth_provider="google_drive",
    icon="SiGoogledrive",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="list_files",
            label="List Files",
            type="boolean",
            required=False,
            default=True,
            description="Allow listing recent Google Drive files.",
        ),
        ConfigField(
            name="search_files",
            label="Search Files",
            type="boolean",
            required=False,
            default=True,
            description="Allow searching Google Drive metadata.",
        ),
        ConfigField(
            name="read_file",
            label="Read Files",
            type="boolean",
            required=False,
            default=True,
            description="Allow reading Google Drive file contents.",
        ),
        ConfigField(
            name="max_read_size",
            label="Max Read Size",
            type="number",
            required=False,
            default=10485760,
            description="Maximum non-Google-Workspace file size to read in bytes.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
    ),
    dependencies=[
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
        "google-auth-oauthlib",
    ],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_drive",
    function_names=(
        "list_files",
        "search_files",
        "read_file",
    ),
)
def google_drive_tools() -> type[GoogleDriveTools]:
    """Return Google Drive tools for file search and read access."""
    from mindroom.custom_tools.google_drive import GoogleDriveTools

    return GoogleDriveTools
