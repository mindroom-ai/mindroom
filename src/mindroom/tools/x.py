"""X (Twitter) tool configuration and native toolkit."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

_XReadAction = Literal["get_user_info", "get_home_timeline", "search_posts"]
_XWriteAction = Literal["create_post", "reply_to_post", "send_dm"]


class _XTools(Toolkit):
    """Read X profiles/timelines/posts and publish posts, replies, or DMs via Tweepy."""

    def __init__(
        self,
        bearer_token: str | None = None,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        access_token: str | None = None,
        access_token_secret: str | None = None,
        include_post_metrics: bool = False,
        wait_on_rate_limit: bool = False,
    ) -> None:
        try:
            import tweepy
        except ImportError as exc:
            msg = "`tweepy` not installed. Install with `pip install tweepy`."
            raise ImportError(msg) from exc

        self.bearer_token = bearer_token or os.getenv("X_BEARER_TOKEN")
        self.consumer_key = consumer_key or os.getenv("X_CONSUMER_KEY")
        self.consumer_secret = consumer_secret or os.getenv("X_CONSUMER_SECRET")
        self.access_token = access_token or os.getenv("X_ACCESS_TOKEN")
        self.access_token_secret = access_token_secret or os.getenv("X_ACCESS_TOKEN_SECRET")
        self.include_post_metrics = include_post_metrics
        self.wait_on_rate_limit = wait_on_rate_limit
        self._http_error = tweepy.HTTPException
        self.client = tweepy.Client(
            bearer_token=self.bearer_token,
            consumer_key=self.consumer_key,
            consumer_secret=self.consumer_secret,
            access_token=self.access_token,
            access_token_secret=self.access_token_secret,
            wait_on_rate_limit=self.wait_on_rate_limit,
        )
        super().__init__(name="x")

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, self._http_error):
            return json.dumps({"error": f"X API error ({exc.response.status_code})"})
        return json.dumps({"error": f"X request failed: {type(exc).__name__}"})

    def _search_posts(self, query: str, max_results: int) -> dict[str, Any]:
        bounded = max(10, min(max_results, 100))
        results = self.client.search_recent_tweets(
            query=query,
            max_results=bounded,
            tweet_fields=["author_id", "created_at", "id", "public_metrics", "text"],
            expansions=["author_id"],
            user_fields=["name", "username", "verified"],
        )
        users_data: dict[Any, dict[str, Any]] = {}
        includes = results.includes
        if isinstance(includes, dict) and "users" in includes:
            for user in includes["users"]:
                users_data[user.id] = {
                    "id": user.id,
                    "name": user.name,
                    "username": user.username,
                    "verified": user.verified,
                }
        if not results.data:
            return {}
        tweets: list[dict[str, Any]] = []
        for tweet in results.data:
            author = users_data.get(tweet.author_id, {"id": tweet.author_id, "name": "Unknown", "username": "unknown"})
            created_at = tweet.created_at
            post: dict[str, Any] = {
                "id": tweet.id,
                "text": tweet.text,
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at is not None else None,
                "author": author,
                "url": f"https://x.com/{author.get('username', 'unknown')}/status/{tweet.id}",
            }
            if self.include_post_metrics:
                metrics = tweet.public_metrics or {}
                post["metrics"] = {
                    "retweet_count": metrics.get("retweet_count", 0),
                    "reply_count": metrics.get("reply_count", 0),
                    "like_count": metrics.get("like_count", 0),
                    "quote_count": metrics.get("quote_count", 0),
                }
            tweets.append(post)
        return {"query": query, "count": len(tweets), "posts": tweets}

    def _send_dm(self, recipient: str, text: str) -> dict[str, Any]:
        target = recipient.strip()
        if target.isdigit():
            recipient_id: Any = target
            recipient_username: str | None = None
        else:
            user = self.client.get_user(username=target, user_auth=True)
            if not user.data:
                return {"error": f"User '{target}' not found. Please check the username or user ID."}
            recipient_id = user.data.id
            recipient_username = target
        response = self.client.create_direct_message(participant_id=recipient_id, text=text)
        return {
            "message": "Direct message sent successfully!",
            "dm_id": response.data["dm_event_id"],
            "recipient_id": recipient_id,
            "recipient_username": recipient_username,
        }

    @tool
    def x_read(
        self,
        action: _XReadAction,
        username: str | None = None,
        query: str | None = None,
        max_results: int = 10,
    ) -> str:
        """Read X user info, home timeline, or recent posts matching a query.

        - `get_user_info`: `username`
        - `get_home_timeline`: no required arguments.
        - `search_posts`: `query`

        Args:
            action: Read operation to perform.
            username: X handle for `get_user_info`.
            query: Search query for `search_posts`.
            max_results: Maximum posts; search clamps to 10-100.

        """
        try:
            if action == "get_user_info":
                if not username or not username.strip():
                    return json.dumps({"error": "`username` is required for action 'get_user_info'."})
                user = self.client.get_user(username=username.strip(), user_fields=["description", "public_metrics"])
                if not user.data:
                    return json.dumps({"error": f"User '{username}' not found."})
                info = user.data.data
                metrics = info.get("public_metrics", {})
                result: dict[str, Any] = {
                    "id": info["id"],
                    "name": info["name"],
                    "username": info["username"],
                    "description": info.get("description"),
                    "followers_count": metrics.get("followers_count", 0),
                    "following_count": metrics.get("following_count", 0),
                    "tweet_count": metrics.get("tweet_count", 0),
                }
            elif action == "get_home_timeline":
                tweets = self.client.get_home_timeline(
                    max_results=max_results,
                    tweet_fields=["created_at", "author_id"],
                )
                timeline = [
                    {
                        "id": tweet.id,
                        "text": tweet.text,
                        "created_at": tweet.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if tweet.created_at is not None
                        else None,
                        "author_id": tweet.author_id,
                    }
                    for tweet in (tweets.data or ())
                ]
                result = {"home_timeline": timeline}
            else:
                if not query or not query.strip():
                    return json.dumps({"error": "`query` is required for action 'search_posts'."})
                result = self._search_posts(query, max_results)
            return json.dumps(result, indent=2)
        except Exception as exc:
            return self._safe_error(exc)

    @tool
    def x_write(
        self,
        action: _XWriteAction,
        text: str,
        post_id: str | None = None,
        recipient: str | None = None,
    ) -> str:
        """Create an X post, reply to a post, or send a direct message.

        - `create_post`: `text`
        - `reply_to_post`: `text`, `post_id`
        - `send_dm`: `text`, `recipient`

        Args:
            action: Write operation to perform.
            text: Text content of the post, reply, or direct message.
            post_id: Target post ID for `reply_to_post`.
            recipient: Recipient username or numeric user ID for `send_dm`.

        """
        try:
            if action == "create_post":
                response = self.client.create_tweet(text=text)
                result: dict[str, Any] = {
                    "message": "Post successfully created!",
                    "url": f"https://x.com/i/web/status/{response.data['id']}",
                }
            elif action == "reply_to_post":
                if not post_id or not post_id.strip():
                    result = {"error": "`post_id` is required for action 'reply_to_post'."}
                else:
                    response = self.client.create_tweet(text=text, in_reply_to_tweet_id=post_id.strip())
                    result = {
                        "message": "Reply successfully posted!",
                        "url": f"https://x.com/i/web/status/{response.data['id']}",
                    }
            elif not recipient or not recipient.strip():
                result = {"error": "`recipient` is required for action 'send_dm'."}
            else:
                result = self._send_dm(recipient, text)
            return json.dumps(result, indent=2)
        except Exception as exc:
            return self._safe_error(exc)


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
    function_names=("x_read", "x_write"),
)
def x_tools() -> type[_XTools]:
    """Return X (Twitter) tools for posting tweets and social media interaction."""
    return _XTools
