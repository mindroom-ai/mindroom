"""Native Giphy GIF search toolkit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from mindroom.llm.media import Image
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"


class GiphyTools(Toolkit):
    """Search Giphy for animated GIFs."""

    def __init__(
        self,
        api_key: str | None = None,
        limit: int = 1,
        enable_search_gifs: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.api_key = api_key
        if not self.api_key:
            logger.error("No Giphy API key provided")
        self.limit = limit

        tools: list[Callable[..., Any]] = []
        if all or enable_search_gifs:
            tools.append(self.search_gifs)
        super().__init__(name="giphy", tools=tools)

    def search_gifs(self, query: str) -> ToolValue:
        """Search Giphy for GIFs matching a query.

        Args:
            query: Search text describing the GIF.

        """
        try:
            response = httpx.get(
                _GIPHY_SEARCH_URL,
                params={"api_key": self.api_key, "q": query, "limit": self.limit},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            gif_urls: list[str] = []
            images: list[Image] = []
            for gif in data.get("data", []):
                original = gif.get("images", {}).get("original", {})
                gif_url = original.get("url")
                if not gif_url:
                    continue
                gif_urls.append(gif_url)
                images.append(
                    Image(
                        id=str(uuid4()),
                        url=gif_url,
                        alt_text=gif.get("alt_text"),
                        revised_prompt=query,
                    ),
                )
            if images:
                return ToolValue(content=f"Found {len(gif_urls)} GIF(s): {gif_urls}", images=tuple(images))
            return ToolValue(content="No gifs found")
        except httpx.HTTPStatusError as exc:
            logger.exception("Giphy HTTP error", status_code=exc.response.status_code)
            return ToolValue(content=f"HTTP error occurred: {exc.response.status_code}")
        except Exception as exc:
            logger.exception("Giphy search error")
            return ToolValue(content=f"An error occurred: {exc}")
