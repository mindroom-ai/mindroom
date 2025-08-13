"""Hacker News tool configuration."""

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
    from agno.tools.hackernews import HackerNewsTools


@register_tool_with_metadata(
    name="hackernews",
    display_name="Hacker News",
    description="Get top stories and user details from Hacker News",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaHackerNews",
    icon_color="text-orange-600",  # Hacker News orange
    config_fields=[
        # Feature flags
        ConfigField(
            name="get_top_stories",
            label="Get Top Stories",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting top stories from Hacker News",
        ),
        ConfigField(
            name="get_user_details",
            label="Get User Details",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting user details from Hacker News",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/search/hackernews",
)
def hackernews_tools() -> type[HackerNewsTools]:
    """Return Hacker News tools for getting stories and user details."""
    from agno.tools.hackernews import HackerNewsTools

    return HackerNewsTools
