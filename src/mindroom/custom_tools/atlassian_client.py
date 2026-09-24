"""Atlassian Cloud REST access through the api.atlassian.com OAuth gateway.

Every error raised here is safe to hand to a model: it carries a short code, a fixed
message, and non-sensitive details, never a request URL, header, or response body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.oauth.atlassian import normalize_cloud_id, normalize_site_url

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from mindroom.oauth.atlassian import AtlassianProduct

_GATEWAY_HOST = "api.atlassian.com"
_GATEWAY_ORIGIN = f"https://{_GATEWAY_HOST}"
_ACCESSIBLE_RESOURCES_URL = f"{_GATEWAY_ORIGIN}/oauth/token/accessible-resources"
# Attachment downloads redirect to signed media URLs, which are fetched without the OAuth bearer.
_MEDIA_HOSTS = frozenset({"api.media.atlassian.com"})
_REQUEST_TIMEOUT_SECONDS = 20.0
# Generous bounds for one API call as a whole: memory for its body, and time including a trickling body.
_MAX_JSON_RESPONSE_BYTES = 32 * 1024 * 1024
_REQUEST_DEADLINE_SECONDS = 60.0
_MAX_DOWNLOAD_REDIRECTS = 3
_DOWNLOAD_DEADLINE_SECONDS = 120.0
# Any media type, but no content coding, so the byte limit counts the bytes actually received.
_DOWNLOAD_HEADERS = {"Accept": "*/*", "Accept-Encoding": "identity"}
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_MAX_ERROR_MESSAGES = 5
_MAX_ERROR_MESSAGE_CHARS = 300
_STATUS_ERROR_CODES = {
    400: "invalid_request",
    403: "permission_denied",
    404: "not_found",
    409: "conflict",
    413: "request_too_large",
    429: "rate_limited",
}


class _SignedMediaUrlLogFilter(logging.Filter):
    """Drop the signature query from httpx request logs for Atlassian media downloads."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.copy_with(query=None) if isinstance(arg, httpx.URL) and arg.host in _MEDIA_HOSTS else arg
                for arg in record.args
            )
        return True


_HTTPX_LOGGER = logging.getLogger("httpx")
if not any(isinstance(log_filter, _SignedMediaUrlLogFilter) for log_filter in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_SignedMediaUrlLogFilter())


