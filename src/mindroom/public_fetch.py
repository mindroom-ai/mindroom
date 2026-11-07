"""Fetch one public HTTP(S) URL under MindRoom's server-side fetch rules, for core tools and plugins."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx
import idna
from httpx._utils import get_environment_proxies

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.server_fetch_url import ServerFetchAsyncHTTPTransport, ServerFetchUrlError, validate_server_fetch_url

if TYPE_CHECKING:
    from collections.abc import Coroutine

type _PublicFetchErrorCode = Literal[
    "url_not_allowed",
    "timeout",
    "too_many_redirects",
    "too_large",
    "content_encoding",
    "http_status",
    "request_failed",
]

# One deadline covers the whole fetch: every redirect hop, its connection, and the body.
_DEADLINE_SECONDS = 30.0
# Each address a hostname resolves to gets this long to connect, so a silent one leaves time to try the next.
_CONNECT_TIMEOUT_SECONDS = 10.0
_MAX_REDIRECTS = 10


@dataclass(frozen=True)
class PublicFetchResult:
    """The body of a fetched page or file, with the URL its redirects ended on and its declared Content-Type."""

    url: str
    content_type: str | None
    content: bytes


class PublicFetchError(ValueError):
    """A public fetch failed; `code` names why, and the message explains it without repeating the URL."""

    def __init__(self, message: str, *, code: _PublicFetchErrorCode, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class _ProxyTransport(httpx.AsyncHTTPTransport):
    """Refuse each destination before the proxy, which resolves the hostname itself, receives it."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Check every hop, redirects included, against the resolved addresses."""
        await asyncio.to_thread(validate_server_fetch_url, str(request.url))
        return await super().handle_async_request(request)


def _proxy_mounts() -> dict[str, httpx.AsyncBaseTransport | None]:
    # HTTPX ignores the proxy environment once a transport is given, so its own routing map is reused here:
    # scheme selection and NO_PROXY keep their meaning, and a bypassed host keeps the guarded direct dial.
    return {
        pattern: None if proxy is None else _ProxyTransport(proxy=proxy)
        for pattern, proxy in get_environment_proxies().items()
    }


async def _read_final_response(response: httpx.Response, max_bytes: int) -> PublicFetchResult:
    if not response.is_success:
        msg = f"The server answered with HTTP status {response.status_code}."
        raise PublicFetchError(msg, code="http_status", status_code=response.status_code)
    codings = response.headers.get_list("content-encoding", split_commas=True)
    if any(coding.lower() not in {"", "identity"} for coding in codings):
        msg = "The response must use identity content encoding."
        raise PublicFetchError(msg, code="content_encoding")
    try:
        content = await collect_bounded_bytes(response.aiter_raw(), max_bytes=max_bytes)
    except ByteLimitExceededError:
        msg = f"The response exceeds the {max_bytes}-byte limit."
        raise PublicFetchError(msg, code="too_large") from None
    return PublicFetchResult(url=str(response.url), content_type=response.headers.get("content-type"), content=content)


async def _fetch(url: str, max_bytes: int, use_env_proxy: bool, user_agent: str | None) -> PublicFetchResult:
    headers = {"Accept-Encoding": "identity"}
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    async with httpx.AsyncClient(
        transport=ServerFetchAsyncHTTPTransport(),
        mounts=_proxy_mounts() if use_env_proxy else None,
        headers=headers,
        timeout=httpx.Timeout(None, connect=_CONNECT_TIMEOUT_SECONDS),
    ) as client:
        request = client.build_request("GET", url.strip())
        for _hop in range(_MAX_REDIRECTS + 1):
            if request.url.userinfo:
                raise ServerFetchUrlError(reason="credentials_in_url")
            response = await client.send(request, stream=True)
            try:
                if response.next_request is None:
                    return await _read_final_response(response, max_bytes)
                # A redirect is left unread; its body could be endless.
                request = response.next_request
            finally:
                await response.aclose()
    msg = f"The fetch stopped after {_MAX_REDIRECTS} redirects."
    raise PublicFetchError(msg, code="too_many_redirects")


async def fetch_public_url(
    url: str,
    *,
    max_bytes: int,
    use_env_proxy: bool = False,
    user_agent: str | None = None,
) -> PublicFetchResult:
    """Fetch a public HTTP(S) URL with GET, following up to ten redirects, within one 30-second deadline.

    Every hop must be a public address: loopback, private, link-local, and metadata targets are refused, including
    an address the hostname resolves to when the connection is made.
    The fetch sends no credentials, refuses a URL that carries them, and returns only an identity-encoded body of at
    most `max_bytes` bytes from a 2xx response.
    With `use_env_proxy`, a hop goes through the proxy that `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` names, unless
    `NO_PROXY` excludes it; that proxy resolves the hostname again itself, so it must refuse private addresses too.
    `user_agent` replaces HTTPX's `User-Agent` header on every hop.
    Every failure raises `PublicFetchError`.
    """
    try:
        async with asyncio.timeout(_DEADLINE_SECONDS):
            return await _fetch(url, max_bytes, use_env_proxy, user_agent)
    except TimeoutError:
        msg = f"The fetch did not finish within {_DEADLINE_SECONDS:g} seconds."
        raise PublicFetchError(msg, code="timeout") from None
    except ServerFetchUrlError as error:
        if error.reason == "dns_resolution_failed":
            # A failed lookup is a network failure the caller may retry, not a refused URL.
            msg = "The request failed (dns_resolution_failed)."
            raise PublicFetchError(msg, code="request_failed") from None
        msg = f"The URL is not allowed for a public fetch ({error.reason})."
        raise PublicFetchError(msg, code="url_not_allowed") from None
    except (httpx.InvalidURL, idna.IDNAError):
        msg = "The URL is not allowed for a public fetch (invalid_url)."
        raise PublicFetchError(msg, code="url_not_allowed") from None
    except httpx.HTTPError as error:
        msg = f"The request failed ({error.__class__.__name__})."
        raise PublicFetchError(msg, code="request_failed") from None


def run_fetch_sync[T](fetch: Coroutine[object, object, T]) -> T:
    """Run an async fetch from synchronous code, such as a synchronous tool, never from an event loop's thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(fetch)
    finally:
        # Unlike `asyncio.run`, closing the loop does not wait for its executor, so a hostname lookup still running
        # past the deadline finishes on its own instead of holding the caller.
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def fetch_public_url_sync(
    url: str,
    *,
    max_bytes: int,
    use_env_proxy: bool = False,
    user_agent: str | None = None,
) -> PublicFetchResult:
    """Run `fetch_public_url` from synchronous code, such as a synchronous tool, never from an event loop's thread."""
    return run_fetch_sync(
        fetch_public_url(url, max_bytes=max_bytes, use_env_proxy=use_env_proxy, user_agent=user_agent),
    )
