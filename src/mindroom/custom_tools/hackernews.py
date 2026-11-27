"""Native Hacker News toolkit backed by the Firebase REST API."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import reject_unexpected_args, require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
_HackerNewsAction = Literal["top_stories", "user"]
_ACTION_FIELDS: dict[str, str] = {
    "top_stories": "num_stories",
    "user": "username",
}


class HackerNewsTools(Toolkit):
    """Fetch top stories and user details from Hacker News."""

    name = "hackers_news"

    def __init__(
        self,
        enable_get_top_stories: bool = True,
        enable_get_user_details: bool = True,
        all: bool = False,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        self.timeout = timeout
        toggles: dict[_HackerNewsAction, bool] = {
            "top_stories": all or enable_get_top_stories,
            "user": all or enable_get_user_details,
        }
        self._enabled_actions: tuple[_HackerNewsAction, ...] = tuple(
            action for action, enabled in toggles.items() if enabled
        )
        super().__init__(name="hackers_news", tools=[self.hackernews] if self._enabled_actions else [])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``hackernews`` schema and parameters to enabled actions."""
        if function.name == "hackernews":
            return restrict_actions(function, self._enabled_actions, action_fields=_ACTION_FIELDS)
        return function

    def hackernews(
        self,
        action: _HackerNewsAction,
        num_stories: int | None = None,
        username: str | None = None,
    ) -> str:
        """Fetch top stories or user details from Hacker News as JSON.

        - `top_stories`: Top Hacker News stories (optional `num_stories`, default 10).
        - `user`: User details for `username`.
        """
        if (
            error := require_enabled_action(action, self._enabled_actions)
            or reject_unexpected_args(
                action,
                _ACTION_FIELDS,
                {"num_stories": num_stories is not None, "username": username is not None},
            )
        ) is not None:
            return f"Error: {error}"
        if action == "top_stories":
            return self._top_stories(10 if num_stories is None else num_stories)
        if not username or not username.strip():
            return "Error: username is required for action='user'."
        return self._user_details(username)

    def _top_stories(self, num_stories: int) -> str:
        if num_stories <= 0:
            return "[]"
        try:
            logger.debug("Getting top Hacker News stories", num_stories=num_stories)
            response = httpx.get(f"{_HN_API_BASE}/topstories.json", timeout=self.timeout)
            response.raise_for_status()
            story_ids = response.json()

            stories = []
            for story_id in story_ids[:num_stories]:
                story_response = httpx.get(f"{_HN_API_BASE}/item/{story_id}.json", timeout=self.timeout)
                story_response.raise_for_status()
                story = story_response.json()
                if story is None:
                    continue
                story["username"] = story.get("by", "unknown")
                stories.append(story)
        except Exception as exc:
            logger.exception("Error fetching Hacker News stories")
            return f"Error fetching stories: {exc}"
        return json.dumps(stories)

    def _user_details(self, username: str) -> str:
        try:
            logger.debug("Getting Hacker News user details", username=username)
            encoded_username = quote(username, safe="")
            response = httpx.get(f"{_HN_API_BASE}/user/{encoded_username}.json", timeout=self.timeout)
            response.raise_for_status()
            user = response.json()
            if not isinstance(user, dict):
                return f"Error getting user details: user '{username}' not found"
            user_details = {
                "id": user.get("id"),
                "karma": user.get("karma"),
                "about": user.get("about"),
                "total_items_submitted": len(user.get("submitted", [])),
            }
        except Exception as exc:
            logger.exception("Error getting Hacker News user details", username=username)
            return f"Error getting user details: {exc}"
        return json.dumps(user_details)
