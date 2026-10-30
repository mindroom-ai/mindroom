"""Reddit tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.reddit import RedditTools


@register_tool_with_metadata(
    name="reddit",
    file_access=ToolFileAccess.NONE,
    display_name="Reddit",
    description="Social media platform for browsing, posting, and interacting with Reddit communities",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiReddit",
    icon_color="text-orange-500",  # Reddit's signature orange color
    config_fields=[
        ConfigField(
            name="client_id",
            env=("REDDIT_CLIENT_ID",),
            label="Client ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="client_secret",
            env=("REDDIT_CLIENT_SECRET",),
            label="Client Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_agent",
            env=("REDDIT_USER_AGENT",),
            label="User Agent",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="username",
            env=("REDDIT_USERNAME",),
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            env=("REDDIT_PASSWORD",),
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="allowed_subreddits",
            label="Allowed Subreddits",
            type="string[]",
            required=False,
            default=None,
            description="Restrict posts and replies to these subreddits (case-insensitive); reads are unaffected",
        ),
    ],
    dependencies=["praw"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#reddit",
    function_names=(
        "create_post",
        "get_subreddit_info",
        "get_subreddit_stats",
        "get_top_posts",
        "get_trending_subreddits",
        "get_user_info",
        "reply_to_comment",
        "reply_to_post",
    ),
)
def reddit_tools() -> type[RedditTools]:
    """Return Reddit tools for social media interaction."""
    from agno.tools.reddit import RedditTools

    return RedditTools
