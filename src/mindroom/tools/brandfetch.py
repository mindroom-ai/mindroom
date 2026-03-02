"""Brandfetch tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.brandfetch import BrandfetchTools


@register_tool_with_metadata(
    name="brandfetch",
    display_name="Brandfetch",
    description="Retrieve brand data including logos, colors, and fonts by domain or name",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaPalette",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            description="Brandfetch API key for Brand API (falls back to BRANDFETCH_API_KEY env var)",
        ),
        ConfigField(
            name="client_id",
            label="Client ID",
            type="text",
            required=False,
            description="Brandfetch Client ID for Brand Search API (falls back to BRANDFETCH_CLIENT_ID env var)",
        ),
        ConfigField(
            name="enable_search_by_identifier",
            label="Enable Search by Identifier",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_by_brand",
            label="Enable Search by Brand",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default="https://api.brandfetch.io/v2",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=20.0,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="async_tools",
            label="Async Tools (Deprecated)",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/others/brandfetch",
    helper_text="Get API keys from [Brandfetch Developers](https://developers.brandfetch.com/)",
)
def brandfetch_tools() -> type[BrandfetchTools]:
    """Return Brandfetch tools for brand data retrieval."""
    from agno.tools.brandfetch import BrandfetchTools

    return BrandfetchTools
