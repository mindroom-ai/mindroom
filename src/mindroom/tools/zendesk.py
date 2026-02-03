"""Zendesk tool configuration."""

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
    from agno.tools.zendesk import ZendeskTools


@register_tool_with_metadata(
    name="zendesk",
    display_name="Zendesk",
    description="Customer support platform for searching help center articles",
    category=ToolCategory.DEVELOPMENT,  # From others/ category
    status=ToolStatus.REQUIRES_CONFIG,  # Requires username, password, company_name
    setup_type=SetupType.API_KEY,  # Uses username/password authentication
    icon="HelpCircle",  # React icon for help/support
    icon_color="text-green-600",  # Zendesk brand green
    config_fields=[
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="company_name",
            label="Company Name",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_search_zendesk",
            label="Enable Search Zendesk",
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
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/zendesk",
)
def zendesk_tools() -> type[ZendeskTools]:
    """Return Zendesk tools for searching help center articles."""
    from agno.tools.zendesk import ZendeskTools

    return ZendeskTools
