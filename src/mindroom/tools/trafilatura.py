"""Trafilatura web scraping and text extraction toolkit."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.public_fetch import PublicFetchError, PublicFetchResult, fetch_public_url, run_fetch_sync
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.trafilatura_call_limit import CALL_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from configparser import ConfigParser

    from courlan import UrlStore
    from trafilatura.downloads import Response
    from trafilatura.spider import CrawlParameters

logger = get_logger(__name__)

# Crawled pages are downloaded again to extract them after the crawl, so the crawler stops visiting pages at this
# share of the time limit; a default crawl of 10 pages pauses 5 seconds after each one and fits in it.
_CRAWL_SHARE = 0.5
_MAX_BATCH_URLS = 10
_MAX_FILE_SIZE: int | None = None
_visit_page: Callable[[CrawlParameters, bool], CrawlParameters] | None = None
_process_url_store: UrlStore | None = None


def _require_trafilatura() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    """Import and return the Trafilatura functions and settings needed by the toolkit."""
    try:
        from trafilatura import extract, extract_metadata, html2txt
        from trafilatura.downloads import USER_AGENT, Response
        from trafilatura.meta import reset_caches
        from trafilatura.settings import DEFAULT_CONFIG
    except ImportError as exc:
        msg = "`trafilatura` not installed. Please install using `pip install trafilatura`"
        raise ImportError(msg) from exc
    return extract, extract_metadata, html2txt, reset_caches, USER_AGENT, Response, DEFAULT_CONFIG


def _load_spider() -> Any | None:  # noqa: ANN401 - optional trafilatura.spider module
    """Import ``trafilatura.spider`` when available, or return None."""
    try:
        from trafilatura import spider as trafilatura_spider
    except ImportError as exc:
        logger.warning("trafilatura_spider_unavailable", error=str(exc))
        return None
    return trafilatura_spider


def _new_url_store() -> UrlStore:
    from courlan import UrlStore

    # The settings Trafilatura gives its own crawl store.
    return UrlStore(compressed=False, strict=False)


@dataclass
class _Call:
    """One batch extract or crawl: when it started, whether its crawl has fetched its start page, and its crawl state."""

    started: float
    crawl_started: bool = False
    url_store: UrlStore = field(default_factory=_new_url_store)


class _CallUrlStore:
    """Stand-in for Trafilatura's crawl store that answers from the current call's own store."""

    def __getattr__(self, name: str) -> object:
        call = _call.get()
        store = _process_url_store if call is None else call.url_store
        return getattr(store, name)


_call: ContextVar[_Call | None] = ContextVar("trafilatura_call", default=None)


def _seconds_left(share: float = 1.0) -> float | None:
    """Return what is left of ``share`` of the current call's time limit, or None outside a batch extract or crawl."""
    call = _call.get()
    return None if call is None else call.started + share * CALL_SECONDS - time.monotonic()


async def _fetch(url: str, seconds: float | None, *, max_bytes: int, user_agent: str) -> PublicFetchResult:
    async with asyncio.timeout(seconds):
        return await fetch_public_url(url, max_bytes=max_bytes, user_agent=user_agent)


def _fetch_response(url: str, *, decode: bool = False) -> Response | None:
    """Download one page through the shared public fetch, within Trafilatura's size limit and the call's time limit.

    A refused URL raises ``PublicFetchError``; any other failure, including a non-2xx response or a download the
    time limit ends, returns ``None``, and so does every download once the time limit has passed.
    """
    _, _, _, _, user_agent, response_cls, default_config = _require_trafilatura()
    max_bytes = _MAX_FILE_SIZE if _MAX_FILE_SIZE is not None else default_config.getint("DEFAULT", "MAX_FILE_SIZE")
    seconds = _seconds_left()
    if seconds is not None and seconds <= 0:
        return None
    try:
        page = run_fetch_sync(_fetch(url, seconds, max_bytes=max_bytes, user_agent=user_agent))
    except TimeoutError:
        logger.warning("trafilatura_download_time_limit_reached", url=url)
        return None
    except PublicFetchError as error:
        if error.code == "url_not_allowed":
            raise
        logger.warning("trafilatura_download_failed", url=url, error=str(error))
        return None
    # The shared fetch returns only a 2xx body.
    fetched = response_cls(page.content, 200, page.url)
    fetched.decode_data(decode)
    return fetched


def _fetch_url(url: str, config: ConfigParser | None = None) -> str | None:
    """Return decoded HTML from a successful guarded download; the spider's ``config`` is ignored."""
    del config
    response = _fetch_response(url, decode=True)
    return None if response is None else response.html


