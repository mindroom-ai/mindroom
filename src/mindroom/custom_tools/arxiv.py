"""Native ArXiv toolkit for searching papers and reading arXiv PDFs."""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional
from urllib.parse import urlparse

import arxiv
import requests
from pypdf import PdfReader

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

_PDF_DOWNLOAD_TIMEOUT_SECONDS = 60
_PDF_CHUNK_SIZE_BYTES = 65536
_EXPORT_ARXIV_HOST = "export.arxiv.org"
_ALLOWED_PDF_SCHEMES = frozenset({"http", "https"})


class _BoundedArxivClient(arxiv.Client):
    """Cap each API page at the remaining result limit without changing the shared client."""

    def _format_url(self, search: arxiv.Search, start: int, page_size: int) -> str:
        # arxiv 4 caps what results() yields, not how many articles it fetches per page.
        if search.max_results is not None:
            page_size = min(page_size, search.max_results - start)
        return super()._format_url(search, start, page_size)


def _article_metadata(result: arxiv.Result) -> dict[str, Any]:
    """Serialize one arXiv search result into the article dictionary."""
    return {
        "title": result.title,
        "id": result.get_short_id(),
        "entry_id": result.entry_id,
        "authors": [author.name for author in result.authors],
        "primary_category": result.primary_category,
        "categories": result.categories,
        "published": result.published.isoformat() if result.published else None,
        "pdf_url": result.pdf_url,
        "links": [link.href for link in result.links],
        "summary": result.summary,
        "comment": result.comment,
    }


def _pdf_filename(result: arxiv.Result) -> str:
    """Build the sanitized PDF filename for an arXiv result."""
    short_id = result.get_short_id().replace("/", "_").replace("\\", "_")
    sanitized_title = re.sub(r"[^\w]", "_", result.title or "UNTITLED")
    return f"{short_id}.{sanitized_title}.pdf"


def _download_pdf(result: arxiv.Result, download_dir: Path) -> Path:
    """Download a result's PDF from export.arxiv.org into ``download_dir`` with path guards."""
    if result.pdf_url is None:
        msg = "No PDF URL available for this result"
        raise ValueError(msg)

    parsed_url = urlparse(result.pdf_url)
    if parsed_url.scheme not in _ALLOWED_PDF_SCHEMES:
        msg = f"Unsupported PDF URL scheme: {parsed_url.scheme!r}"
        raise ValueError(msg)

    filename = _pdf_filename(result)
    resolved_dir = download_dir.resolve()
    candidate = resolved_dir / filename
    if candidate.is_symlink():
        msg = f"Refusing to overwrite symlinked PDF path: {filename!r}"
        raise ValueError(msg)
    path = candidate.resolve()
    if path.parent != resolved_dir:
        msg = f"Invalid arXiv PDF filename: {filename!r}"
        raise ValueError(msg)

    pdf_url = parsed_url._replace(netloc=_EXPORT_ARXIV_HOST).geturl()
    with requests.get(pdf_url, stream=True, timeout=_PDF_DOWNLOAD_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        with path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=_PDF_CHUNK_SIZE_BYTES):
                if chunk:
                    file.write(chunk)
    return path


class ArxivTools(Toolkit):
    """Search arXiv and extract text from arXiv paper PDFs."""

    name = "arxiv_tools"

    def __init__(
        self,
        enable_search_arxiv: bool = True,
        enable_read_arxiv_papers: bool = True,
        all: bool = False,  # noqa: A002
        download_dir: Path | str | None = None,
    ) -> None:
        self.client: arxiv.Client = _BoundedArxivClient()
        self.download_dir = Path(download_dir) if download_dir else None

        super().__init__(name="arxiv_tools")
        if not (all or enable_search_arxiv):
            self.functions.pop(self.arxiv_search.__name__, None)
        if not (all or enable_read_arxiv_papers):
            self.functions.pop(self.arxiv_read_papers.__name__, None)

    @tool
    def arxiv_search(self, query: str, num_articles: int = 10) -> str:
        """Use this function to search arXiv for a query and return the top articles.

        Args:
            query (str): The query to search arXiv for.
            num_articles (int, optional): The number of articles to return. Defaults to 10.

        Returns:
            str: A JSON of the articles with title, id, authors, pdf_url and summary.

        """
        if num_articles <= 0:
            return "[]"
        articles: list[dict[str, Any]] = []
        logger.debug("Searching arXiv", query=query)
        search = arxiv.Search(
            query=query,
            max_results=num_articles,
            sort_by=arxiv.SortCriterion.Relevance,
            sort_order=arxiv.SortOrder.Descending,
        )
        for result in self.client.results(search=search):
            try:
                articles.append(_article_metadata(result))
            except Exception:
                logger.exception("Error processing article")
        return json.dumps(articles, indent=4)

    @tool
    def arxiv_read_papers(
        self,
        id_list: list[str],
        pages_to_read: Optional[int] = None,  # noqa: UP045 - Optional keeps null out of the tool schema.
    ) -> str:
        """Use this function to read a list of arxiv papers and return the content.

        Args:
            id_list (list, str): The list of `id` of the papers to add to the knowledge base.
                    Should be of the format: ["2103.03404v1", "2103.03404v2"]
            pages_to_read (int, optional): The number of pages to read from the paper.
                    None means read all pages. Defaults to None.

        Returns:
            str: JSON of the papers.

        """
        directory = nullcontext(self.download_dir) if self.download_dir else TemporaryDirectory(prefix="arxiv_pdfs_")
        with directory as download_dir:
            return self._read_papers(id_list, pages_to_read, Path(download_dir))

    def _read_papers(self, id_list: list[str], pages_to_read: int | None, download_dir: Path) -> str:
        download_dir.mkdir(parents=True, exist_ok=True)
        articles: list[dict[str, Any]] = []
        logger.debug("Reading arXiv papers", id_list=id_list)
        for result in self.client.results(search=arxiv.Search(id_list=id_list)):
            try:
                article = _article_metadata(result)
                if result.pdf_url:
                    logger.debug("Downloading arXiv PDF", pdf_url=result.pdf_url)
                    pdf_path = _download_pdf(result, download_dir)
                    logger.debug("Downloaded arXiv PDF", pdf_path=str(pdf_path))
                    pdf_reader = PdfReader(pdf_path)
                    content: list[dict[str, Any]] = []
                    for page_number, page in enumerate(pdf_reader.pages, start=1):
                        if pages_to_read is not None and page_number > pages_to_read:
                            break
                        content.append({"page": page_number, "text": page.extract_text()})
                    article["content"] = content
                articles.append(article)
            except Exception:
                logger.exception("Error processing article")
        return json.dumps(articles, indent=4)
