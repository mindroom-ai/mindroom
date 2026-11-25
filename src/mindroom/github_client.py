"""Shared owner-scoped GitHub credentials and fixed-host `https://api.github.com` HTTP helpers."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final, Literal, TypeIs

import httpx

from mindroom.credentials.manager import (
    CredentialsUnreadableError,
    credential_layers,
    get_runtime_credentials_manager,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    load_oauth_credentials_status,
    oauth_credentials_usable,
    refresh_oauth_credentials,
    resolve_oauth_credential_context,
)
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.providers import OAuthRefreshRejectedError
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialLayers, CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext, OAuthCredentialsStatus

logger = get_logger(__name__)

GITHUB_API_BASE: Final = "https://api.github.com"
GITHUB_API_VERSION: Final = "2022-11-28"
GITHUB_DEFAULT_ACCEPT: Final = "application/vnd.github+json"
GITHUB_USER_TOKEN_SERVICE: Final = "github_user_token"  # noqa: S105
GITHUB_TOKEN_SHAPE = re.compile(r"[!-~]{1,512}")
GITHUB_LOGIN_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
GitHubAuthMode = Literal["token", "app", "anonymous"]

_GITHUB_ORIGIN: Final = ("https", "api.github.com", None, b"")
_MAX_REDIRECTS: Final = 3
_MAX_ERROR_BODY_BYTES: Final = 64 * 1024
_LINK_ENTRY_RE = re.compile(r"<([^>\s]+)>((?:\s*;\s*[^,;]+)*)")
_LINK_REL_RE = re.compile(r';\s*rel\s*=\s*(?:"([^"]*)"|([^\s;]+))', re.IGNORECASE)


class GitHubClientError(RuntimeError):
    """Raised when a GitHub API response exceeds bounds or cannot be decoded as expected JSON."""


@dataclass(frozen=True, slots=True)
class GitHubGetResult:
    """Result of one fixed-host `https://api.github.com` GET request."""

    status_code: int
    etag: str | None
    data: Any
    headers: dict[str, str]
    rate_limited: bool
    retry_after_seconds: float | None
    next_page: int | None = None


def matches_github_shape(value: object, shape: re.Pattern[str]) -> TypeIs[str]:
    """Return whether ``value`` is a string matching ``shape`` completely."""
    return isinstance(value, str) and shape.fullmatch(value) is not None


def build_user_github_oauth_context(
    user_id: str,
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> OAuthCredentialContext | None:
    """Return ``user_id``'s own GitHub OAuth credential context, or None when no requester scope resolves."""
    identity = build_requester_execution_identity(agent_name="oauth", runtime_paths=runtime_paths, requester_id=user_id)
    context = resolve_oauth_credential_context(
        github_oauth_provider(),
        runtime_paths,
        get_runtime_credentials_manager(runtime_paths),
        None,
        execution_identity=identity,
        config=config,
    )
    return None if context.worker_target is None else context


def github_user_token_store(
    context: OAuthCredentialContext,
    *,
    get_layers: Callable[..., CredentialLayers] = credential_layers,
) -> CredentialsManager:
    """Return the user's primary-only store for their saved GitHub personal access token."""
    return get_layers(
        GITHUB_USER_TOKEN_SERVICE,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
        primary_built_tool=True,
    ).writable_store()


def read_saved_github_token(
    context: OAuthCredentialContext,
    *,
    get_layers: Callable[..., CredentialLayers] = credential_layers,
) -> dict[str, str] | None:
    """Return the user's saved ``{token, login}`` or None; raises ``CredentialsUnreadableError`` if unreadable."""
    try:
        document = github_user_token_store(context, get_layers=get_layers).read_saved_credentials(
            GITHUB_USER_TOKEN_SERVICE,
        )
    except ValueError as exc:
        msg = f"Stored credentials for {GITHUB_USER_TOKEN_SERVICE} could not be located"
        raise CredentialsUnreadableError(msg) from exc
    if document is None:
        return None
    token, login = document.get("token"), document.get("login")
    if matches_github_shape(token, GITHUB_TOKEN_SHAPE) and matches_github_shape(login, GITHUB_LOGIN_SHAPE):
        return {"token": token, "login": login}
    msg = f"Stored credentials for {GITHUB_USER_TOKEN_SERVICE} hold no token and login"
    raise CredentialsUnreadableError(msg)


