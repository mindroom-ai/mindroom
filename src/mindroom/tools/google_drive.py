"""Google Drive tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.google_drive import GoogleDriveTools


@register_tool_with_metadata(
    name="google_drive",
    display_name="Google Drive",
    description="Search, read, upload, and organize files in the connected user's Google Drive",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    consumes_workspace_paths=True,
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
            name="download_file",
            label="Download Files",
            type="boolean",
            required=False,
            default=False,
            description="Allow downloading or exporting Google Drive files.",
        ),
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow uploading, creating folders, moving or renaming files, and trashing files.",
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
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
    ),
    dependencies=[
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
        "google-auth-oauthlib",
    ],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_drive",
    function_names=(
        "google_drive_list_files",
        "google_drive_search_files",
        "google_drive_read_file",
        "google_drive_download_file",
        "google_drive_upload_file",
        "google_drive_create_folder",
        "google_drive_move_file",
        "google_drive_trash_file",
    ),
)
def google_drive_tools() -> type[GoogleDriveTools]:
    """Return Google Drive tools for file reading and management."""
    from mindroom.custom_tools.google_drive import GoogleDriveTools

    return GoogleDriveTools
