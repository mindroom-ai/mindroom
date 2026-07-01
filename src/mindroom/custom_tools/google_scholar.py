"""Google Scholar tools for MindRoom agents.

Google Scholar has no official API, so this toolkit uses the scholarly
library, the de-facto standard unofficial Google Scholar client.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agno.tools import Toolkit
from scholarly import MaxTriesExceededException, scholarly

_RATE_LIMIT_MESSAGE = "Google Scholar is currently blocking automated requests. Try again later."


def _search_publications(query: str, limit: int) -> list[dict[str, Any]]:
    """Collect publication summaries from a blocking scholarly search."""
    publications: list[dict[str, Any]] = []
    for publication in scholarly.search_pubs(query):
        bib = publication.get("bib", {})
        publications.append(
            {
                "title": bib.get("title"),
                "authors": bib.get("author"),
                "year": bib.get("pub_year"),
                "venue": bib.get("venue"),
                "abstract": bib.get("abstract"),
                "citations": publication.get("num_citations"),
                "url": publication.get("pub_url"),
                "pdf_url": publication.get("eprint_url"),
            },
        )
        if len(publications) >= limit:
            break
    return publications


class GoogleScholarTools(Toolkit):
    """Tools for searching academic publications on Google Scholar."""

    def __init__(
        self,
        max_results: int = 5,
        **kwargs: Any,  # noqa: ANN401 - mirrors Agno Toolkit passthrough kwargs.
    ) -> None:
        """Initialize Google Scholar tools."""
        self.max_results = max_results
        super().__init__(name="google_scholar_tools", tools=[self.search_google_scholar], **kwargs)

    async def search_google_scholar(self, query: str, max_results: int | None = None) -> str:
        """Search Google Scholar for academic publications.

        Args:
            query: Search terms, such as a topic, paper title, or author name.
            max_results: Maximum number of publications to return (defaults to the configured limit).

        Returns:
            A JSON list of publications with title, authors, year, venue, abstract,
            citation count, and publication/PDF URLs.

        """
        limit = max_results if max_results is not None else self.max_results
        try:
            publications = await asyncio.to_thread(_search_publications, query, limit)
        except MaxTriesExceededException:
            return _RATE_LIMIT_MESSAGE
        return json.dumps(publications, indent=2)
