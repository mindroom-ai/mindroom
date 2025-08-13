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
        # Reddit instance parameter
        ConfigField(
            name="reddit_instance",
            label="Reddit Instance",
            type="text",
            required=False,
            default=None,
            description="Pre-configured Reddit instance (optional, will create one if not provided)",
        ),
        # Authentication parameters
        ConfigField(
            name="client_id",
            label="Client ID",
            type="text",
            required=False,
            placeholder="your_client_id",
            description="Reddit API client ID (can also be set via REDDIT_CLIENT_ID env var)",
        ),
        ConfigField(
            name="client_secret",
            label="Client Secret",
            type="password",
            required=False,
            placeholder="your_client_secret",
            description="Reddit API client secret (can also be set via REDDIT_CLIENT_SECRET env var)",
        ),
        ConfigField(
            name="user_agent",
            label="User Agent",
            type="text",
            required=False,
            default="RedditTools v1.0",
            placeholder="MyApp v1.0",
            description="User agent string for API requests (can also be set via REDDIT_USER_AGENT env var)",
        ),
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            placeholder="your_reddit_username",
            description="Reddit username for authenticated actions (can also be set via REDDIT_USERNAME env var)",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            placeholder="your_reddit_password",
            description="Reddit password for authenticated actions (can also be set via REDDIT_PASSWORD env var)",
        ),
        # Feature flags grouped by functionality
        # User and community information
        ConfigField(
            name="get_user_info",
            label="Get User Info",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting information about Reddit users",
        ),
        ConfigField(
            name="get_top_posts",
            label="Get Top Posts",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting top posts from subreddits",
        ),
        ConfigField(
            name="get_subreddit_info",
            label="Get Subreddit Info",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting information about subreddits",
        ),
        ConfigField(
            name="get_trending_subreddits",
            label="Get Trending Subreddits",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting currently trending subreddits",
        ),
        ConfigField(
            name="get_subreddit_stats",
            label="Get Subreddit Stats",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting statistics about subreddits",
        ),
        # Content creation and interaction
        ConfigField(
            name="create_post",
            label="Create Post",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new posts in subreddits",
        ),
        ConfigField(
            name="reply_to_post",
            label="Reply to Post",
            type="boolean",
            required=False,
            default=True,
            description="Enable replying to existing posts",
        ),
        ConfigField(
            name="reply_to_comment",
            label="Reply to Comment",
            type="boolean",
            required=False,
            default=True,
            description="Enable replying to existing comments",
        ),
    ],
    dependencies=["praw"],
    docs_url=None,
)
def reddit_tools() -> type[RedditTools]:
    """Return Reddit tools for social media interaction."""
    from agno.tools.reddit import RedditTools

    return RedditTools
