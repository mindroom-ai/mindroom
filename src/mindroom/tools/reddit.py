"""Reddit tool configuration."""

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
    from agno.tools.reddit import RedditTools


@register_tool_with_metadata(
    name="reddit",
    display_name="Reddit",
    description="Social media platform for browsing, posting, and interacting with Reddit communities",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaReddit",
    icon_color="text-orange-500",  # Reddit's signature orange color
    config_fields=[
        ConfigField(
            name="reddit_instance",
            label="Reddit Instance",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="client_id",
            label="Client ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="client_secret",
            label="Client Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_agent",
            label="User Agent",
            type="text",
            required=False,
            default=None,
        ),
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
    ],
    dependencies=["praw"],
    docs_url=None,
)
def reddit_tools() -> type[RedditTools]:
    """Return Reddit tools for social media interaction."""
    from agno.tools.reddit import RedditTools

    return RedditTools