def _crawl_fetch_response(url: str, *, decode: bool = False, config: ConfigParser | None = None) -> Response | None:
    """Download one crawled page; a refused start page fails the crawl, and a refused link after it is skipped."""
    del config
    call = _call.get()
    start_page = call is None or not call.crawl_started
    if call is not None:
        call.crawl_started = True
    try:
        return _fetch_response(url, decode=decode)
    except PublicFetchError as error:
        if start_page:
            raise
        logger.warning("trafilatura_crawl_skipped_link", url=url, error=str(error))
        return None


def _crawl_page(params: CrawlParameters, initial: bool = False) -> CrawlParameters:
    """Visit the crawl's next page, or end the crawl, leaving its unvisited pages, once its share of time is spent."""
    left = _seconds_left(_CRAWL_SHARE)
    if left is not None and left <= 0:
        params.is_on = False
        return params
    if _visit_page is None:
        msg = "Trafilatura spider guard was not initialized"
        raise RuntimeError(msg)
    return _visit_page(params, initial)


def _pause(seconds: float) -> None:
    """Wait between crawled pages, never past the crawl's share of the call's time limit."""
    left = _seconds_left(_CRAWL_SHARE)
    time.sleep(seconds if left is None else max(min(seconds, left), 0))


@contextmanager
def _time_limit() -> Iterator[None]:
    token = _call.set(_Call(started=time.monotonic()))
    try:
        yield
    finally:
        _call.reset(token)


def install_server_fetch_guard() -> None:
    """Install MindRoom's SSRF-guarded fetchers, per-call deadline hooks, and isolated crawl store on Trafilatura's spider."""
    global _visit_page, _process_url_store
    trafilatura_spider = _load_spider()
    if trafilatura_spider is None:
        return
    if trafilatura_spider.crawl_page is not _crawl_page:
        _visit_page = trafilatura_spider.crawl_page
    if not isinstance(trafilatura_spider.URL_STORE, _CallUrlStore):
        _process_url_store = trafilatura_spider.URL_STORE
    trafilatura_spider.fetch_url = _fetch_url
    trafilatura_spider.fetch_response = _crawl_fetch_response
    trafilatura_spider.crawl_page = _crawl_page
    trafilatura_spider.sleep = _pause
    trafilatura_spider.URL_STORE = _CallUrlStore()


