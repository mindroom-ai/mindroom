"""File generation tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.file_generation import FileGenerationTools


@register_tool_with_metadata(
    name="file_generation",
    display_name="File Generation",
    description="Generate JSON, CSV, PDF, and text files from data",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaFileExport",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="output_directory",
            label="Output Directory",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_json_generation",
            label="Enable JSON Generation",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_csv_generation",
            label="Enable CSV Generation",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_pdf_generation",
            label="Enable PDF Generation",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_txt_generation",
            label="Enable TXT Generation",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["reportlab"],
    docs_url="https://docs.agno.com/tools/toolkits/others/file_generation",
)
def file_generation_tools() -> type[FileGenerationTools]:
    """Return File Generation tools for creating JSON, CSV, PDF, and text files."""
    from agno.tools.file_generation import FileGenerationTools

    return FileGenerationTools
