"""Trello tool configuration."""

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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your_trello_api_key",
            description="Trello API key (can also be set via TRELLO_API_KEY env var)",
        ),
        ConfigField(
            name="api_secret",
            label="API Secret",
            type="password",
            required=False,
            placeholder="your_trello_api_secret",
            description="Trello API secret (can also be set via TRELLO_API_SECRET env var)",
        ),
        ConfigField(
            name="token",
            label="Token",
            type="password",
            required=False,
            placeholder="your_trello_token",
            description="Trello token (can also be set via TRELLO_TOKEN env var)",
        ),
        # Card operations
        ConfigField(
            name="create_card",
            label="Create Card",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new cards in boards and lists",
        ),
        ConfigField(
            name="move_card",
            label="Move Card",
            type="boolean",
            required=False,
            default=True,
            description="Enable moving cards between lists",
        ),
        ConfigField(
            name="get_cards",
            label="Get Cards",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving all cards from a list",
        ),
        # Board operations
        ConfigField(
            name="create_board",
            label="Create Board",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new Trello boards",
        ),
        ConfigField(
            name="list_boards",
            label="List Boards",
            type="boolean",
            required=False,
            default=True,
            description="Enable listing all accessible Trello boards",
        ),
        ConfigField(
            name="get_board_lists",
            label="Get Board Lists",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving all lists on a board",
        ),
        # List operations
        ConfigField(
            name="create_list",
            label="Create List",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new lists on boards",
        ),
    ],
    dependencies=["py-trello"],
    docs_url="https://docs.agno.com/tools/toolkits/others/trello",
)
def trello_tools() -> type[TrelloTools]:
    """Return Trello tools for project board management."""
    from agno.tools.trello import TrelloTools

    return TrelloTools
