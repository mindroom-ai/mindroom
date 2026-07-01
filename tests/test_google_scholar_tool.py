"""Tests for the Google Scholar toolkit."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from scholarly import MaxTriesExceededException

from mindroom.custom_tools import google_scholar as google_scholar_module
from mindroom.custom_tools.google_scholar import GoogleScholarTools

if TYPE_CHECKING:
    import pytest

_PUBLICATION = {
    "bib": {
        "title": "Attention is all you need",
        "author": ["A Vaswani", "N Shazeer"],
        "pub_year": "2017",
        "venue": "NeurIPS",
        "abstract": "The dominant sequence transduction models are based on RNNs.",
    },
    "num_citations": 256704,
    "pub_url": "https://example.org/paper",
    "eprint_url": "https://example.org/paper.pdf",
}


def test_search_google_scholar_returns_publication_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool flattens scholarly publication dicts into a compact JSON list."""
    monkeypatch.setattr(
        google_scholar_module.scholarly,
        "search_pubs",
        lambda _query: iter([_PUBLICATION]),
    )

    result = asyncio.run(GoogleScholarTools().search_google_scholar("attention is all you need"))

    publications = json.loads(result)
    assert publications == [
        {
            "title": "Attention is all you need",
            "authors": ["A Vaswani", "N Shazeer"],
            "year": "2017",
            "venue": "NeurIPS",
            "abstract": "The dominant sequence transduction models are based on RNNs.",
            "citations": 256704,
            "url": "https://example.org/paper",
            "pdf_url": "https://example.org/paper.pdf",
        },
    ]


def test_search_google_scholar_limits_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured max_results caps how many publications are consumed."""
    monkeypatch.setattr(
        google_scholar_module.scholarly,
        "search_pubs",
        lambda _query: iter([_PUBLICATION] * 10),
    )

    tools = GoogleScholarTools(max_results=3)
    assert len(json.loads(asyncio.run(tools.search_google_scholar("attention")))) == 3
    assert len(json.loads(asyncio.run(tools.search_google_scholar("attention", max_results=2)))) == 2


def test_search_google_scholar_reports_rate_limiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked scrape returns a friendly message instead of raising."""

    def raise_blocked(_query: str) -> Any:  # noqa: ANN401 - mirrors untyped scholarly API.
        raise MaxTriesExceededException

    monkeypatch.setattr(google_scholar_module.scholarly, "search_pubs", raise_blocked)

    result = asyncio.run(GoogleScholarTools().search_google_scholar("attention"))
    assert result == google_scholar_module._RATE_LIMIT_MESSAGE
