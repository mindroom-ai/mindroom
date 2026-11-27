"""Native PubMed toolkit backed by NCBI E-utilities over httpx."""

from __future__ import annotations

import json
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_DEFAULT_MAX_RESULTS = 10
_CONCISE_SUMMARY_LIMIT = 200


def _element_text(element: ET.Element | None, default: str) -> str:
    """Return full nested text from an XML element, or ``default`` when absent or empty."""
    if element is None:
        return default
    text = "".join(element.itertext())
    return text or default


def _abstract_text(article: ET.Element) -> str:
    """Join PubMed abstract sections, keeping section labels when present."""
    sections = article.findall(".//AbstractText")
    if not sections:
        return "No abstract available"
    parts: list[str] = []
    for section in sections:
        text = "".join(section.itertext())
        if not text:
            continue
        label = section.get("Label", "")
        parts.append(f"{label}: {text}" if label else text)
    return "\n\n".join(parts).strip() or "No abstract available"


def _first_author(article: ET.Element) -> str:
    """Return the first listed author formatted as ``Last, Fore`` or ``Last``."""
    first_author_elem = article.find(".//AuthorList/Author[1]")
    if first_author_elem is None:
        return "Unknown"
    last_name = first_author_elem.find("LastName")
    fore_name = first_author_elem.find("ForeName")
    if last_name is not None and fore_name is not None and last_name.text and fore_name.text:
        return f"{last_name.text}, {fore_name.text}"
    if last_name is not None and last_name.text:
        return last_name.text
    return "Unknown"


def _format_article(article: dict[str, Any], *, expanded: bool) -> str:
    """Format one parsed PubMed article in concise or expanded text form."""
    if expanded:
        return (
            f"Published: {article.get('Published')}\n"
            f"Title: {article.get('Title')}\n"
            f"First Author: {article.get('First_Author')}\n"
            f"Journal: {article.get('Journal')}\n"
            f"Publication Type: {article.get('Publication_Type')}\n"
            f"DOI: {article.get('DOI')}\n"
            f"PubMed URL: {article.get('PubMed_URL')}\n"
            f"Full Text URL: {article.get('Full_Text_URL')}\n"
            f"Keywords: {article.get('Keywords')}\n"
            f"MeSH Terms: {article.get('MeSH_Terms')}\n"
            f"Summary:\n{article.get('Summary')}"
        )
    summary = str(article.get("Summary", ""))
    truncated = f"{summary[:_CONCISE_SUMMARY_LIMIT]}..." if len(summary) > _CONCISE_SUMMARY_LIMIT else summary
    return f"Title: {article.get('Title')}\nPublished: {article.get('Published')}\nSummary: {truncated}"


class PubmedTools(Toolkit):
    """Search PubMed via NCBI E-utilities and return formatted article summaries."""

    name = "pubmed"

    def __init__(
        self,
        email: str = "your_email@example.com",
        max_results: int | None = None,
        results_expanded: bool = False,
        enable_search_pubmed: bool = True,
        all: bool = False,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        self.email = email
        self.max_results = max_results
        self.results_expanded = results_expanded
        self.timeout = timeout
        super().__init__(name="pubmed")
        if not (all or enable_search_pubmed):
            self.functions.pop(self.pubmed_search.__name__, None)

    def fetch_pubmed_ids(self, query: str, max_results: int, email: str) -> list[str]:
        """Fetch matching PubMed IDs from NCBI ESearch."""
        params: dict[str, str | int] = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "email": email,
            "usehistory": "y",
        }
        response = httpx.get(_ESEARCH_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        root = ET.fromstring(response.content)  # noqa: S314
        return [id_elem.text for id_elem in root.findall(".//Id") if id_elem.text is not None]

    def fetch_details(self, pubmed_ids: list[str]) -> ET.Element:
        """Fetch full XML article metadata for ``pubmed_ids`` from NCBI EFetch."""
        params = {"db": "pubmed", "id": ",".join(pubmed_ids), "retmode": "xml"}
        response = httpx.get(_EFETCH_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        return ET.fromstring(response.content)  # noqa: S314

    def parse_details(self, xml_root: ET.Element) -> list[dict[str, Any]]:
        """Parse NCBI EFetch XML into structured PubMed article dictionaries."""
        articles: list[dict[str, Any]] = []
        for article in xml_root.findall(".//PubmedArticle"):
            doi_elem = article.find(".//ArticleIdList/ArticleId[@IdType='doi']")
            doi = doi_elem.text if doi_elem is not None and doi_elem.text else "No DOI available"

            pmid_elem = article.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None and pmid_elem.text else ""
            pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "No URL available"

            pmc_elem = article.find(".//ArticleIdList/ArticleId[@IdType='pmc']")
            full_text_url = "Not available"
            if pmc_elem is not None and pmc_elem.text:
                full_text_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_elem.text}/"
            elif doi_elem is not None and doi_elem.text:
                full_text_url = f"https://doi.org/{doi}"

            keywords = [kw.text for kw in article.findall(".//KeywordList/Keyword") if kw.text]
            mesh_terms = [mesh.text for mesh in article.findall(".//MeshHeading/DescriptorName") if mesh.text]
            pub_types = [pt.text for pt in article.findall(".//PublicationTypeList/PublicationType") if pt.text]

            articles.append(
                {
                    "Published": _element_text(article.find(".//PubDate/Year"), "No date available"),
                    "Title": _element_text(article.find(".//ArticleTitle"), "No title available"),
                    "Summary": _abstract_text(article),
                    "First_Author": _first_author(article),
                    "DOI": doi,
                    "PubMed_URL": pubmed_url,
                    "Full_Text_URL": full_text_url,
                    "Keywords": ", ".join(keywords) if keywords else "No keywords available",
                    "MeSH_Terms": ", ".join(mesh_terms) if mesh_terms else "No MeSH terms available",
                    "Journal": _element_text(article.find(".//Journal/Title"), "Unknown Journal"),
                    "Publication_Type": ", ".join(pub_types) if pub_types else "Not specified",
                },
            )
        return articles

    @tool
    def pubmed_search(self, query: str, max_results: int | None = None) -> str:
        """Search PubMed, using the configured max_results when the call omits it."""
        effective_max_results = self.max_results if max_results is None else max_results
        if effective_max_results is None:
            effective_max_results = _DEFAULT_MAX_RESULTS
        if effective_max_results <= 0:
            return "[]"

        try:
            logger.debug("Searching PubMed", query=query, max_results=effective_max_results)
            ids = self.fetch_pubmed_ids(query, effective_max_results, self.email)
            if not ids:
                return "[]"
            details_root = self.fetch_details(ids)
            articles = self.parse_details(details_root)
            results = [_format_article(article, expanded=self.results_expanded) for article in articles]
        except Exception as exc:
            return f"Could not fetch articles. Error: {exc}"
        return json.dumps(results)
