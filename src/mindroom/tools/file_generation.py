"""File Generation tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.file_generation import FileGenerationTools


@register_tool_with_metadata(
    name="file_generation",
    file_access=ToolFileAccess.NONE,
    display_name="File Generation",
    description="Generate JSON, CSV, PDF, DOCX, HTML, text, and code files inside the agent workspace",
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
            description="Directory inside the agent workspace where generated files are saved",
        ),
        bool_config_field("enable_json_generation", "Enable JSON Generation"),
        bool_config_field("enable_csv_generation", "Enable CSV Generation"),
        bool_config_field("enable_pdf_generation", "Enable PDF Generation"),
        bool_config_field("enable_docx_generation", "Enable DOCX Generation"),
        bool_config_field("enable_txt_generation", "Enable TXT Generation"),
        bool_config_field("enable_html_generation", "Enable HTML Generation"),
        bool_config_field("enable_code_generation", "Enable Code Generation"),
        bool_config_field("save_files", "Save Files", default=False),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["python-docx", "reportlab"],
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#file_generation",
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,),
    function_names=("generate_file",),
)
def file_generation_tools() -> type[FileGenerationTools]:
    """Return File Generation tools that save generated files only inside the agent workspace."""
    from mindroom.custom_tools.file_generation import FileGenerationTools

    return FileGenerationTools
