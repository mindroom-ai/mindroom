"""X (Twitter) tool configuration."""

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
    from agno.tools.x import XTools


@register_tool_with_metadata(
    name="x",
    display_name="X (Twitter)",
    description="Post tweets, send DMs, and search Twitter/X content",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTwitter",
    icon_color="text-blue-400",
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="bearer_token",
            label="Bearer Token",
            type="password",
            required=False,
            placeholder="AAA...",
            description="Twitter API Bearer token (can also be set via X_BEARER_TOKEN env var)",
        ),
        ConfigField(
            name="consumer_key",
            label="Consumer Key",
            type="password",
            required=False,
            placeholder="consumer_key...",
            description="Twitter API consumer key (can also be set via X_CONSUMER_KEY env var)",
        ),
        ConfigField(
            name="consumer_secret",
            label="Consumer Secret",
            type="password",
            required=False,
            placeholder="consumer_secret...",
            description="Twitter API consumer secret (can also be set via X_CONSUMER_SECRET env var)",
        ),
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=False,
            placeholder="access_token...",
            description="Twitter API access token (can also be set via X_ACCESS_TOKEN env var)",
        ),
        ConfigField(
            name="access_token_secret",
            label="Access Token Secret",
            type="password",
            required=False,
            placeholder="access_token_secret...",
            description="Twitter API access token secret (can also be set via X_ACCESS_TOKEN_SECRET env var)",
        ),
        # Feature flags/boolean parameters grouped by functionality
        # Search and content features
        ConfigField(
            name="include_post_metrics",
            label="Include Post Metrics",
            type="boolean",
            required=False,
            default=False,
            description="Enable including post metrics (likes, retweets, etc.) in search results",
        ),
        # API behavior settings
        ConfigField(
            name="wait_on_rate_limit",
            label="Wait on Rate Limit",
            type="boolean",
            required=False,
            default=False,
            description="Enable waiting when rate limit is reached instead of failing",
        ),
    ],
    dependencies=["tweepy"],
    docs_url="https://docs.agno.com/tools/toolkits/social/x",
)
def x_tools() -> type[XTools]:
    """Return X (Twitter) tools for posting tweets and social media interaction."""
    from agno.tools.x import XTools

    return XTools
