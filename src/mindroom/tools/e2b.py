"""E2B code execution tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.e2b import E2BTools


@register_tool_with_metadata(
    name="e2b",
    display_name="E2B Code Execution",
    description="Code execution sandbox environment with Python, file operations, and web server capabilities",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Terminal",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="e2b_...",
            description="E2B API key for authentication (can also be set via E2B_API_KEY env var)",
        ),
        # Configuration
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=300,
            placeholder="300",
            description="Timeout in seconds for the sandbox (default: 5 minutes)",
        ),
        # Code execution features
        ConfigField(
            name="run_code",
            label="Run Code",
            type="boolean",
            required=False,
            default=True,
            description="Enable running Python code in the sandbox",
        ),
        # File operations
        ConfigField(
            name="upload_file",
            label="Upload File",
            type="boolean",
            required=False,
            default=True,
            description="Enable uploading files to the sandbox",
        ),
        ConfigField(
            name="download_result",
            label="Download Result",
            type="boolean",
            required=False,
            default=True,
            description="Enable downloading execution results (PNG images, charts, files)",
        ),
        # Filesystem operations
        ConfigField(
            name="filesystem",
            label="Filesystem Operations",
            type="boolean",
            required=False,
            default=False,
            description="Enable filesystem operations (list, read, write files and directories)",
        ),
        # Internet access
        ConfigField(
            name="internet_access",
            label="Internet Access",
            type="boolean",
            required=False,
            default=False,
            description="Enable internet access functions (public URLs, web servers)",
        ),
        # Sandbox management
        ConfigField(
            name="sandbox_management",
            label="Sandbox Management",
            type="boolean",
            required=False,
            default=False,
            description="Enable sandbox management functions (timeout, status, shutdown)",
        ),
        # Advanced configuration
        ConfigField(
            name="sandbox_options",
            label="Sandbox Options",
            type="text",
            required=False,
            placeholder='{"template": "python"}',
            description="Additional options to pass to the Sandbox constructor (JSON format)",
        ),
        # Command execution
        ConfigField(
            name="command_execution",
            label="Command Execution",
            type="boolean",
            required=False,
            default=False,
            description="Enable shell command execution in the sandbox",
        ),
    ],
    dependencies=["e2b_code_interpreter"],
    docs_url="https://docs.agno.com/tools/toolkits/others/e2b",
)
def e2b_tools() -> type[E2BTools]:
    """Return E2B code execution tools for secure sandbox environments."""
    from agno.tools.e2b import E2BTools

    return E2BTools
