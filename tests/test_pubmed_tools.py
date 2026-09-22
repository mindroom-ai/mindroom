"""PubMed results retain identifying metadata at summary-length boundaries."""

from __future__ import annotations

import json

import httpx
import pytest

from mindroom.tools.pubmed import pubmed_tools


@pytest.mark.parametrize("abstract", [None, "Short abstract", "a" * 200, "a" * 201])
@pytest.mark.parametrize("expanded", [False, True])
def test_pubmed_retains_article_metadata(
    monkeypatch: pytest.MonkeyPatch,
    abstract: str | None,
    expanded: bool,
) -> None:
    """Real SDK parsing and toolkit calls keep title/year with any abstract length."""
    abstract_xml = "" if abstract is None else f"<Abstract><AbstractText>{abstract}</AbstractText></Abstract>"
    article_xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>"
        "<ArticleTitle>Example article</ArticleTitle>"
        "<Journal><Title>Example journal</Title><JournalIssue><PubDate><Year>2026</Year>"
        "</PubDate></JournalIssue></Journal>"
        f"{abstract_xml}</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/esearch.fcgi"):
            assert request.url.params["retmax"] == "3"
            return httpx.Response(200, text="<eSearchResult><IdList><Id>123</Id></IdList></eSearchResult>")
        assert request.url.path.endswith("/efetch.fcgi")
        assert request.url.params["id"] == "123"
        return httpx.Response(200, text=article_xml)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "get", client.get)
        toolkit = pubmed_tools()(max_results=3, results_expanded=expanded)
        entrypoint = toolkit.functions["search_pubmed"].entrypoint
        assert entrypoint is not None
        results = json.loads(entrypoint("synthetic query"))

    assert len(paths) == 2
    assert len(results) == 1
    result = results[0]
    assert "Title: Example article" in result
    assert "Published: 2026" in result
    summary = abstract if abstract is not None else "No abstract available"
    if expanded:
        assert "Journal: Example journal" in result
        assert result.endswith(f"Summary:\n{summary}")
    else:
        expected_summary = summary[:200] + ("..." if len(summary) > 200 else "")
        assert result == f"Title: Example article\nPublished: 2026\nSummary: {expected_summary}"
