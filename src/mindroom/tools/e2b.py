"""E2B code execution tool configuration."""

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
    from mindroom.custom_tools.e2b import MindRoomE2BTools


@register_tool_with_metadata(
    name="e2b",
    file_access=ToolFileAccess.AGENT,
    requires_primary_runtime=True,
    consumes_workspace_paths=True,
    display_name="E2B Code Execution",
    description="Code execution sandbox environment with Python, file operations, and web server capabilities",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Terminal",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=300,
        ),
        ConfigField(
            name="sandbox_options",
            label="Sandbox Options",
            type="text",
            required=False,
            default=None,
        ),
    ],
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
    dependencies=["e2b_code_interpreter"],
    docs_url="https://docs.agno.com/tools/toolkits/others/e2b",
    function_names=(
        "download_chart_data",
        "download_file_from_sandbox",
        "download_png_result",
        "get_public_url",
        "get_sandbox_status",
        "kill_background_command",
        "list_files",
        "list_running_sandboxes",
        "read_file_content",
        "run_background_command",
        "run_command",
        "run_python_code",
        "run_server",
        "set_sandbox_timeout",
        "shutdown_sandbox",
        "stream_command",
        "upload_file",
        "watch_directory",
        "write_file_content",
    ),
)
def e2b_tools() -> type[MindRoomE2BTools]:
    """Return E2B code execution tools whose local file transfers stay in the agent workspace."""
    from mindroom.custom_tools.e2b import MindRoomE2BTools

    return MindRoomE2BTools