async def _oauth_usable(
    context: OAuthCredentialContext,
    credentials: dict[str, Any] | None,
    *,
    is_usable: Callable[..., bool] = oauth_credentials_usable,
) -> bool:
    return bool(credentials) and await asyncio.to_thread(
        is_usable,
        context.provider,
        context.runtime_paths,
        credentials,
    )


async def load_usable_github_oauth_credentials(
    context: OAuthCredentialContext | None,
    *,
    load_status: Callable[..., Awaitable[OAuthCredentialsStatus]] = load_oauth_credentials_status,
    is_usable: Callable[..., bool] = oauth_credentials_usable,
) -> dict[str, Any] | None:
    """Return stored GitHub OAuth credentials while usable, without refreshing or creating a store."""
    if context is None:
        return None
    credentials = (await load_status(context, create_store=False)).credentials
    return credentials if await _oauth_usable(context, credentials, is_usable=is_usable) else None


async def refresh_github_oauth_access_token(
    context: OAuthCredentialContext | None,
    *,
    load_status: Callable[..., Awaitable[OAuthCredentialsStatus]] = load_oauth_credentials_status,
    is_usable: Callable[..., bool] = oauth_credentials_usable,
) -> str | None:
    """Return a current GitHub OAuth access token, refreshing if needed, or None when unconnected."""
    if (
        context is None
        or await load_usable_github_oauth_credentials(context, load_status=load_status, is_usable=is_usable) is None
    ):
        return None
    try:
        credentials = await refresh_oauth_credentials(context)
    except (OAuthCredentialUnreadableError, OAuthRefreshRejectedError):
        return None
    if not await _oauth_usable(context, credentials, is_usable=is_usable):
        return None
    token = (credentials or {}).get("token") or (credentials or {}).get("access_token")
    return token if isinstance(token, str) and token else None


async def resolve_owner_github_token(
    owner_user_id: str,
    config: Config | None,
    runtime_paths: RuntimePaths,
) -> tuple[str | None, GitHubAuthMode]:
    """Resolve one owner's GitHub token: saved PAT first, else GitHub App OAuth, else anonymous."""
    context = build_user_github_oauth_context(owner_user_id, config, runtime_paths)
    if context is None:
        return None, "anonymous"
    try:
        saved = await asyncio.to_thread(read_saved_github_token, context)
    except CredentialsUnreadableError:
        logger.warning("github_token_unreadable")
        raise
    if saved is not None:
        return saved["token"], "token"
    access_token = await refresh_github_oauth_access_token(context)
    return (access_token, "app") if access_token is not None else (None, "anonymous")


def github_redirect_next(
    response: httpx.Response,
    method: str,
    content: bytes | None,
) -> tuple[str, httpx.URL, bytes | None] | None:
    """Return the next ``(method, url, content)`` for a redirect strictly within ``https://api.github.com``."""
    url = None if response.next_request is None else response.next_request.url
    if url is None or (url.scheme, url.host, url.port, url.userinfo) != _GITHUB_ORIGIN:
        return None
    if response.status_code == 303:
        return "GET", url, None
    return (method, url, content) if (response.status_code in {307, 308} or method == "GET") else None


