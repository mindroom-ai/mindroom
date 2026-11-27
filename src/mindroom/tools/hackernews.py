"""Hacker News tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.hackernews import HackerNewsTools


@register_tool_with_metadata(
    name="hackernews",
    file_access=ToolFileAccess.NONE,
    display_name="Hacker News",
    description="Get top stories and user details from Hacker News",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaHackerNews",
    icon_color="text-orange-600",  # Hacker News orange
    config_fields=[
        bool_config_field("enable_get_top_stories", "Enable Get Top Stories"),
        bool_config_field("enable_get_user_details", "Enable Get User Details"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/research-sources/#hackernews",
    function_names=("hackernews",),
    action_scoped_grants=("hackernews",),
)
def hackernews_tools() -> type[HackerNewsTools]:
    """Return Hacker News tools for getting stories and user details."""
    from mindroom.custom_tools.hackernews import HackerNewsTools

    return HackerNewsTools