class TrafilaturaTools(Toolkit):
    """Web scraping and text extraction toolkit powered by Trafilatura with SSRF and per-call deadline guards."""

    def __init__(
        self,
        output_format: str = "txt",
        include_comments: bool = True,
        include_tables: bool = True,
        include_images: bool = False,
        include_formatting: bool = False,
        include_links: bool = False,
        with_metadata: bool = False,
        favor_precision: bool = False,
        favor_recall: bool = False,
        target_language: str | None = None,
        deduplicate: bool = False,
        max_tree_size: int | None = None,
        max_crawl_urls: int = 10,
        max_known_urls: int = 100000,
        enable_extract_text: bool = True,
        enable_extract_metadata_only: bool = True,
        enable_html_to_text: bool = True,
        enable_extract_batch: bool = True,
        enable_crawl_website: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        _require_trafilatura()
        install_server_fetch_guard()

        self.output_format = output_format
        self.include_comments = include_comments
        self.include_tables = include_tables
        self.include_images = include_images
        self.include_formatting = include_formatting
        self.include_links = include_links
        self.with_metadata = with_metadata
        self.favor_precision = favor_precision
        self.favor_recall = favor_recall
        self.target_language = target_language
        self.deduplicate = deduplicate
        self.max_tree_size = max_tree_size
        self.max_crawl_urls = max_crawl_urls
        self.max_known_urls = max_known_urls

        tools: list[Callable[..., Any]] = []
        if all or enable_extract_text:
            tools.append(self.extract_text)
        if all or enable_extract_metadata_only:
            tools.append(self.extract_metadata_only)
        if all or enable_html_to_text:
            tools.append(self.html_to_text)
        if all or enable_extract_batch:
            tools.append(self.extract_batch)
        if all or enable_crawl_website:
            if _load_spider() is None:
                logger.warning("trafilatura_crawler_disabled")
            else:
                tools.append(self.crawl_website)

        super().__init__(name="trafilatura_tools", tools=tools)

    def _get_extraction_params(
        self,
        output_format: str | None = None,
        include_comments: bool | None = None,
        include_tables: bool | None = None,
        include_images: bool | None = None,
        include_formatting: bool | None = None,
        include_links: bool | None = None,
        with_metadata: bool | None = None,
        favor_precision: bool | None = None,
        favor_recall: bool | None = None,
        target_language: str | None = None,
        deduplicate: bool | None = None,
        max_tree_size: int | None = None,
        url_blacklist: set[str] | None = None,
        author_blacklist: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build Trafilatura extraction parameters with fallbacks to instance defaults."""
        return {
            "output_format": output_format if output_format is not None else self.output_format,
            "include_comments": include_comments if include_comments is not None else self.include_comments,
            "include_tables": include_tables if include_tables is not None else self.include_tables,
            "include_images": include_images if include_images is not None else self.include_images,
            "include_formatting": include_formatting if include_formatting is not None else self.include_formatting,
            "include_links": include_links if include_links is not None else self.include_links,
            "with_metadata": with_metadata if with_metadata is not None else self.with_metadata,
            "favor_precision": favor_precision if favor_precision is not None else self.favor_precision,
            "favor_recall": favor_recall if favor_recall is not None else self.favor_recall,
            "target_language": target_language if target_language is not None else self.target_language,
            "deduplicate": deduplicate if deduplicate is not None else self.deduplicate,
            "max_tree_size": max_tree_size if max_tree_size is not None else self.max_tree_size,
            "url_blacklist": url_blacklist,
            "author_blacklist": author_blacklist,
        }

    def extract_text(self, url: str, output_format: str | None = None) -> str:
        """Extract main text content from a web page URL using Trafilatura.

        Args:
            url: The URL to extract content from.
            output_format: Output format. Options: 'txt', 'json', 'xml', 'markdown', 'csv', 'html', 'xmltei'.

        Returns:
            Extracted content in the specified format, or an error message if extraction fails.

        """
        extract, _, _, reset_caches, _, _, _ = _require_trafilatura()
        try:
            logger.debug("trafilatura_extract_text", url=url)
            html_content = _fetch_url(url)
            if not html_content:
                return f"Error: Could not fetch content from URL: {url}"

            params = self._get_extraction_params(output_format=output_format)
            result = extract(html_content, url=url, **params)
            if result is None:
                return f"Error: Could not extract readable content from URL: {url}"

            reset_caches()
        except Exception as exc:
            logger.warning("trafilatura_extract_text_failed", url=url, error=str(exc))
            return f"Error extracting text from {url}: {exc}"
        else:
            return result

    def extract_metadata_only(self, url: str, as_json: bool = True) -> str:
        """Extract only metadata from a web page URL.

        Args:
            url: The URL to extract metadata from.
            as_json: Whether to return metadata as JSON string.

        Returns:
            Extracted metadata as JSON string or formatted text.

        """
        _, extract_metadata, _, reset_caches, _, _, _ = _require_trafilatura()
        try:
            logger.debug("trafilatura_extract_metadata", url=url)
            html_content = _fetch_url(url)
            if not html_content:
                return f"Error: Could not fetch content from URL: {url}"

            metadata_doc = extract_metadata(
                html_content,
                default_url=url,
                extensive=True,
                author_blacklist=None,
            )
            if metadata_doc is None:
                return f"Error: Could not extract metadata from URL: {url}"

            metadata_dict = metadata_doc.as_dict()
            reset_caches()
            if as_json:
                return json.dumps(metadata_dict, indent=2, default=str)
            return "\n".join(f"{key}: {value}" for key, value in metadata_dict.items())
        except Exception as exc:
            logger.warning("trafilatura_extract_metadata_failed", url=url, error=str(exc))
            return f"Error extracting metadata from {url}: {exc}"

    def html_to_text(self, html_content: str, clean: bool = True) -> str:
        """Convert HTML content to plain text using Trafilatura's html2txt function.

        Args:
            html_content: The HTML content to convert.
            clean: Whether to remove potentially undesirable elements.

        Returns:
            Plain text extracted from HTML.

        """
        _, _, html2txt, reset_caches, _, _, _ = _require_trafilatura()
        try:
            logger.debug("trafilatura_html_to_text")
            result = html2txt(html_content, clean=clean)
            reset_caches()
        except Exception as exc:
            logger.warning("trafilatura_html_to_text_failed", error=str(exc))
            return f"Error converting HTML to text: {exc}"
        else:
            return result or "Error: Could not extract text from HTML content"

    def extract_batch(self, urls: list[str]) -> str:
        """Extract content from multiple URLs in batch.

        Args:
            urls: List of URLs to extract content from.

        Returns:
            JSON containing batch extraction results.

        """
        if len(urls) > _MAX_BATCH_URLS:
            return f"Error: extract_batch takes at most {_MAX_BATCH_URLS} URLs per call; split the list into several calls."
        extract, _, _, reset_caches, _, _, _ = _require_trafilatura()
        with _time_limit():
            try:
                logger.debug("trafilatura_extract_batch", count=len(urls))
                results: dict[str, str] = {}
                failed_urls: list[str] = []

                for url in urls:
                    try:
                        params = self._get_extraction_params()
                        html_content = _fetch_url(url)
                        if html_content:
                            content = extract(html_content, url=url, **params)
                            if content:
                                results[url] = content
                            else:
                                failed_urls.append(url)
                        else:
                            failed_urls.append(url)
                    except Exception as exc:
                        failed_urls.append(url)
                        results[url] = f"Error: {exc}"

                reset_caches()
                batch_results = {
                    "successful_extractions": len(results)
                    - len([value for value in results.values() if str(value).startswith("Error:")]),
                    "failed_extractions": len(failed_urls),
                    "total_urls": len(urls),
                    "results": results,
                    "failed_urls": failed_urls,
                }
                return json.dumps(batch_results, indent=2, default=str)
            except Exception as exc:
                logger.warning("trafilatura_extract_batch_failed", error=str(exc))
                return f"Error in batch extraction: {exc}"

    def crawl_website(self, homepage_url: str, extract_content: bool = False) -> str:
        """Crawl a website and optionally extract content from discovered pages.

        Args:
            homepage_url: The starting URL (preferably homepage) to crawl from.
            extract_content: Whether to extract content from discovered URLs.

        Returns:
            JSON containing crawl results and optionally extracted content.

        """
        trafilatura_spider = _load_spider()
        if trafilatura_spider is None:
            return "Error: Web crawling functionality not available. Trafilatura spider module could not be imported."
        extract, _, _, reset_caches, _, _, _ = _require_trafilatura()
        install_server_fetch_guard()
        with _time_limit():
            try:
                logger.debug("trafilatura_crawl_website", homepage_url=homepage_url)
                to_visit, known_links = trafilatura_spider.focused_crawler(
                    homepage=homepage_url,
                    max_seen_urls=self.max_crawl_urls,
                    max_known_urls=self.max_known_urls,
                    lang=self.target_language,
                )

                crawl_results: dict[str, Any] = {
                    "homepage": homepage_url,
                    "to_visit": list(to_visit) if to_visit else [],
                    "known_links": list(known_links) if known_links else [],
                    "stats": {
                        "urls_to_visit": len(to_visit) if to_visit else 0,
                        "known_links_count": len(known_links) if known_links else 0,
                    },
                }

                if extract_content and known_links:
                    logger.debug("trafilatura_crawl_extract_content")
                    extracted_content: dict[str, str] = {}
                    urls_to_extract = list(known_links)[: min(10, len(known_links))]
                    for url in urls_to_extract:
                        try:
                            params = self._get_extraction_params()
                            html_content = _fetch_url(url)
                            if html_content:
                                content = extract(html_content, url=url, **params)
                                if content:
                                    extracted_content[url] = content
                        except Exception as exc:
                            extracted_content[url] = f"Error extracting content: {exc}"
                    crawl_results["extracted_content"] = extracted_content

                reset_caches()
                return json.dumps(crawl_results, indent=2, default=str)
            except Exception as exc:
                logger.warning("trafilatura_crawl_website_failed", homepage_url=homepage_url, error=str(exc))
                return f"Error crawling website {homepage_url}: {exc}"


@register_tool_with_metadata(
    name="trafilatura",
    file_access=ToolFileAccess.NONE,
    display_name="Trafilatura",
    description="Extract text and metadata from web pages, crawl websites, and convert HTML to text",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaFileAlt",
    icon_color="text-teal-500",
    config_fields=[
        ConfigField(
            name="output_format",
            label="Output Format",
            type="text",
            required=False,
            default="txt",
            description="Output format: txt, json, markdown, xml, csv, or html",
        ),
        ConfigField(
            name="include_comments",
            label="Include Comments",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="include_tables",
            label="Include Tables",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="include_images",
            label="Include Images",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="include_formatting",
            label="Include Formatting",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="include_links",
            label="Include Links",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="with_metadata",
            label="Include Metadata",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="favor_precision",
            label="Favor Precision",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="favor_recall",
            label="Favor Recall",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="target_language",
            label="Target Language",
            type="text",
            required=False,
            default=None,
            placeholder="e.g., en, de, fr (ISO 639-1)",
        ),
        ConfigField(
            name="deduplicate",
            label="Deduplicate",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="max_tree_size",
            label="Max Tree Size",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="max_crawl_urls",
            label="Max Crawl URLs",
            type="number",
            required=False,
            default=10,
        ),
        ConfigField(
            name="max_known_urls",
            label="Max Known URLs",
            type="number",
            required=False,
            default=100000,
        ),
        ConfigField(
            name="enable_extract_text",
            label="Enable Extract Text",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_extract_metadata_only",
            label="Enable Extract Metadata Only",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_html_to_text",
            label="Enable HTML to Text",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_extract_batch",
            label="Enable Extract Batch",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_crawl_website",
            label="Enable Crawl Website",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["trafilatura"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#trafilatura",
    function_names=("crawl_website", "extract_batch", "extract_metadata_only", "extract_text", "html_to_text"),
)
def trafilatura_tools() -> type[TrafilaturaTools]:
    """Return Trafilatura tools whose downloads follow the server-fetch policy within one time limit per call."""
    _require_trafilatura()
    install_server_fetch_guard()
    return TrafilaturaTools
