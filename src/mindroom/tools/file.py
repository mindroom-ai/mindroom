"""File tool configuration."""

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
    from agno.tools.file import FileTools


@register_tool_with_metadata(
    name="file",
    display_name="File Tools",
    description="Local file operations including read, write, list, and search",
    category=ToolCategory.DEVELOPMENT,  # Local tool
    status=ToolStatus.AVAILABLE,  # No config needed
    setup_type=SetupType.NONE,  # No authentication required
    icon="FaFolder",  # React icon name
    icon_color="text-yellow-500",  # Tailwind color class
    config_fields=[
        # Base directory configuration
        ConfigField(
            name="base_dir",
            label="Base Directory",
            type="text",
            required=False,
            default=None,
            placeholder="/path/to/directory",
            description="Base directory for file operations (defaults to current working directory)",
        ),
        # File operation controls
        ConfigField(
            name="save_files",
            label="Save Files",
            type="boolean",
            required=False,
            default=True,
            description="Enable file saving operations",
        ),
        ConfigField(
            name="read_files",
            label="Read Files",
            type="boolean",
            required=False,
            default=True,
            description="Enable file reading operations",
        ),
        ConfigField(
            name="list_files",
            label="List Files",
            type="boolean",
            required=False,
            default=True,
            description="Enable file listing operations",
        ),
        ConfigField(
            name="search_files",
            label="Search Files",
            type="boolean",
            required=False,
            default=True,
            description="Enable file search operations with pattern matching",
        ),
    ],
    dependencies=["agno"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/local/file",
)
def file_tools() -> type[FileTools]:
    """Return file tools for local file operations."""
    from agno.tools.file import FileTools

    return FileTools
