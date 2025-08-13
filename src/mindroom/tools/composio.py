"""Composio tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from composio_agno import ComposioToolSet  # type: ignore[import-untyped]


@register_tool_with_metadata(
    name="composio",
    display_name="Composio",
    description="Access 1000+ integrations including Gmail, Salesforce, GitHub, and more",
    category=ToolCategory.INTEGRATIONS,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaConnectdevelop",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="comp_...",
            description="Composio API key (can also be set via COMPOSIO_API_KEY env var)",
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            description="Base URL for Composio API (leave empty for default)",
        ),
        ConfigField(
            name="entity_id",
            label="Entity ID",
            type="text",
            required=False,
            default="default",
            placeholder="default",
            description="Entity identifier for Composio workspace",
        ),
        # Workspace Configuration
        ConfigField(
            name="workspace_id",
            label="Workspace ID",
            type="text",
            required=False,
            placeholder="workspace_123",
            description="Workspace identifier for organizing tools and data",
        ),
        ConfigField(
            name="workspace_config",
            label="Workspace Config",
            type="text",
            required=False,
            placeholder='{"type": "local"}',
            description="JSON configuration for workspace settings",
        ),
        # Connection Configuration
        ConfigField(
            name="connected_account_ids",
            label="Connected Account IDs",
            type="text",
            required=False,
            placeholder='{"github": "account_123"}',
            description="JSON mapping of app names to connected account IDs",
        ),
        # Advanced Configuration
        ConfigField(
            name="metadata",
            label="Metadata",
            type="text",
            required=False,
            placeholder='{"key": "value"}',
            description="JSON metadata for tools and actions configuration",
        ),
        ConfigField(
            name="processors",
            label="Processors",
            type="text",
            required=False,
            description="Custom processors configuration (JSON format)",
        ),
        ConfigField(
            name="output_dir",
            label="Output Directory",
            type="text",
            required=False,
            placeholder="/path/to/output",
            description="Directory path for output files",
        ),
        ConfigField(
            name="lockfile",
            label="Lock File Path",
            type="text",
            required=False,
            placeholder="/path/to/lockfile",
            description="Path to lock file for concurrency control",
        ),
        # Numerical Configuration
        ConfigField(
            name="max_retries",
            label="Max Retries",
            type="number",
            required=False,
            default=3,
            description="Maximum number of retries for failed operations",
        ),
        ConfigField(
            name="verbosity_level",
            label="Verbosity Level",
            type="number",
            required=False,
            placeholder="1",
            description="Logging verbosity level (0-3, higher = more verbose)",
        ),
        # Feature Flags
        ConfigField(
            name="output_in_file",
            label="Output in File",
            type="boolean",
            required=False,
            default=False,
            description="Enable file-based output for operations",
        ),
        ConfigField(
            name="allow_tracing",
            label="Allow Tracing",
            type="boolean",
            required=False,
            default=False,
            description="Enable operation tracing for debugging",
        ),
        ConfigField(
            name="lock",
            label="Enable Locking",
            type="boolean",
            required=False,
            default=True,
            description="Enable file locking for concurrent operations",
        ),
        # Logging Configuration
        ConfigField(
            name="logging_level",
            label="Logging Level",
            type="text",
            required=False,
            default="INFO",
            placeholder="INFO",
            description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        ),
    ],
    dependencies=["composio-agno"],
    docs_url="https://docs.agno.com/tools/toolkits/others/composio",
)
def composio_tools() -> type[ComposioToolSet]:
    """Return Composio tools for accessing 1000+ integrations."""
    from composio_agno import ComposioToolSet

    return ComposioToolSet  # type: ignore[no-any-return]
