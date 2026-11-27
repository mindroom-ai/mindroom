"""PubMed toolkit behavior over faked NCBI E-utilities responses."""

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
    """XML parsing and toolkit calls keep title/year with any abstract length."""
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
        results = json.loads(toolkit.functions["pubmed_search"]("synthetic query"))

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


def test_pubmed_expanded_metadata_labeled_abstracts_and_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expanded mode preserves author, DOI/PMC URLs, keywords, MeSH terms, and labeled abstracts."""
    article_xml = (
        "<PubmedArticleSet><PubmedArticle>"
        "<MedlineCitation><PMID>456</PMID><Article>"
        "<ArticleTitle>Study of <i>E. coli</i></ArticleTitle>"
        "<Journal><Title>Microbiology</Title><JournalIssue><PubDate>"
        "<Year>2025</Year></PubDate></JournalIssue></Journal>"
        "<Abstract>"
        '<AbstractText Label="METHODS">Cultured samples.</AbstractText>'
        '<AbstractText Label="RESULTS">Observed growth.</AbstractText>'
        "</Abstract>"
        "<AuthorList><Author><LastName>Pasteur</LastName><ForeName>Louis</ForeName></Author></AuthorList>"
        "<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>"
        "</Article>"
        "<MeshHeadingList><MeshHeading><DescriptorName>Bacteria</DescriptorName></MeshHeading></MeshHeadingList>"
        "<KeywordList><Keyword>microbes</Keyword><Keyword>culture</Keyword></KeywordList>"
        "</MedlineCitation>"
        "<PubmedData><ArticleIdList>"
        '<ArticleId IdType="doi">10.1000/example</ArticleId>'
        '<ArticleId IdType="pmc">PMC999</ArticleId>'
        "</ArticleIdList></PubmedData>"
        "</PubmedArticle></PubmedArticleSet>"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esearch.fcgi"):
            assert request.url.params["email"] == "researcher@example.org"
            assert request.url.params["retmax"] == "10"
            return httpx.Response(200, text="<eSearchResult><IdList><Id>456</Id></IdList></eSearchResult>")
        return httpx.Response(200, text=article_xml)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "get", client.get)
        toolkit = pubmed_tools()(email="researcher@example.org", results_expanded=True)
        (entry,) = json.loads(toolkit.pubmed_search("bacteria"))

    assert "Title: Study of E. coli" in entry
    assert "First Author: Pasteur, Louis" in entry
    assert "DOI: 10.1000/example" in entry
    assert "PubMed URL: https://pubmed.ncbi.nlm.nih.gov/456/" in entry
    assert "Full Text URL: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC999/" in entry
    assert "Keywords: microbes, culture" in entry
    assert "MeSH Terms: Bacteria" in entry
    assert "Publication Type: Journal Article" in entry
    assert entry.endswith("Summary:\nMETHODS: Cultured samples.\n\nRESULTS: Observed growth.")


def test_pubmed_empty_results_errors_and_enable_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ID lists skip EFetch, HTTP errors return the error string, and enable flags control functions."""
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.params.get("term") == "empty":
            return httpx.Response(200, text="<eSearchResult><IdList/></eSearchResult>")
        return httpx.Response(500, text="server error")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "get", client.get)
        toolkit = pubmed_tools()()
        assert toolkit.pubmed_search("empty") == "[]"
        assert len(paths) == 1
        assert toolkit.pubmed_search("fail").startswith("Could not fetch articles. Error:")

    disabled = pubmed_tools()(enable_search_pubmed=False)
    assert disabled.name == "pubmed"
    assert disabled.functions == {}
    assert list(pubmed_tools()(enable_search_pubmed=False, all=True).functions) == ["pubmed_search"]
