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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="comp_...",
            description="Composio API key (can also be set via COMPOSIO_API_KEY env var)",
        ),
        ConfigField(
            name="entity_id",
            label="Entity ID",
            type="text",
            required=False,
            placeholder="default",
            description="Entity identifier for Composio workspace",
        ),
        # Tool configuration
        ConfigField(
            name="actions",
            label="Enabled Actions",
            type="text",
            required=False,
            placeholder="GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER",
            description="Comma-separated list of specific Composio actions to enable",
        ),
        ConfigField(
            name="apps",
            label="Enabled Apps",
            type="text",
            required=False,
            placeholder="github,gmail,slack",
            description="Comma-separated list of apps to enable (e.g., github, gmail, slack)",
        ),
        # Feature flags
        ConfigField(
            name="use_local",
            label="Use Local Tools",
            type="boolean",
            required=False,
            default=False,
            description="Enable local tool execution when available",
        ),
        ConfigField(
            name="enable_logging",
            label="Enable Logging",
            type="boolean",
            required=False,
            default=True,
            description="Enable detailed logging for Composio operations",
        ),
    ],
    dependencies=["composio-agno"],
    docs_url="https://docs.agno.com/tools/toolkits/others/composio",
)
def composio_tools() -> type[ComposioToolSet]:
    """Return Composio tools for accessing 1000+ integrations."""
    from composio_agno import ComposioToolSet

    return ComposioToolSet  # type: ignore[no-any-return]
