"""Native Wikipedia toolkit for searching and retrieving summaries."""

from __future__ import annotations

import json

import wikipedia
from wikipedia.exceptions import DisambiguationError

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)


class WikipediaTools(Toolkit):
    """Search Wikipedia and return page summaries."""

    name = "wikipedia_tools"

    def __init__(
        self,
        auto_suggest: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.auto_suggest = auto_suggest
        _ = all
        super().__init__(name="wikipedia_tools")

    @tool
    def search_wikipedia(self, query: str) -> str:
        """Searches Wikipedia for a query.

        Args:
            query: The query to search for.

        Returns:
            Relevant documents from wikipedia.

        """
        logger.info("Searching Wikipedia", query=query)
        try:
            content = wikipedia.summary(query, auto_suggest=self.auto_suggest)
        except DisambiguationError as exc:
            return json.dumps({"disambiguation": query, "options": exc.options})
        except Exception as exc:
            logger.exception("Error searching Wikipedia", query=query)
            return json.dumps({"error": str(exc)})
        return json.dumps({"name": query, "meta_data": {}, "content": content})