class AtlassianError(Exception):
    """A failure described only by a code, a fixed message, and non-sensitive details."""

    def __init__(self, *, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class AtlassianAccessRejectedError(AtlassianError):
    """The gateway rejected the OAuth access token, so the requester must reconnect."""


@dataclass(frozen=True, slots=True)
class AtlassianSite:
    """One Atlassian Cloud site the OAuth grant can reach."""

    cloud_id: str
    url: str | None
    name: str | None
    scopes: frozenset[str]

    def summary(self) -> dict[str, str | None]:
        """Return the non-secret identity used in site-selection errors."""
        return {"cloud_id": self.cloud_id, "name": self.name, "url": self.url}


@dataclass(frozen=True, slots=True)
class AtlassianSitePin:
    """Configured site restriction; the cloud ID is authoritative when both are set."""

    site_url: str | None = None
    cloud_id: str | None = None


@dataclass(frozen=True, slots=True)
class _AtlassianDownload:
    """Bytes and untrusted response metadata from one bounded binary download."""

    content: bytes
    content_type: str | None
    content_disposition: str | None


def _new_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Build one short-lived client; redirects are never followed implicitly."""
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False, transport=transport)


def _bearer_headers(access_token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}


def _transport_error(exc: httpx.HTTPError) -> AtlassianError:
    # httpx messages can include the request URL, so report only the failure type.
    return AtlassianError(code="request_failed", message=f"The Atlassian request failed ({type(exc).__name__}).")


def _scrubbed(text: str) -> str:
    """Drop control characters and URLs from one provider error message."""
    printable = "".join(char if char.isprintable() else " " for char in text)
    return _URL_PATTERN.sub("<url>", printable).strip()[:_MAX_ERROR_MESSAGE_CHARS]


def _error_messages(content: bytes | None) -> list[str]:
    """Return Jira or Confluence error messages from a failed response body, without the raw body."""
    try:
        payload = json.loads(content) if content else None
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    candidates: list[object] = []
    error_messages = payload.get("errorMessages")
    if isinstance(error_messages, list):
        candidates.extend(error_messages)
    errors = payload.get("errors")
    if isinstance(errors, dict):
        candidates.extend(f"{field}: {message}" for field, message in errors.items() if isinstance(message, str))
    elif isinstance(errors, list):
        candidates.extend(item.get("detail") or item.get("title") for item in errors if isinstance(item, dict))
    candidates.append(payload.get("message"))
    messages = [_scrubbed(item) for item in candidates if isinstance(item, str) and item.strip()]
    return [message for message in messages if message][:_MAX_ERROR_MESSAGES]


def _status_error(status_code: int, content: bytes | None) -> AtlassianError:
    if status_code == 401:
        return AtlassianAccessRejectedError(
            code="access_rejected",
            message="Atlassian rejected the connected account's authorization.",
            status_code=status_code,
        )
    return AtlassianError(
        code=_STATUS_ERROR_CODES.get(status_code, "atlassian_error"),
        message="Atlassian rejected the request.",
        status_code=status_code,
        messages=_error_messages(content),
    )


async def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes | None:
    """Read a response body, or return None as soon as it is known to pass max_bytes."""
    declared = response.headers.get("content-length", "").strip()
    if declared.isdecimal() and int(declared) > max_bytes:
        return None
    try:
        return await collect_bounded_bytes(response.aiter_bytes(), max_bytes=max_bytes)
    except ByteLimitExceededError:
        return None


async def _send_json(
    method: str,
    url: str,
    access_token: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json_body: object = None,
) -> object:
    """Send one bearer request and decode its JSON, bounding both the body size and the whole exchange."""
    try:
        async with (
            asyncio.timeout(_REQUEST_DEADLINE_SECONDS),
            _new_http_client() as client,
            client.stream(
                method,
                url,
                headers=_bearer_headers(access_token),
                params=dict(params) if params else None,
                json=json_body,
            ) as response,
        ):
            status_code = response.status_code
            content = await _bounded_body(response, _MAX_JSON_RESPONSE_BYTES)
    except TimeoutError:
        raise AtlassianError(
            code="request_timeout",
            message=f"The Atlassian request did not finish within {_REQUEST_DEADLINE_SECONDS:.0f} seconds.",
        ) from None
    except httpx.HTTPError as exc:
        raise _transport_error(exc) from None
    if not httpx.codes.is_success(status_code):
        raise _status_error(status_code, content)
    if content is None:
        raise AtlassianError(
            code="response_too_large",
            message=f"Atlassian returned more than {_MAX_JSON_RESPONSE_BYTES} bytes. "
            "Narrow the request, for example with fewer fields or a smaller limit.",
            max_bytes=_MAX_JSON_RESPONSE_BYTES,
        )
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        raise AtlassianError(
            code="invalid_response",
            message="Atlassian returned a response that is not JSON.",
        ) from None


def _site_from_resource(resource: object) -> AtlassianSite | None:
    """Parse one accessible resource, skipping entries whose cloud ID could alter a gateway path."""
    if not isinstance(resource, dict):
        return None
    resource = cast("dict[str, object]", resource)
    raw_cloud_id = resource.get("id")
    if not isinstance(raw_cloud_id, str):
        return None
    try:
        cloud_id = normalize_cloud_id(raw_cloud_id)
    except ValueError:
        return None
    raw_url = resource.get("url")
    try:
        url = normalize_site_url(raw_url) if isinstance(raw_url, str) else None
    except ValueError:
        url = None
    name = resource.get("name")
    scopes = resource.get("scopes")
    return AtlassianSite(
        cloud_id=cloud_id,
        url=url,
        name=name if isinstance(name, str) else None,
        scopes=frozenset(scope for scope in scopes if isinstance(scope, str))
        if isinstance(scopes, list)
        else frozenset(),
    )


async def accessible_sites(access_token: str) -> list[AtlassianSite]:
    """Return the Atlassian Cloud sites the access token was granted for."""
    resources = await _send_json("GET", _ACCESSIBLE_RESOURCES_URL, access_token)
    sites_by_cloud_id: dict[str, AtlassianSite] = {}
    for resource in resources if isinstance(resources, list) else []:
        site = _site_from_resource(resource)
        if site is None:
            continue
        # One site can be listed once per product; merge its entries so it counts once.
        known = sites_by_cloud_id.get(site.cloud_id)
        sites_by_cloud_id[site.cloud_id] = site if known is None else replace(known, scopes=known.scopes | site.scopes)
    return list(sites_by_cloud_id.values())


def select_site(
    sites: list[AtlassianSite],
    *,
    product: AtlassianProduct,
    product_scopes: Collection[str],
    pin: AtlassianSitePin,
) -> AtlassianSite:
    """Pick the pinned site for one product, never falling back to a different site."""
    candidates = [site for site in sites if site.scopes.intersection(product_scopes)]
    if pin.cloud_id is not None:
        matches = [site for site in candidates if site.cloud_id == pin.cloud_id]
    elif pin.site_url is not None:
        matches = [site for site in candidates if site.url == pin.site_url]
    else:
        matches = candidates
    if len(matches) == 1:
        return matches[0]
    available = [site.summary() for site in candidates]
    if pin.cloud_id is None and pin.site_url is None:
        if candidates:
            raise AtlassianError(
                code="site_selection_required",
                message=f"The connected account can reach several {product} sites; "
                "configure site_url or cloud_id for this tool.",
                product=product,
                available_sites=available,
            )
        raise AtlassianError(
            code="site_not_found",
            message=f"The connected account cannot reach any {product} site. "
            "Reconnect with an account that has access.",
            product=product,
        )
    raise AtlassianError(
        code="site_not_found",
        message=f"The configured {product} site is not available to the connected account. "
        "Reconnect with an account that can access it, or correct the site setting.",
        product=product,
        configured_site_url=pin.site_url,
        configured_cloud_id=pin.cloud_id,
        available_sites=available,
    )


def _gateway_prefix(product: AtlassianProduct, site: AtlassianSite) -> str:
    return f"/ex/{product}/{site.cloud_id}/"


async def request_json(
    access_token: str,
    site: AtlassianSite,
    product: AtlassianProduct,
    method: str,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json_body: object = None,
) -> object:
    """Call one product REST path on the pinned site and return its decoded JSON."""
    url = f"{_GATEWAY_ORIGIN}{_gateway_prefix(product, site)}{path.lstrip('/')}"
    return await _send_json(method, url, access_token, params=params, json_body=json_body)


def _fully_unquoted(segment: str) -> str | None:
    """Decode a path segment until stable, so nested encoding cannot hide separators; None if it never settles."""
    for _ in range(4):
        decoded = unquote(segment)
        if decoded == segment:
            return segment
        segment = decoded
    return None


def _is_gateway_url(url: str, gateway_prefix: str) -> bool:
    """Return whether url is this product and site's gateway path, the only destination for the bearer."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "https"
        or parts.hostname != _GATEWAY_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not parts.path.startswith(gateway_prefix)
    ):
        return False
    for segment in parts.path.split("/"):
        decoded = _fully_unquoted(segment)
        if decoded is None or decoded in {".", ".."} or any(char in decoded for char in "/\\;"):
            return False
    return True