async def read_bounded_body(response: httpx.Response, limit: int) -> bytes | None:
    """Return the streamed ``response`` body, or None as soon as it grows past ``limit`` bytes."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def is_github_rate_limited(response: httpx.Response) -> bool:
    """Return whether GitHub answered with a rate limit: any 429, or a 403 with 0 remaining or Retry-After."""
    limited = response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
    return response.status_code == 429 or (response.status_code == 403 and limited)


def parse_retry_after_seconds(headers: Mapping[str, str], *, now: float | None = None) -> float | None:
    """Return positive wait seconds from ``Retry-After`` or ``X-RateLimit-Reset`` (when remaining is 0)."""
    normalized = {name.lower(): value.strip() for name, value in headers.items()}
    current_time = time.time() if now is None else now
    if raw_retry := normalized.get("retry-after", ""):
        wait = _parse_retry_after_value(raw_retry, current_time)
        if wait is not None:
            return wait
    if normalized.get("x-ratelimit-remaining") == "0" and (raw_reset := normalized.get("x-ratelimit-reset", "")):
        return _positive_finite_float(raw_reset, offset=-current_time)
    return None


def _positive_finite_float(raw: str, *, offset: float = 0.0) -> float | None:
    try:
        value = float(raw) + offset
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _parse_retry_after_value(raw: str, current_time: float) -> float | None:
    if (seconds := _positive_finite_float(raw)) is not None:
        return seconds
    try:
        retry_dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_dt.tzinfo is None:
        retry_dt = retry_dt.replace(tzinfo=UTC)
    wait = retry_dt.timestamp() - current_time
    return wait if math.isfinite(wait) and wait > 0 else None


def _parse_next_page(link_header: str | None) -> int | None:
    if not link_header:
        return None
    for raw_url, params in _LINK_ENTRY_RE.findall(link_header):
        rel_match = _LINK_REL_RE.search(params)
        if rel_match is None or "next" not in (rel_match.group(1) or rel_match.group(2) or "").lower().split():
            continue
        try:
            url = httpx.URL(raw_url)
        except httpx.InvalidURL:
            continue
        if (url.scheme, url.host, url.port, url.userinfo) != _GITHUB_ORIGIN:
            continue
        raw_page = url.params.get("page")
        if raw_page is not None and raw_page.isascii() and raw_page.isdigit() and (page := int(raw_page)) >= 1:
            return page
    return None


async def _build_get_result(response: httpx.Response, *, request_etag: str | None, max_bytes: int) -> GitHubGetResult:
    headers = {name.lower(): value for name, value in response.headers.items()}
    if response.status_code == 304:
        return GitHubGetResult(304, headers.get("etag") or request_etag, None, headers, False, None, None)
    body = await read_bounded_body(response, max_bytes)
    if body is None:
        msg = f"GitHub API response exceeded {max_bytes} bytes"
        raise GitHubClientError(msg)
    rate_limited = is_github_rate_limited(response)
    retry_after = parse_retry_after_seconds(headers)
    if response.status_code == 200:
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = "GitHub API returned invalid JSON"
            raise GitHubClientError(msg) from exc
        return GitHubGetResult(
            200,
            headers.get("etag"),
            data,
            headers,
            rate_limited,
            retry_after,
            _parse_next_page(headers.get("link")),
        )
    error_data: dict[str, Any] | None = None
    if body and len(body) <= _MAX_ERROR_BODY_BYTES:
        try:
            decoded = json.loads(body)
            error_data = decoded if isinstance(decoded, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_data = None
    return GitHubGetResult(response.status_code, headers.get("etag"), error_data, headers, rate_limited, retry_after)


async def _send_github_get(
    active_client: httpx.AsyncClient,
    url: httpx.URL,
    headers: dict[str, str],
    *,
    etag: str | None,
    max_bytes: int,
) -> GitHubGetResult:
    method = "GET"
    for redirects in range(_MAX_REDIRECTS + 1):
        async with active_client.stream(method, url, headers=headers, follow_redirects=False) as response:
            hop = github_redirect_next(response, method, None) if redirects < _MAX_REDIRECTS else None
            if hop is not None:
                method, url, _ = hop
                continue
            return await _build_get_result(response, request_etag=etag, max_bytes=max_bytes)
    msg = "GitHub API redirect loop terminated unexpectedly"
    raise GitHubClientError(msg)


async def github_get_json(
    path: str,
    *,
    token: str | None,
    etag: str | None = None,
    timeout_seconds: float = 20.0,
    max_bytes: int = 2 * 1024 * 1024,
    client: httpx.AsyncClient | None = None,
) -> GitHubGetResult:
    """Send one safe, bounded GET request to ``https://api.github.com`` and decode JSON on 200."""
    if not path.startswith("/") or path.startswith("//") or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        msg = f"GitHub API path must start with '/': {path!r}"
        raise ValueError(msg)
    url = httpx.URL(GITHUB_API_BASE + path)
    if (url.scheme, url.host, url.port, url.userinfo) != _GITHUB_ORIGIN:
        msg = f"GitHub API path must target {GITHUB_API_BASE}: {path!r}"
        raise ValueError(msg)
    headers = {"Accept": GITHUB_DEFAULT_ACCEPT, "X-GitHub-Api-Version": GITHUB_API_VERSION, "User-Agent": "mindroom"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    async with asyncio.timeout(timeout_seconds):
        if client is not None:
            return await _send_github_get(client, url, headers, etag=etag, max_bytes=max_bytes)
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as owned_client:
            return await _send_github_get(owned_client, url, headers, etag=etag, max_bytes=max_bytes)
