"""Route Trafilatura toolkit downloads through MindRoom's server-fetch policy."""

from __future__ import annotations

import agno.tools.trafilatura as agno_trafilatura
from agno.utils.log import log_warning
from trafilatura import spider as trafilatura_spider
from trafilatura.downloads import USER_AGENT, Response
from trafilatura.settings import DEFAULT_CONFIG

from mindroom.public_fetch import PublicFetchError, fetch_public_url_sync

_MAX_FILE_SIZE = DEFAULT_CONFIG.getint("DEFAULT", "MAX_FILE_SIZE")


def _fetch_response(url: str, *, decode: bool = False) -> Response | None:
    """Download one page through the shared public fetch, within Trafilatura's download size limit.

    A refused URL raises ``PublicFetchError``; any other failure, including a non-2xx response, returns ``None``.
    """
    try:
        page = fetch_public_url_sync(url, max_bytes=_MAX_FILE_SIZE, user_agent=USER_AGENT)
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
    # ::test_trafilatura_crawl_extracts_public_pages,
    # ::test_trafilatura_crawl_rejects_discovered_redirect_to_loopback, and
    # ::test_trafilatura_factory_installs_guarded_fetchers.
    trafilatura_spider.fetch_url = _fetch_url  # ty: ignore[invalid-assignment]
    trafilatura_spider.fetch_response = _fetch_response  # ty: ignore[invalid-assignment]