def _redirect_rejected(message: str, host: str | None = None) -> AtlassianError:
    atlassian_host = host if host and host.endswith((".atlassian.com", ".atlassian.net")) else None
    return AtlassianError(code="redirect_rejected", message=message, redirect_host=atlassian_host)


def _redirect_target(current_url: str, location: str | None, gateway_prefix: str) -> str:
    """Resolve one redirect, allowing only the same gateway path or the Atlassian media service."""
    if not location:
        msg = "Atlassian returned a download redirect without a location."
        raise _redirect_rejected(msg)
    try:
        target = urljoin(current_url, location)
        parts = urlsplit(target)
        port = parts.port
    except ValueError:
        msg = "Atlassian returned a malformed download redirect."
        raise _redirect_rejected(msg) from None
    host = parts.hostname
    if _is_gateway_url(target, gateway_prefix) or (
        parts.scheme == "https"
        and host in _MEDIA_HOSTS
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    ):
        return target
    msg = "Atlassian redirected the download to an unsupported location."
    raise _redirect_rejected(msg, host)


def _download_status_error(status_code: int, *, from_gateway: bool) -> AtlassianError:
    """Describe a failed hop by status only; error bodies can echo signed URLs."""
    if not from_gateway:
        return AtlassianError(
            code="download_failed",
            message="The Atlassian media service rejected the download.",
            status_code=status_code,
        )
    if status_code in {401, 403, 404}:
        # A usable grant always carries the download scope, so this is a page, attachment, or site permission.
        return AtlassianError(
            code="attachment_unavailable",
            message="Atlassian did not allow this attachment download. It may not exist, or the connected account "
            "may lack permission to view this page, its attachments, or this site.",
            status_code=status_code,
        )
    return AtlassianError(
        code="download_failed",
        message="Atlassian rejected the attachment download.",
        status_code=status_code,
    )


