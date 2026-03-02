"""E2B code execution tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
    dependencies=["e2b_code_interpreter"],
    docs_url="https://docs.agno.com/tools/toolkits/others/e2b",
)
def e2b_tools() -> type[E2BTools]:
    """Return E2B code execution tools for secure sandbox environments."""
    from agno.tools.e2b import E2BTools

    return E2BTools
