"""Trello tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.trello import TrelloTools


@register_tool_with_metadata(
    name="trello",
    display_name="Trello",
    description="Project board management with Trello API integration",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTrello",
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
            name="api_secret",
            label="API Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="token",
            label="Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["py-trello"],
    docs_url="https://docs.agno.com/tools/toolkits/others/trello",
)
def trello_tools() -> type[TrelloTools]:
    """Return Trello tools for project board management."""
    from agno.tools.trello import TrelloTools

    return TrelloTools
