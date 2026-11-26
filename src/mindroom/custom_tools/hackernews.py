"""Native Hacker News toolkit backed by the Firebase REST API."""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

_HN_API_BASE = "https://hacker-news.firebaseio.com/v0"


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
        super().__init__(name="hackers_news")
        if not (all or enable_get_top_stories):
            self.functions.pop(self.get_top_hackernews_stories.__name__, None)
        if not (all or enable_get_user_details):
            self.functions.pop(self.get_user_details.__name__, None)

    @tool
    def get_top_hackernews_stories(self, num_stories: int = 10) -> str:
        """Use this function to get top stories from Hacker News.

        Args:
            num_stories (int): Number of stories to return. Defaults to 10.

        Returns:
            str: JSON string of top stories.

        """
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

    @tool
    def get_user_details(self, username: str) -> str:
        """Use this function to get the details of a Hacker News user using their username.

        Args:
            username (str): Username of the user to get details for.

        Returns:
            str: JSON string of the user details.

        """
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
