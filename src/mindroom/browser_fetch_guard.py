"""Shared Playwright request guard for server-side browser tools."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.async_api import Route

_BROWSER_INTERNAL_SCHEMES = frozenset({"about", "blob", "data"})
# A page can open many connections to hostnames whose nameservers never answer. Browser lookups therefore
# run on their own threads, so they cannot exhaust the default executor the rest of the runtime shares, and each
# destination relay holds at most a few of them at once.
_BROWSER_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="mindroom-browser-dns")
# Model-requested URLs (open, navigate, desktop open) resolve on separate threads that page traffic cannot occupy.
_BROWSER_TOOL_URL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mindroom-browser-url")


async def run_browser_dns_lookup[**P, T](function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run one blocking page-driven destination lookup on the threads reserved for browser DNS."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_BROWSER_DNS_EXECUTOR, functools.partial(function, *args, **kwargs))


async def run_browser_tool_url_check[**P, T](function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run one blocking validation of a model-requested URL on threads pages cannot exhaust."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_BROWSER_TOOL_URL_EXECUTOR, functools.partial(function, *args, **kwargs))


def validate_browser_fetch_url(
    url: str,
    *,
    allow_private_networks: bool = False,
    allow_loopback: bool = False,
    resolve_hostnames: bool = True,
) -> str:
    """Validate a browser request URL while allowing non-network browser internals."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as exc:
        raise ServerFetchUrlError(reason="invalid_host") from exc
    if scheme in _BROWSER_INTERNAL_SCHEMES:
        return url
    return validate_server_fetch_url(
        url,
        allow_private_networks=allow_private_networks,
        allow_loopback=allow_loopback,
        resolve_hostnames=resolve_hostnames,
    )


async def continue_or_abort_browser_fetch(
    route: Route,
    *,
    allow_private_networks: bool = False,
    allow_loopback: bool = False,
) -> None:
    """Continue public browser fetches and abort unsafe server-side destinations.

    The destination relay resolves and validates every address Chromium dials, so this first filter checks schemes,
    address literals, and local or metadata names without a DNS lookup that a page could stall.
    """
    try:
        validate_browser_fetch_url(
            route.request.url,
            allow_private_networks=allow_private_networks,
            allow_loopback=allow_loopback,
            resolve_hostnames=False,
        )
    except ServerFetchUrlError:
        await route.abort("blockedbyclient")
        return
    await route.continue_()
