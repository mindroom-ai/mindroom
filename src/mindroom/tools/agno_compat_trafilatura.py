"""Route Trafilatura toolkit downloads through MindRoom's server-fetch policy."""

from __future__ import annotations

import agno.tools.trafilatura as agno_trafilatura
import httpx
from agno.utils.log import log_warning
from trafilatura import spider as trafilatura_spider
from trafilatura.downloads import USER_AGENT, Response
from trafilatura.settings import DEFAULT_CONFIG

from mindroom.server_fetch_url import (
    ServerFetchHTTPTransport,
    validate_server_fetch_redirect_url,
    validate_server_fetch_url,
)

_DOWNLOAD_TIMEOUT_SECONDS = DEFAULT_CONFIG.getint("DEFAULT", "DOWNLOAD_TIMEOUT")
_MAX_REDIRECTS = DEFAULT_CONFIG.getint("DEFAULT", "MAX_REDIRECTS")
_MAX_FILE_SIZE = DEFAULT_CONFIG.getint("DEFAULT", "MAX_FILE_SIZE")


def _read_response(response: httpx.Response, url: str, *, decode: bool) -> Response | None:
    """Buffer one uncompressed response body within Trafilatura's download size limit."""
    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
        log_warning("Trafilatura download used an unrequested content encoding")
        return None
    data = bytearray()
    for chunk in response.iter_raw():
        if len(data) + len(chunk) > _MAX_FILE_SIZE:
            log_warning("Trafilatura download exceeded the maximum file size")
            return None
        data.extend(chunk)
    fetched = Response(bytes(data), response.status_code, url)
    fetched.decode_data(decode)
    return fetched


def _fetch_response(url: str, *, decode: bool = False) -> Response | None:
    """Download one page after validating the URL, every redirect hop, and each dialed address.

    Unsafe targets raise ``ServerFetchUrlError``.
    Ordinary download failures return ``None`` like Trafilatura's own downloader.
    Bodies are requested uncompressed so the size limit bounds memory before any decoding.
    """
    request_url = validate_server_fetch_url(url)
    try:
        with httpx.Client(
            transport=ServerFetchHTTPTransport(),
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        ) as client:
            for _redirect_count in range(_MAX_REDIRECTS + 1):
                with client.stream("GET", request_url) as response:
                    if not response.is_redirect:
                        return _read_response(response, request_url, decode=decode)
                    location = response.headers.get("location")
                request_url = validate_server_fetch_redirect_url(request_url, location)
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        log_warning(f"Trafilatura download failed: {error.__class__.__name__}")
        return None
    log_warning("Trafilatura download stopped after too many redirects")
    return None


def _fetch_url(url: str) -> str | None:
    """Return decoded HTML from a successful guarded download."""
    response = _fetch_response(url, decode=True)
    if response is None or response.status != 200:
        return None
    return response.html


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
    # ::test_trafilatura_rejects_dns_rebind_at_connect_time,
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
