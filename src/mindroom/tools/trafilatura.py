"""Trafilatura web scraping and text extraction toolkit."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from mindroom.logging_config import get_logger
from mindroom.public_fetch import PublicFetchError, PublicFetchResult, fetch_public_url, run_fetch_sync
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import reject_unexpected_args, require_enabled_action, restrict_actions
from mindroom.trafilatura_call_limit import CALL_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from configparser import ConfigParser

    from courlan import UrlStore
    from trafilatura.downloads import Response
    from trafilatura.spider import CrawlParameters

    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

# Crawled pages are downloaded again to extract them after the crawl, so the crawler stops visiting pages at this
# share of the time limit; a default crawl of 10 pages pauses 5 seconds after each one and fits in it.
_CRAWL_SHARE = 0.5
_MAX_BATCH_URLS = 10
_MAX_FILE_SIZE: int | None = None
_visit_page: Callable[[CrawlParameters, bool], CrawlParameters] | None = None
_process_url_store: UrlStore | None = None

_ExtractAction = Literal["text", "metadata", "batch"]
_EXTRACT_ACTION_FIELDS: dict[str, str] = {
    "text": "url output_format",
    "metadata": "url as_json",
    "batch": "urls",
}


class _TrafilaturaImports(NamedTuple):
    extract: Any
    extract_metadata: Any
    html2txt: Any
    reset_caches: Any
    user_agent: str
    response_cls: Any
    default_config: ConfigParser


def _require_trafilatura() -> _TrafilaturaImports:
    """Import and return the Trafilatura functions and settings needed by the toolkit."""
    from trafilatura import extract, extract_metadata, html2txt
    from trafilatura.downloads import USER_AGENT, Response
    from trafilatura.meta import reset_caches
    from trafilatura.settings import DEFAULT_CONFIG

    return _TrafilaturaImports(
        extract=extract,
        extract_metadata=extract_metadata,
        html2txt=html2txt,
        reset_caches=reset_caches,
        user_agent=USER_AGENT,
        response_cls=Response,
        default_config=DEFAULT_CONFIG,
    )


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
    trafilatura = _require_trafilatura()
    max_bytes = (
        _MAX_FILE_SIZE if _MAX_FILE_SIZE is not None else trafilatura.default_config.getint("DEFAULT", "MAX_FILE_SIZE")
    )
    seconds = _seconds_left()
    if seconds is not None and seconds <= 0:
        return None
    try:
        page = run_fetch_sync(_fetch(url, seconds, max_bytes=max_bytes, user_agent=trafilatura.user_agent))
    except TimeoutError:
        logger.warning("trafilatura_download_time_limit_reached", url=url)
        return None
    except PublicFetchError as error:
        if error.code == "url_not_allowed":
            raise
        logger.warning("trafilatura_download_failed", url=url, error=str(error))
        return None
    # The shared fetch returns only a 2xx body.
    fetched = trafilatura.response_cls(page.content, 200, page.url)
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


def _install_server_fetch_guard() -> None:
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


class _TrafilaturaTools(Toolkit):
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
        _install_server_fetch_guard()

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

        extract_toggles: dict[_ExtractAction, bool] = {
            "text": all or enable_extract_text,
            "metadata": all or enable_extract_metadata_only,
            "batch": all or enable_extract_batch,
        }
        self._extract_actions: tuple[_ExtractAction, ...] = tuple(
            action for action, enabled in extract_toggles.items() if enabled
        )

        tools: list[Callable[..., Any]] = []
        if self._extract_actions:
            tools.append(self.trafilatura_extract)
        if all or enable_html_to_text:
            tools.append(self.html_to_text)
        if all or enable_crawl_website:
            if _load_spider() is None:
                logger.warning("trafilatura_crawler_disabled")
            else:
                tools.append(self.crawl_website)

        super().__init__(name="trafilatura_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``trafilatura_extract`` schema and parameters to enabled actions."""
        if function.name == "trafilatura_extract":
            return restrict_actions(function, self._extract_actions, action_fields=_EXTRACT_ACTION_FIELDS)
        return function

    def _get_extraction_params(self, output_format: str | None = None) -> dict[str, Any]:
        """Build Trafilatura extraction parameters from instance defaults and optional ``output_format``."""
        return {
            "output_format": output_format if output_format is not None else self.output_format,
            "include_comments": self.include_comments,
            "include_tables": self.include_tables,
            "include_images": self.include_images,
            "include_formatting": self.include_formatting,
            "include_links": self.include_links,
            "with_metadata": self.with_metadata,
            "favor_precision": self.favor_precision,
            "favor_recall": self.favor_recall,
            "target_language": self.target_language,
            "deduplicate": self.deduplicate,
            "max_tree_size": self.max_tree_size,
            "url_blacklist": None,
            "author_blacklist": None,
        }

    def _fetch_and_extract(self, trafilatura: _TrafilaturaImports, url: str) -> str | None:
        html_content = _fetch_url(url)
        if not html_content:
            return None
        content = trafilatura.extract(html_content, url=url, **self._get_extraction_params())
        return content or None

    def trafilatura_extract(
        self,
        action: _ExtractAction,
        url: str | None = None,
        urls: list[str] | None = None,
        output_format: str | None = None,
        as_json: bool | None = None,
    ) -> str:
        """Extract from URL(s) with Trafilatura (for raw HTML strings use `html_to_text`; to crawl a site use `crawl_website`).

        - `text`: Extract main content from `url` (optional `output_format`: txt, json, xml, markdown, csv, html, xmltei).
        - `metadata`: Extract metadata only from `url` (optional `as_json`, default True).
        - `batch`: Extract main content from up to 10 `urls` and return JSON results.
        """
        if (disabled := require_enabled_action(action, self._extract_actions)) is not None:
            return f"Error: {disabled}"
        if (
            unexpected := reject_unexpected_args(
                action,
                _EXTRACT_ACTION_FIELDS,
                {
                    "url": url is not None,
                    "urls": urls is not None,
                    "output_format": output_format is not None,
                    "as_json": as_json is not None,
                },
            )
        ) is not None:
            return f"Error: {unexpected}"
        if action == "batch":
            return "Error: urls is required for batch extraction." if urls is None else self._extract_batch(urls)
        if not url:
            return f"Error: url is required for {action} extraction."
        if action == "text":
            return self._extract_text(url, output_format=output_format)
        return self._extract_metadata(url, as_json=True if as_json is None else as_json)

    def _extract_text(self, url: str, *, output_format: str | None) -> str:
        trafilatura = _require_trafilatura()
        try:
            logger.debug("trafilatura_extract_text", url=url)
            html_content = _fetch_url(url)
            if not html_content:
                return f"Error: Could not fetch content from URL: {url}"
            result = trafilatura.extract(html_content, url=url, **self._get_extraction_params(output_format))
            if result is None:
                return f"Error: Could not extract readable content from URL: {url}"
            trafilatura.reset_caches()
        except Exception as exc:
            logger.warning("trafilatura_extract_text_failed", url=url, error=str(exc))
            return f"Error extracting text from {url}: {exc}"
        else:
            return result

    def _extract_metadata(self, url: str, *, as_json: bool) -> str:
        trafilatura = _require_trafilatura()
        try:
            logger.debug("trafilatura_extract_metadata", url=url)
            html_content = _fetch_url(url)
            if not html_content:
                return f"Error: Could not fetch content from URL: {url}"
            metadata_doc = trafilatura.extract_metadata(
                html_content,
                default_url=url,
                extensive=True,
                author_blacklist=None,
            )
            if metadata_doc is None:
                return f"Error: Could not extract metadata from URL: {url}"
            metadata_dict = metadata_doc.as_dict()
            trafilatura.reset_caches()
            if as_json:
                return json.dumps(metadata_dict, indent=2, default=str)
            return "\n".join(f"{key}: {value}" for key, value in metadata_dict.items())
        except Exception as exc:
            logger.warning("trafilatura_extract_metadata_failed", url=url, error=str(exc))
            return f"Error extracting metadata from {url}: {exc}"

    def _extract_batch(self, urls: list[str]) -> str:
        if len(urls) > _MAX_BATCH_URLS:
            return (
                f"Error: trafilatura_extract(action='batch') takes at most {_MAX_BATCH_URLS} URLs per call; "
                "split the list into several calls."
            )
        trafilatura = _require_trafilatura()
        with _time_limit():
            try:
                logger.debug("trafilatura_extract_batch", count=len(urls))
                results: dict[str, str] = {}
                failed_urls: list[str] = []
                for url in urls:
                    try:
                        content = self._fetch_and_extract(trafilatura, url)
                        if content is not None:
                            results[url] = content
                        else:
                            failed_urls.append(url)
                    except Exception as exc:
                        failed_urls.append(url)
                        results[url] = f"Error: {exc}"

                trafilatura.reset_caches()
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

    def html_to_text(self, html_content: str, clean: bool = True) -> str:
        """Convert raw HTML strings (not URLs) to clean text with Trafilatura (`html_content`, optional `clean`, default True)."""
        trafilatura = _require_trafilatura()
        try:
            logger.debug("trafilatura_html_to_text")
            result = trafilatura.html2txt(html_content, clean=clean)
            trafilatura.reset_caches()
        except Exception as exc:
            logger.warning("trafilatura_html_to_text_failed", error=str(exc))
            return f"Error converting HTML to text: {exc}"
        else:
            return result or "Error: Could not extract text from HTML content"

    def crawl_website(self, homepage_url: str, extract_content: bool = False) -> str:
        """Crawl a website from its `homepage_url` with Trafilatura and optionally extract page content (`extract_content`)."""
        trafilatura_spider = _load_spider()
        if trafilatura_spider is None:
            return "Error: Web crawling functionality not available. Trafilatura spider module could not be imported."
        trafilatura = _require_trafilatura()
        _install_server_fetch_guard()
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
                    for url in list(known_links)[: min(10, len(known_links))]:
                        try:
                            content = self._fetch_and_extract(trafilatura, url)
                            if content is not None:
                                extracted_content[url] = content
                        except Exception as exc:
                            extracted_content[url] = f"Error extracting content: {exc}"
                    crawl_results["extracted_content"] = extracted_content

                trafilatura.reset_caches()
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
        bool_config_field("include_comments", "Include Comments"),
        bool_config_field("include_tables", "Include Tables"),
        bool_config_field("include_images", "Include Images", default=False),
        bool_config_field("include_formatting", "Include Formatting", default=False),
        bool_config_field("include_links", "Include Links", default=False),
        bool_config_field("with_metadata", "Include Metadata", default=False),
        bool_config_field("favor_precision", "Favor Precision", default=False),
        bool_config_field("favor_recall", "Favor Recall", default=False),
        ConfigField(
            name="target_language",
            label="Target Language",
            type="text",
            required=False,
            default=None,
            placeholder="e.g., en, de, fr (ISO 639-1)",
        ),
        bool_config_field("deduplicate", "Deduplicate", default=False),
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
        bool_config_field("enable_extract_text", "Enable Extract Text"),
        bool_config_field("enable_extract_metadata_only", "Enable Extract Metadata Only"),
        bool_config_field("enable_html_to_text", "Enable HTML to Text"),
        bool_config_field("enable_extract_batch", "Enable Extract Batch"),
        bool_config_field("enable_crawl_website", "Enable Crawl Website"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["trafilatura"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#trafilatura",
    function_names=("crawl_website", "html_to_text", "trafilatura_extract"),
    action_scoped_grants=("trafilatura_extract",),
)
def trafilatura_tools() -> type[Toolkit]:
    """Return Trafilatura tools whose downloads follow the server-fetch policy within one time limit per call."""
    _install_server_fetch_guard()
    return _TrafilaturaTools
