"""Route Trafilatura toolkit downloads through MindRoom's server-fetch policy, within one time limit per call."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import agno.tools.trafilatura as agno_trafilatura
from agno.tools.trafilatura import TrafilaturaTools
from agno.utils.log import log_warning
from trafilatura import spider as trafilatura_spider
from trafilatura.downloads import USER_AGENT, Response
from trafilatura.settings import DEFAULT_CONFIG

from mindroom.public_fetch import PublicFetchError, PublicFetchResult, fetch_public_url, run_fetch_sync

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_FILE_SIZE = DEFAULT_CONFIG.getint("DEFAULT", "MAX_FILE_SIZE")
# A default crawl of 10 pages pauses 5 seconds after each crawled page and downloads about 21 pages, so it fits.
_CALL_SECONDS = 120.0
_MAX_BATCH_URLS = 10


@dataclass
class _Call:
    """One batch extract or crawl: when it stops downloading, and whether its crawl has fetched its start page."""

    deadline: float
    crawl_started: bool = False


_call: ContextVar[_Call | None] = ContextVar("trafilatura_call", default=None)


def _seconds_left() -> float | None:
    """Return the time left in the current batch extract or crawl, or None outside one."""
    call = _call.get()
    return None if call is None else call.deadline - time.monotonic()


async def _fetch(url: str, seconds: float | None) -> PublicFetchResult:
    async with asyncio.timeout(seconds):
        return await fetch_public_url(url, max_bytes=_MAX_FILE_SIZE, user_agent=USER_AGENT)


def _fetch_response(url: str, *, decode: bool = False) -> Response | None:
    """Download one page through the shared public fetch, within Trafilatura's size limit and the call's time limit.

    A refused URL raises ``PublicFetchError``; any other failure, including a non-2xx response or a download the
    time limit ends, returns ``None``, and so does every download once the time limit has passed.
    """
    seconds = _seconds_left()
    if seconds is not None and seconds <= 0:
        return None
    try:
        page = run_fetch_sync(_fetch(url, seconds))
    except TimeoutError:
        log_warning("Trafilatura download stopped at the tool call's time limit")
        return None
    except PublicFetchError as error:
        if error.code == "url_not_allowed":
            raise
        log_warning(f"Trafilatura download failed: {error}")
        return None
    # The shared fetch returns only a 2xx body.
    fetched = Response(page.content, 200, page.url)
    fetched.decode_data(decode)
    return fetched


def _fetch_url(url: str) -> str | None:
    """Return decoded HTML from a successful guarded download."""
    response = _fetch_response(url, decode=True)
    return None if response is None else response.html


def _crawl_fetch_response(url: str, *, decode: bool = False) -> Response | None:
    """Download one crawled page; a refused start page fails the crawl, and a refused link after it is skipped."""
    call = _call.get()
    start_page = call is None or not call.crawl_started
    if call is not None:
        call.crawl_started = True
    try:
        return _fetch_response(url, decode=decode)
    except PublicFetchError as error:
        if start_page:
            raise
        log_warning(f"Trafilatura crawl skipped a link: {error}")
        return None


def _pause(seconds: float) -> None:
    """Wait between crawled pages, never past the call's time limit."""
    left = _seconds_left()
    time.sleep(seconds if left is None else max(min(seconds, left), 0))


@contextmanager
def _time_limit() -> Iterator[None]:
    token = _call.set(_Call(deadline=time.monotonic() + _CALL_SECONDS))
    try:
        yield
    finally:
        _call.reset(token)


