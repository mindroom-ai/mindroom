"""SearxNG search toolkit."""

from __future__ import annotations

import json
from typing import Literal

import httpx

from mindroom.tool_system.authoring import Toolkit

SearxngCategory = Literal["images", "it", "map", "music", "news", "science", "videos"]


class SearxngTools(Toolkit):
    """Search the web and specialized categories through a SearxNG instance."""

    def __init__(
        self,
        host: str,
        engines: list[str] | None = None,
        fixed_max_results: int | None = None,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.engines = list(engines) if engines is not None else None
        self.fixed_max_results = fixed_max_results
        self.timeout = timeout
        super().__init__(name="searxng", tools=[self.searxng_search])

    def searxng_search(
        self,
        query: str,
        category: SearxngCategory | None = None,
        max_results: int = 5,
    ) -> str:
        """Search via SearxNG (general web by default, or a specialized category).

        Args:
            query: Search query string.
            category: Optional vertical; omit for general web search.
            max_results: Maximum results to return (overridden when `fixed_max_results` is configured).

        Returns:
            JSON string of SearxNG search results, or an error message.

        """
        params: dict[str, str] = {"format": "json", "q": query}
        if self.engines:
            params["engines"] = ",".join(self.engines)
        if category is not None:
            params["categories"] = category

        url = f"{self.host.rstrip('/')}/search"
        try:
            response = httpx.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return f"Error fetching results from searxng: {exc}"

        raw_limit = self.fixed_max_results if self.fixed_max_results is not None else max_results
        limit = max(1, int(raw_limit))
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            payload["results"] = payload["results"][:limit]
        return json.dumps(payload)
