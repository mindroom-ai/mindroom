"""Shared Playwright request guard for server-side browser tools."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url

if TYPE_CHECKING:
    from playwright.async_api import Route

_BROWSER_INTERNAL_SCHEMES = frozenset({"about", "blob", "data"})


def _validate_browser_fetch_url(url: str) -> str:
    """Validate a browser request URL while allowing non-network browser internals."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as exc:
        raise ServerFetchUrlError(reason="invalid_host") from exc
    if scheme in _BROWSER_INTERNAL_SCHEMES:
        return url
    return validate_server_fetch_url(url)


async def continue_or_abort_browser_fetch(route: Route) -> None:
    """Continue public browser fetches and abort unsafe server-side destinations."""
    try:
        await asyncio.to_thread(_validate_browser_fetch_url, route.request.url)
    except ServerFetchUrlError:
        await route.abort("blockedbyclient")
        return
    await route.continue_()