# AGNO_COMPAT: TrafilaturaTools batch extraction and crawling have no limit on a call's total time.
# Reason: Agno 3.0.9 extract_batch downloads any number of URLs one after another, and crawl_website makes
# about 21 downloads while Trafilatura's spider, through its module-level sleep, pauses after each crawled page
# for as long as the site's robots.txt crawl delay asks; each download is bounded, but one call could hold its
# thread for many minutes.
# Upstream issue: Tracking gap; no matching issue identified on November 7, 2026.
# Upstream PR: None identified for a total time limit or a batch length limit.
# Remove when: TrafilaturaTools bounds a call's total download and pause time and its batch length, and returns
# what it gathered when the time runs out.
# Coverage: tests/test_trafilatura_tool.py::test_trafilatura_refuses_a_batch_over_its_url_limit,
# ::test_trafilatura_batch_stops_downloading_at_its_time_limit, and
# ::test_trafilatura_crawl_stops_at_its_time_limit_despite_a_long_crawl_delay.
class TimeLimitedTrafilaturaTools(TrafilaturaTools):
    """Trafilatura toolkit whose batch extracts and crawls stop downloading at one time limit per call."""

    @override
    def extract_batch(self, urls: list[str]) -> str:
        if len(urls) > _MAX_BATCH_URLS:
            return f"Error: extract_batch takes at most {_MAX_BATCH_URLS} URLs per call; split the list into several calls."
        with _time_limit():
            return super().extract_batch(urls)

    @override
    def crawl_website(self, homepage_url: str, extract_content: bool = False) -> str:
        with _time_limit():
            return super().crawl_website(homepage_url, extract_content)


def install_server_fetch_guard() -> None:
    """Replace every downloader TrafilaturaTools reaches with the guarded fetchers."""
    # AGNO_COMPAT: TrafilaturaTools extraction downloads through a fixed module-level fetcher.
    # Reason: Agno 3.0.9 extract_text, extract_metadata_only, extract_batch, and the
    # crawl_website content loop call the module-level trafilatura.fetch_url binding,
    # which follows redirects and dials any address, so model-chosen URLs reached
    # loopback, private, and metadata targets from the MindRoom process.
    # Upstream issue: Tracking gap; no matching issue identified on September 24, 2026.
    # Upstream PR: None identified for an injectable TrafilaturaTools fetcher.
    # Remove when: TrafilaturaTools accepts a caller-supplied fetcher for every extraction
    # path; retain validation of each URL, redirect hop, and dialed address.
    # Coverage: tests/test_trafilatura_tool.py::test_trafilatura_rejects_unsafe_targets_without_connecting,
    # ::test_trafilatura_revalidates_redirects_before_following,
    # ::test_trafilatura_refuses_a_hostname_that_resolves_to_loopback,
    # ::test_trafilatura_extracts_public_page,
    # ::test_trafilatura_rejects_oversized_download,
    # ::test_trafilatura_rejects_compressed_download,
    # ::test_trafilatura_factory_installs_guarded_fetchers, and
    # tests/test_tools_metadata.py::test_local_url_fetch_tools_do_not_contact_loopback_targets.
    agno_trafilatura.fetch_url = _fetch_url  # ty: ignore[invalid-assignment]
    # AGNO_COMPAT: TrafilaturaTools crawling delegates to Trafilatura's spider without a fetch hook.
    # Reason: Agno 3.0.9 crawl_website calls trafilatura.spider.focused_crawler, whose
    # module-level fetch_url and fetch_response bindings download robots.txt, redirects,
    # meta-refresh targets, and discovered links without a destination policy.
    # Upstream issue: Tracking gap; no matching issue identified on September 24, 2026.
    # Upstream PR: None identified for a crawler fetch hook in Agno or Trafilatura.
    # Remove when: crawl_website can pass a caller-supplied fetcher to every crawler
    # download; retain validation of each URL, redirect hop, and dialed address.
    # Coverage: tests/test_trafilatura_tool.py::test_trafilatura_rejects_unsafe_targets_without_connecting,
    # ::test_trafilatura_revalidates_redirects_before_following,
    # ::test_trafilatura_crawl_extracts_public_pages,
    # ::test_trafilatura_crawl_skips_a_discovered_link_it_refuses,
    # ::test_trafilatura_crawl_stops_at_its_time_limit_despite_a_long_crawl_delay, and
    # ::test_trafilatura_factory_installs_guarded_fetchers.
    trafilatura_spider.fetch_url = _fetch_url  # ty: ignore[invalid-assignment]
    trafilatura_spider.fetch_response = _crawl_fetch_response  # ty: ignore[invalid-assignment]
    # The pause after each crawled page ends at the call's time limit, as TimeLimitedTrafilaturaTools describes.
    trafilatura_spider.sleep = _pause  # ty: ignore[invalid-assignment]