async def download(
    access_token: str,
    site: AtlassianSite,
    product: AtlassianProduct,
    path: str,
    *,
    max_bytes: int,
) -> _AtlassianDownload:
    """Download one binary gateway path, following at most a few Atlassian-controlled redirects.

    The bearer goes only to this product and site's gateway path, never to a media host.
    No hop receives another hop's cookies, and content-coded bodies are rejected before decoding.
    """
    gateway_prefix = _gateway_prefix(product, site)
    url = f"{_GATEWAY_ORIGIN}{gateway_prefix}{path.lstrip('/')}"
    try:
        async with asyncio.timeout(_DOWNLOAD_DEADLINE_SECONDS), _new_http_client() as client:
            for _hop in range(_MAX_DOWNLOAD_REDIRECTS + 1):
                from_gateway = _is_gateway_url(url, gateway_prefix)
                # A cookie set by one hop, even for a parent domain, must not reach the next.
                client.cookies.clear()
                headers = dict(_DOWNLOAD_HEADERS)
                if from_gateway:
                    headers["Authorization"] = f"Bearer {access_token}"
                # Each hop is checked before it is followed, whatever the client default is.
                async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                    if response.is_redirect:
                        url = _redirect_target(url, response.headers.get("location"), gateway_prefix)
                        continue
                    if not response.is_success:
                        raise _download_status_error(response.status_code, from_gateway=from_gateway)
                    return await _bounded_download(response, max_bytes)
    except TimeoutError:
        raise AtlassianError(
            code="download_timeout",
            message=f"The download did not finish within {_DOWNLOAD_DEADLINE_SECONDS:.0f} seconds.",
        ) from None
    except httpx.HTTPError as exc:
        raise AtlassianError(code="download_failed", message=f"The download failed ({type(exc).__name__}).") from None
    raise AtlassianError(code="redirect_rejected", message="Atlassian redirected the download too many times.")


def _too_large(max_bytes: int) -> AtlassianError:
    return AtlassianError(
        code="attachment_too_large",
        message=f"The attachment exceeds the {max_bytes}-byte download limit.",
        max_bytes=max_bytes,
    )


async def _bounded_download(response: httpx.Response, max_bytes: int) -> _AtlassianDownload:
    """Read the raw body, rejecting content coding and stopping as soon as it passes max_bytes."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity"}:
        raise AtlassianError(
            code="download_failed",
            message="Atlassian returned a content-encoded download, which is not supported.",
        )
    declared = response.headers.get("content-length", "").strip()
    if declared.isdecimal() and int(declared) > max_bytes:
        raise _too_large(max_bytes)
    try:
        content = await collect_bounded_bytes(response.aiter_raw(), max_bytes=max_bytes)
    except ByteLimitExceededError:
        raise _too_large(max_bytes) from None
    return _AtlassianDownload(
        content=content,
        content_type=response.headers.get("content-type"),
        content_disposition=response.headers.get("content-disposition"),
    )
