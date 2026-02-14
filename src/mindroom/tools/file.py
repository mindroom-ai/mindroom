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
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_save_file",
            label="Enable Save File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_file",
            label="Enable Read File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_delete_file",
            label="Enable Delete File",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_list_files",
            label="Enable List Files",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_files",
            label="Enable Search Files",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_file_chunk",
            label="Enable Read File Chunk",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_replace_file_chunk",
            label="Enable Replace File Chunk",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="expose_base_directory",
            label="Expose Base Directory",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="max_file_length",
            label="Max File Length",
            type="number",
            required=False,
            default=10000000,
        ),
        ConfigField(
            name="max_file_lines",
            label="Max File Lines",
            type="number",
            required=False,
            default=100000,
        ),
        ConfigField(
            name="line_separator",
            label="Line Separator",
            type="text",
            required=False,
            default="\n",
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["agno"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/local/file",
)
def file_tools() -> type[FileTools]:
    """Return file tools for local file operations."""
    from agno.tools.file import FileTools

    return FileTools
