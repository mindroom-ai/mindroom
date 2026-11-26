"""Native Baidu Search toolkit backed by the baidusearch package."""

from __future__ import annotations

import json
from typing import Any

import pycountry
from baidusearch.baidusearch import search

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)


def _normalize_language(language: str) -> str:
    """Normalize a language code or name to ISO 639-1 alpha-2, falling back to 'zh'."""
    if len(language) == 2:
        return language
    try:
        return pycountry.languages.lookup(language).alpha_2
    except (LookupError, AttributeError):
        return "zh"


class BaiduSearchTools(Toolkit):
    """Search the web using Baidu."""

    name = "baidusearch"

    def __init__(
        self,
        fixed_max_results: int | None = None,
        fixed_language: str | None = None,
        headers: Any = None,  # noqa: ANN401
        proxy: str | None = None,
        timeout: int | None = 10,
        debug: bool | None = False,
        enable_baidu_search: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.fixed_max_results = fixed_max_results
        self.fixed_language = fixed_language
        self.headers = headers
        self.proxy = proxy
        self.timeout = timeout
        self.debug = debug

        super().__init__(name="baidusearch")
        if not (all or enable_baidu_search):
            self.functions.pop(self.baidu_search.__name__, None)

    @tool(description="Execute Baidu search and return results")
    def baidu_search(self, query: str, max_results: int = 5, language: str = "zh") -> str:
        """Execute Baidu search and return results.

        Args:
            query (str): Search keyword
            max_results (int, optional): Maximum number of results to return, default 5
            language (str, optional): Search language, default Chinese

        Returns:
            str: A JSON formatted string containing the search results.

        """
        effective_max_results = self.fixed_max_results if self.fixed_max_results is not None else max_results
        if effective_max_results <= 0:
            return "[]"

        effective_language = _normalize_language(self.fixed_language or language)

        logger.debug("Searching Baidu", language=effective_language, query=query)

        results = search(keyword=query, num_results=effective_max_results) or []
        formatted: list[dict[str, str]] = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "abstract": item.get("abstract", ""),
                "rank": str(idx),
            }
            for idx, item in enumerate(results, 1)
        ]
        return json.dumps(formatted, indent=2)
