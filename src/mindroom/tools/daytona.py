"""Daytona tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.daytona import DaytonaTools


@register_tool_with_metadata(
    name="daytona",
    display_name="Daytona",
    description="Execute code in secure, remote sandbox environments",
    category=ToolCategory.DEVELOPMENT,  # others/ maps to DEVELOPMENT
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses API key authentication
    icon="FaTerminal",  # Terminal icon for code execution
    icon_color="text-blue-600",  # Blue color for development tools
    config_fields=[
        # Authentication parameters
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="dt_...",
            description="Daytona API key (can also be set via DAYTONA_API_KEY env var)",
        ),
        ConfigField(
            name="api_url",
            label="API URL",
            type="url",
            required=False,
            placeholder="https://api.daytona.io",
            description="Daytona API URL (can also be set via DAYTONA_API_URL env var)",
        ),
        # Sandbox configuration
        ConfigField(
            name="sandbox_id",
            label="Sandbox ID",
            type="text",
            required=False,
            placeholder="sandbox-123",
            description="Specific sandbox ID to use. If None, creates or uses persistent sandbox",
        ),
        ConfigField(
            name="sandbox_language",
            label="Sandbox Language",
            type="text",
            required=False,
            default="PYTHON",
            placeholder="PYTHON",
            description="Primary language for the sandbox (PYTHON, JAVASCRIPT, TYPESCRIPT)",
        ),
        ConfigField(
            name="sandbox_target",
            label="Sandbox Target",
            type="text",
            required=False,
            placeholder="target-config",
            description="Target configuration for the sandbox",
        ),
        ConfigField(
            name="sandbox_os",
            label="Sandbox OS",
            type="text",
            required=False,
            placeholder="ubuntu-20.04",
            description="Operating system for the sandbox",
        ),
        ConfigField(
            name="auto_stop_interval",
            label="Auto Stop Interval",
            type="number",
            required=False,
            default=60,
            description="Auto-stop interval in minutes (0 to disable)",
        ),
        ConfigField(
            name="sandbox_os_user",
            label="Sandbox OS User",
            type="text",
            required=False,
            placeholder="daytona",
            description="OS user for the sandbox",
        ),
        ConfigField(
            name="sandbox_env_vars",
            label="Sandbox Environment Variables",
            type="text",
            required=False,
            placeholder='{"ENV_VAR": "value"}',
            description="Environment variables for the sandbox (JSON format)",
        ),
        ConfigField(
            name="sandbox_labels",
            label="Sandbox Labels",
            type="text",
            required=False,
            default="{}",
            placeholder='{"label": "value"}',
            description="Labels for the sandbox (JSON format)",
        ),
        ConfigField(
            name="organization_id",
            label="Organization ID",
            type="text",
            required=False,
            placeholder="org-123",
            description="Organization ID for the sandbox",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=300,
            description="Timeout for sandbox operations in seconds",
        ),
        # Feature flags
        ConfigField(
            name="auto_create_sandbox",
            label="Auto Create Sandbox",
            type="boolean",
            required=False,
            default=True,
            description="Automatically create sandbox if none exists",
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify SSL",
            type="boolean",
            required=False,
            default=False,
            description="Whether to verify SSL certificates",
        ),
        ConfigField(
            name="persistent",
            label="Persistent",
            type="boolean",
            required=False,
            default=True,
            description="Whether to reuse the same sandbox across agent sessions",
        ),
        ConfigField(
            name="sandbox_public",
            label="Sandbox Public",
            type="boolean",
            required=False,
            description="Whether the sandbox should be public",
        ),
        # Custom instructions
        ConfigField(
            name="instructions",
            label="Instructions",
            type="text",
            required=False,
            placeholder="Custom guidelines for the toolkit",
            description="Custom instructions for the toolkit",
        ),
        ConfigField(
            name="add_instructions",
            label="Add Instructions",
            type="boolean",
            required=False,
            default=False,
            description="Whether to add instructions to the agent",
        ),
    ],
    dependencies=["daytona"],
    docs_url="https://docs.agno.com/tools/toolkits/others/daytona",
)
def daytona_tools() -> type[DaytonaTools]:
    """Return Daytona tools for secure code execution in remote sandbox environments."""
    from agno.tools.daytona import DaytonaTools

    return DaytonaTools
