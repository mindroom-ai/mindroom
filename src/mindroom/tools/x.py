"""X (Twitter) tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.x import XTools


@register_tool_with_metadata(
    name="x",
    file_access=ToolFileAccess.NONE,
    display_name="X (Twitter)",
    description="Post tweets, send DMs, and search Twitter/X content",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiX",
    icon_color="text-blue-400",
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="bearer_token",
            env=("X_BEARER_TOKEN",),
            label="Bearer Token",
            type="password",
            required=False,
            placeholder="AAA...",
            description="Twitter API Bearer token",
        ),
        ConfigField(
            name="consumer_key",
            env=("X_CONSUMER_KEY",),
            label="Consumer Key",
            type="password",
            required=False,
            placeholder="consumer_key...",
            description="Twitter API consumer key",
        ),
        ConfigField(
            name="consumer_secret",
            env=("X_CONSUMER_SECRET",),
            label="Consumer Secret",
            type="password",
            required=False,
            placeholder="consumer_secret...",
            description="Twitter API consumer secret",
        ),
        ConfigField(
            name="access_token",
            env=("X_ACCESS_TOKEN",),
            label="Access Token",
            type="password",
            required=False,
            placeholder="access_token...",
            description="Twitter API access token",
        ),
        ConfigField(
            name="access_token_secret",
            env=("X_ACCESS_TOKEN_SECRET",),
            label="Access Token Secret",
            type="password",
            required=False,
            placeholder="access_token_secret...",
            description="Twitter API access token secret",
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
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#x",
    function_names=(
        "create_post",
        "get_home_timeline",
        "get_user_info",
        "reply_to_post",
        "search_posts",
        "send_dm",
    ),
)
def x_tools() -> type[XTools]:
    """Return X (Twitter) tools for posting tweets and social media interaction."""
    from agno.tools.x import XTools

    return XTools
